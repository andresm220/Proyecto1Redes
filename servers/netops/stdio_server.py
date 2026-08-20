"""stdio transport adapter for the netops server.

Run with:  python -m servers.netops.stdio_server

Reads one JSON-RPC message per line from stdin and writes one per line to
stdout. stdout carries protocol and nothing else; every log line goes to
stderr, so a stray print can never corrupt the message stream.

All business logic lives in core.py. This file only maps the wire onto it,
which is what will let an HTTP adapter sit beside it later without moving code.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any

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


def log(text: str) -> None:
    print(f"[netops] {text}", file=sys.stderr, flush=True)


class NetopsStdioServer:
    def __init__(self, store: NetopsStore | None = None) -> None:
        self.store = store or NetopsStore()
        # Both halves of the handshake must happen, in order. Tracking only the
        # notification would let a client skip initialize entirely and still be
        # treated as initialized.
        self.initialize_received = False
        self.initialized = False

    # -- methods -----------------------------------------------------------

    def handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        client = params.get("clientInfo", {})
        log(f"initialize from {client.get('name', '?')} {client.get('version', '?')}")
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
                log("ignoring notifications/initialized: initialize never completed")
                return
            self.initialized = True
            log("handshake complete")
        else:
            log(f"ignoring unknown notification: {notification.method}")

    # -- the loop ----------------------------------------------------------

    def handle_line(self, line: str) -> dict[str, Any] | None:
        """Turn one input line into one response, or None if none is owed."""
        try:
            raw = jsonrpc.decode(line)
        except jsonrpc.ParseError as exc:
            # The id is unrecoverable here, so the response carries id: null.
            return jsonrpc.build_error_from(None, exc)

        try:
            message = jsonrpc.parse_message(raw)
        except jsonrpc.JsonRpcError as exc:
            return jsonrpc.build_error_from(raw.get("id"), exc)

        if isinstance(message, jsonrpc.Notification):
            self.handle_notification(message)
            return None

        if not isinstance(message, jsonrpc.Request):
            # A response arriving here means the peer is confused; log and drop.
            log(f"ignoring unexpected {type(message).__name__}")
            return None

        try:
            return jsonrpc.build_response(message.id, self.dispatch(message))
        except jsonrpc.JsonRpcError as exc:
            return jsonrpc.build_error_from(message.id, exc)
        except Exception as exc:  # noqa: BLE001 - the last line of defence
            # An unhandled failure is -32603. The traceback goes to stderr so it
            # is debuggable without ever reaching the protocol stream.
            log(f"unhandled error in {message.method}:\n{traceback.format_exc()}")
            return jsonrpc.build_error_from(
                message.id, jsonrpc.InternalError(f"{type(exc).__name__}: {exc}")
            )

    def serve(self, stdin=None, stdout=None) -> int:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        log(f"listening on stdio, protocol {core.PROTOCOL_VERSION}")

        for line in stdin:
            if not line.strip():
                continue
            response = self.handle_line(line)
            if response is not None:
                stdout.write(jsonrpc.encode(response) + "\n")
                stdout.flush()

        log("stdin closed, shutting down")
        return 0


def main() -> int:
    # Windows defaults these streams to cp1252, which would corrupt any accented
    # payload. The host also sets PYTHONIOENCODING, but a server launched by
    # another client (Claude Desktop, say) may not get that, so force it here.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    return NetopsStdioServer().serve()


if __name__ == "__main__":
    sys.exit(main())
