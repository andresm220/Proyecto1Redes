"""Streamable HTTP transport: talk MCP to a remote server over POST /mcp.

`MCPClient` is not changed by this file existing, and neither is the agent.
That is the point of requirement 6: the same client, the same handshake, the
same correlation, reaching a server in the cloud instead of a pipe.

## Fitting request/response into a queue

`Transport` is a queue: `send` puts a message on the wire, `receive` blocks
until one comes back, and nothing promises the two are related. HTTP is the
opposite shape - one POST, one answer, paired by construction.

The join is small. `send` performs the POST and pushes whatever body comes
back onto an inbox; `receive` pops from that inbox. The reader thread inside
`MCPClient` then correlates by `id` exactly as it does over stdio, and never
learns that the answer arrived on the same socket as the question.

A notification is answered with `202 Accepted` and no body, so it pushes
nothing - which is precisely right, because no reply is owed and the reader
thread must not be handed one.

## What this transport tracks that stdio does not

Two headers, both of which it learns rather than being told:

`Mcp-Session-Id` arrives on the response to `initialize` and has to be echoed
on every later request. A pipe needs no such thing - the pipe *is* the session
- but HTTP requests arrive independently and must say which conversation they
belong to.

`MCP-Protocol-Version` must be sent on every request after `initialize`, and
the value is the one the server named in its initialize result, not the one
the client asked for. The transport reads it out of the response body as it
passes through.

Both are captured by watching the traffic, so `MCPClient` does not have to
grow an HTTP-shaped hook to hand them over.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

import httpx

from host.mcp.transport import Transport, TransportError

# Pushed onto the inbox when the transport is closed, so a blocked reader wakes.
_EOF = object()

DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 1
RETRY_BACKOFF_SECONDS = 1.0

SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"


class HttpTransport(Transport):
    """One MCP connection to a remote server, over Streamable HTTP."""

    kind = "http"

    def __init__(
        self,
        url: str,
        name: str = "server",
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        on_stderr: Callable[[str, str], None] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = url
        self.name = name
        self.timeout = timeout
        self.retries = retries
        self._on_stderr = on_stderr

        self._client = client
        self._owns_client = client is None
        self._inbox: queue.Queue[Any] = queue.Queue()
        self._send_lock = threading.Lock()
        self._closed = threading.Event()

        # Learned from the traffic rather than configured.
        self.session_id: str | None = None
        self.protocol_version: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)

    @property
    def is_running(self) -> bool:
        return self._client is not None and not self._closed.is_set()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._client is not None and self._owns_client:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        # Unblock anyone still sitting in receive().
        self._inbox.put(_EOF)

    # -- headers -----------------------------------------------------------

    def _headers(self, message: dict[str, Any]) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Both are advertised because the specification lets a server
            # answer either with one JSON object or with an SSE stream. This
            # server always chooses the former; saying so is still correct.
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers[SESSION_HEADER] = self.session_id
        # Sent on everything *after* initialize: during initialize the version
        # is still being negotiated, so there is nothing truthful to claim yet.
        if self.protocol_version and message.get("method") != "initialize":
            headers[PROTOCOL_HEADER] = self.protocol_version
        return headers

    def _remember(self, message: dict[str, Any], response: httpx.Response, body: Any) -> None:
        """Pick the session id and the negotiated version out of the traffic."""
        if message.get("method") != "initialize":
            return
        session_id = response.headers.get(SESSION_HEADER)
        if session_id:
            self.session_id = session_id
        if isinstance(body, dict):
            result = body.get("result")
            if isinstance(result, dict):
                version = result.get("protocolVersion")
                if isinstance(version, str):
                    self.protocol_version = version

    # -- writing -----------------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        """POST one message, and queue the answer if the server owed one."""
        if self._client is None:
            raise TransportError(f"{self.name}: transport is not started")
        if self._closed.is_set():
            raise TransportError(f"{self.name}: transport is closed")

        # Serialised because the session id and protocol version are read and
        # written here: two concurrent initializes would race over them.
        with self._send_lock:
            response = self._post(message)
            body = self._body_of(response)
            self._remember(message, response, body)

        if body is not None:
            self._inbox.put(body)

    def _post(self, message: dict[str, Any]) -> httpx.Response:
        """POST with one retry on a network failure, none on an HTTP answer.

        A refused connection or a dropped one is worth retrying: the request
        may never have reached the server. A 4xx is not - the server answered,
        it just said no, and repeating the call would only ask again.
        """
        assert self._client is not None
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return self._client.post(
                    self.url, json=message, headers=self._headers(message), timeout=self.timeout
                )
            except httpx.TimeoutException as exc:
                last = exc
                detail = f"no response within {self.timeout}s"
            except httpx.TransportError as exc:
                last = exc
                detail = str(exc)
            if attempt < self.retries:
                self._report(f"{detail}; retrying once")
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        raise TransportError(f"{self.name}: POST {self.url} failed: {last}") from last

    def _body_of(self, response: httpx.Response) -> dict[str, Any] | None:
        """Turn an HTTP answer into the message it carries, or None.

        202 means the server owed nothing, which is the correct answer to a
        notification. Any other non-2xx is an exchange-level failure: the
        request never became a JSON-RPC conversation, so it is raised rather
        than pushed onto the inbox where it would sit uncorrelated forever.
        """
        if response.status_code == 202 or not response.content:
            return None

        if response.status_code >= 400:
            raise TransportError(
                f"{self.name}: HTTP {response.status_code} from {self.url}: "
                f"{self._explain(response)}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise TransportError(
                f"{self.name}: response was not JSON: {response.text[:200]}"
            ) from exc
        if not isinstance(body, dict):
            raise TransportError(f"{self.name}: response was not a JSON object")
        return body

    @staticmethod
    def _explain(response: httpx.Response) -> str:
        """Prefer the JSON-RPC error message the server put in the body."""
        try:
            body = response.json()
        except ValueError:
            return response.text[:200]
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            return str(body["error"].get("message", body["error"]))
        return response.text[:200]

    # -- reading -----------------------------------------------------------

    def receive(self) -> dict[str, Any] | None:
        item = self._inbox.get()
        if item is _EOF:
            # Put it back so every other waiter also sees the close.
            self._inbox.put(_EOF)
            return None
        return item

    def _report(self, text: str) -> None:
        if self._on_stderr is not None:
            self._on_stderr(self.name, text)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "open" if self.is_running else "closed"
        return f"<HttpTransport {self.name} {self.url} {state}>"
