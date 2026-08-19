"""MCPClient: the MCP session on top of a Transport.

The transport moves messages; this class owns everything above that line -
request/response correlation, the initialize handshake, and the MCP methods
themselves (tools/list, tools/call, ping).

Correlation runs on a dedicated reader thread: every outgoing request registers
a Future under its id, and the reader resolves it when the matching response
arrives. If the server dies, every pending Future is failed rather than left
hanging, so a caller never blocks forever on a peer that is gone.
"""

from __future__ import annotations

import itertools
import threading
from concurrent.futures import Future
from typing import Any, Callable

from host.mcp import jsonrpc
from host.mcp.transport import Transport, TransportError

PROTOCOL_VERSION = "2025-11-25"
CLIENT_NAME = "uvg-mcp-host"
CLIENT_VERSION = "0.1.0"

DEFAULT_TIMEOUT = 30.0


class McpError(Exception):
    """The server answered with a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")


class ProtocolVersionError(Exception):
    """The server offered a protocol version this client does not support.

    The specification requires the client to disconnect and report, rather than
    trying to carry on with a version it cannot guarantee.
    """

    def __init__(self, offered: str, supported: str) -> None:
        self.offered = offered
        self.supported = supported
        super().__init__(
            f"server offered protocol version {offered!r}, this client supports {supported!r}"
        )


class MCPClient:
    """One MCP session against one server."""

    def __init__(
        self,
        transport: Transport,
        name: str = "server",
        protocol_version: str = PROTOCOL_VERSION,
        timeout: float = DEFAULT_TIMEOUT,
        on_message: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.transport = transport
        self.name = name
        self.protocol_version = protocol_version
        self.timeout = timeout
        self._on_message = on_message

        self._ids = itertools.count(1)
        self._id_lock = threading.Lock()
        self._pending: dict[jsonrpc.MessageId, Future] = {}
        self._pending_lock = threading.Lock()

        self._reader: threading.Thread | None = None
        self._closed = threading.Event()

        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        self.instructions: str = ""
        self.initialized = False

    # -- plumbing ----------------------------------------------------------

    def _next_id(self) -> int:
        with self._id_lock:
            return next(self._ids)

    def _log(self, direction: str, message: dict[str, Any]) -> None:
        if self._on_message is not None:
            self._on_message(self.name, direction, message)

    def _send(self, message: dict[str, Any]) -> None:
        self._log("send", message)
        self.transport.send(message)

    def _read_loop(self) -> None:
        """Resolve pending futures as responses arrive; stop at EOF."""
        try:
            while True:
                raw = self.transport.receive()
                if raw is None:
                    break
                self._log("recv", raw)
                try:
                    message = jsonrpc.parse_message(raw)
                except jsonrpc.JsonRpcError:
                    # A malformed envelope from the server is not something we
                    # can correlate; drop it and keep the session alive.
                    continue
                self._handle(message)
        finally:
            self._fail_all_pending(
                TransportError(f"{self.name}: connection closed before the response arrived")
            )

    def _handle(self, message: jsonrpc.Message) -> None:
        if isinstance(message, (jsonrpc.SuccessResponse, jsonrpc.ErrorResponse)):
            self._resolve(message)
        elif isinstance(message, jsonrpc.Request):
            # This host exposes no methods to servers. Answering -32601 is
            # correct and keeps the server from waiting on a reply forever.
            self._send(
                jsonrpc.build_error(
                    message.id,
                    jsonrpc.METHOD_NOT_FOUND,
                    f"this client exposes no method {message.method!r}",
                )
            )
        # Notifications from the server carry no id and are never answered.

    def _resolve(self, message: jsonrpc.SuccessResponse | jsonrpc.ErrorResponse) -> None:
        with self._pending_lock:
            future = self._pending.pop(message.id, None)
        if future is None or future.done():
            return  # late or duplicate response for an id we no longer track
        if isinstance(message, jsonrpc.SuccessResponse):
            future.set_result(message.result)
        else:
            future.set_exception(McpError(message.code, message.message, message.data))

    def _fail_all_pending(self, error: Exception) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send a request and block until its response arrives."""
        if self._closed.is_set():
            raise TransportError(f"{self.name}: session is closed")

        message_id = self._next_id()
        future: Future = Future()
        with self._pending_lock:
            self._pending[message_id] = future

        try:
            self._send(jsonrpc.build_request(message_id, method, params or {}))
        except Exception:
            with self._pending_lock:
                self._pending.pop(message_id, None)
            raise

        try:
            return future.result(timeout=timeout or self.timeout)
        except TimeoutError as exc:
            with self._pending_lock:
                self._pending.pop(message_id, None)
            raise TransportError(
                f"{self.name}: no response to {method!r} within {timeout or self.timeout}s"
            ) from exc

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send(jsonrpc.build_notification(method, params or {}))

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> dict[str, Any]:
        """Run the mandatory handshake: initialize, version check, initialized."""
        self.transport.start()
        self._reader = threading.Thread(
            target=self._read_loop, name=f"{self.name}-session", daemon=True
        )
        self._reader.start()

        result = self.request(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )

        offered = result.get("protocolVersion")
        if offered != self.protocol_version:
            # Required by the spec: disconnect rather than proceed on a version
            # we cannot honour.
            self.close()
            raise ProtocolVersionError(str(offered), self.protocol_version)

        self.server_info = result.get("serverInfo", {})
        self.server_capabilities = result.get("capabilities", {})
        self.instructions = result.get("instructions", "") or ""

        self.notify("notifications/initialized")
        self.initialized = True
        return result

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self.initialized = False
        self.transport.close()
        self._fail_all_pending(TransportError(f"{self.name}: session closed"))
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=self.timeout)

    def __enter__(self) -> "MCPClient":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- MCP methods -------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        self._require_initialized("tools/list")
        result = self.request("tools/list")
        tools = result.get("tools", [])
        if not isinstance(tools, list):
            raise McpError(jsonrpc.INTERNAL_ERROR, "tools/list did not return a list of tools")
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke one tool.

        A domain failure comes back as a successful result carrying
        `isError: true`; only a protocol failure raises McpError.
        """
        self._require_initialized("tools/call")
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def ping(self) -> Any:
        return self.request("ping")

    def _require_initialized(self, method: str) -> None:
        if not self.initialized:
            raise TransportError(f"{self.name}: {method} called before the handshake completed")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "initialized" if self.initialized else "not initialized"
        return f"<MCPClient {self.name} {state}>"
