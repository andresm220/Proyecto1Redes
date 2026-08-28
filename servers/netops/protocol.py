"""The netops MCP protocol engine, with no transport attached.

This holds everything about *being an MCP server* that is not about moving
bytes: the handshake and its ordering, the method table, and the mapping from
a failure to the right JSON-RPC error code. It takes a decoded message and
returns the message owed in reply, or None when none is owed.

Both adapters wrap it. `stdio_server.py` feeds it lines off stdin;
`http_server.py` feeds it the body of a POST. Neither one re-implements the
protocol, which is the server-side counterpart of the rule the host follows:
if adding the remote transport had meant duplicating the state machine, the
abstraction would be in the wrong place.

The two error kinds are kept apart deliberately. A malformed exchange - bad
JSON, an unknown method, arguments that do not satisfy the advertised schema -
is a JSON-RPC `error` object with a negative code. A well-formed call that
cannot succeed - an account that does not exist - is a successful `result`
carrying `isError: true`, produced inside core.dispatch. They are not
interchangeable, and the distinction is visible in the session log.
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from host.mcp import jsonrpc
from servers.netops import core
from servers.netops.store import NetopsStore

SUPPORTED_METHODS = (
    "initialize",
    "notifications/initialized",
    "tools/list",
    "tools/call",
    "ping",
)


class NetopsSession:
    """One MCP session: the handshake state plus the methods it unlocks.

    A session is per-connection state, not per-server state. The store is
    shared: two clients talking to the same server must see the same tickets.
    """

    def __init__(
        self,
        store: NetopsStore | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store or NetopsStore()
        self._log = log or (lambda _text: None)
        # Both halves of the handshake must happen, in order. Tracking only the
        # notification would let a client skip initialize entirely and still be
        # treated as initialized.
        self.initialize_received = False
        self.initialized = False

    # -- methods -----------------------------------------------------------

    def handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        client = params.get("clientInfo", {})
        if not isinstance(client, dict):
            client = {}
        self._log(f"initialize from {client.get('name', '?')} {client.get('version', '?')}")
        self.initialize_received = True
        return {
            "protocolVersion": core.PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": core.SERVER_NAME, "version": core.SERVER_VERSION},
            "instructions": core.INSTRUCTIONS,
        }

    def handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise jsonrpc.InvalidParamsError("params.name must be a non-empty string")
        return core.dispatch(self.store, name, params.get("arguments", {}))

    def dispatch(self, request: jsonrpc.Request) -> Any:
        method = request.method
        params = request.params if isinstance(request.params, dict) else {}

        if method == "initialize":
            return self.handle_initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            self._require_initialized(method)
            return core.list_tools()
        if method == "tools/call":
            self._require_initialized(method)
            return self.handle_tools_call(params)
        raise jsonrpc.MethodNotFoundError(f"Method not found: {method}")

    def _require_initialized(self, method: str) -> None:
        if not self.initialized:
            raise jsonrpc.InvalidRequestError(
                f"{method} was called before the initialize handshake completed"
            )

    def handle_notification(self, notification: jsonrpc.Notification) -> None:
        # A notification is never answered, whatever it is.
        if notification.method == "notifications/initialized":
            if not self.initialize_received:
                # The notification confirms a handshake; it cannot start one.
                self._log("ignoring notifications/initialized: initialize never completed")
                return
            self.initialized = True
            self._log("handshake complete")
        else:
            self._log(f"ignoring unknown notification: {notification.method}")

    # -- entry points ------------------------------------------------------

    def handle_message(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        """Route one decoded message. Returns the reply, or None if none is owed."""
        try:
            message = jsonrpc.parse_message(raw)
        except jsonrpc.JsonRpcError as exc:
            return jsonrpc.build_error_from(raw.get("id") if isinstance(raw, dict) else None, exc)

        if isinstance(message, jsonrpc.Notification):
            self.handle_notification(message)
            return None

        if not isinstance(message, jsonrpc.Request):
            # A response arriving here means the peer is confused; log and drop.
            self._log(f"ignoring unexpected {type(message).__name__}")
            return None

        try:
            return jsonrpc.build_response(message.id, self.dispatch(message))
        except jsonrpc.JsonRpcError as exc:
            return jsonrpc.build_error_from(message.id, exc)
        except Exception as exc:  # noqa: BLE001 - the last line of defence
            # An unhandled failure is -32603. The traceback goes to the log so
            # it is debuggable without ever reaching the protocol stream.
            self._log(f"unhandled error in {message.method}:\n{traceback.format_exc()}")
            return jsonrpc.build_error_from(
                message.id, jsonrpc.InternalError(f"{type(exc).__name__}: {exc}")
            )

    def handle_line(self, line: str) -> dict[str, Any] | None:
        """Decode one NDJSON line, then route it.

        Only the stdio adapter needs this: over HTTP the body has already been
        parsed by the web framework before it reaches us.
        """
        try:
            raw = jsonrpc.decode(line)
        except jsonrpc.ParseError as exc:
            # The id is unrecoverable here, so the response carries id: null.
            return jsonrpc.build_error_from(None, exc)
        return self.handle_message(raw)
