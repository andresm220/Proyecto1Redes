"""Streamable HTTP transport adapter for the netops server.

Run with:  uvicorn servers.netops.http_server:app --host 0.0.0.0 --port 8080

The same server as `stdio_server.py`, reached over HTTP instead of a pipe.
Neither adapter re-implements the protocol: both wrap `NetopsSession`, and the
business logic stays in `core.py`. That is the whole point of requirement 6 -
"the same server, deployed remotely" has to mean the same code, not a copy.

## The endpoint

One endpoint, `POST /mcp`, carrying exactly one JSON-RPC message per request.
The status code says what kind of message the server received, which is the
part of Streamable HTTP that actually matters here:

    request       200 with `Content-Type: application/json` and the response
    notification  202 Accepted with no body, because none is owed
    response      202 Accepted, likewise

Only the single-JSON-response mode is implemented. The specification also
allows answering with an SSE stream, and `GET /mcp` for a server-initiated
one, but nothing in this project needs a server to push: every exchange is a
client request and its answer.

## Sessions

`initialize` creates a session and returns its id in the `Mcp-Session-Id`
response header; every later request must carry that header back. This is the
one thing HTTP needs that stdio does not - a pipe *is* the session, and it
ends when the process does, whereas HTTP requests arrive independently and
have to be told which conversation they belong to.

An unknown session id is a 404, which the specification defines as the signal
for a client to start a new session rather than retry.

## Errors, and which layer they belong to

An HTTP status describes the *exchange*: the body was not JSON, the session is
gone, the protocol version is one we cannot speak. A JSON-RPC error object
describes the *message*: unknown method, bad arguments. A well-formed call
that simply cannot succeed is neither - it is a 200 carrying a successful
result with `isError: true`, produced inside `core.dispatch`.

So `-32601 Method not found` comes back as HTTP 200, because the request was
perfectly well formed and the answer to it is an error object. Conflating the
two layers would make the capture analysis unreadable.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from host.mcp import jsonrpc
from servers.netops import core
from servers.netops.protocol import NetopsSession
from servers.netops.store import NetopsStore

SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"

# Every revision this server can answer. A client that asks for one of these
# gets it back from initialize; anything else is refused at the header check.
SUPPORTED_PROTOCOL_VERSIONS = (core.PROTOCOL_VERSION,)

# Sessions are cheap - a handshake flag and a reference to the shared store -
# but an abandoned one still occupies a slot, so the count is capped rather
# than left to grow without bound on a public endpoint.
MAX_SESSIONS = 500

logger = logging.getLogger("netops.http")


class SessionRegistry:
    """The live sessions, keyed by the id handed out at initialize.

    Guarded by a lock because the session work runs on FastAPI's thread pool
    rather than on the event loop, so two requests really can touch this at
    the same time.
    """

    def __init__(self, store: NetopsStore, max_sessions: int = MAX_SESSIONS) -> None:
        self.store = store
        self.max_sessions = max_sessions
        self._sessions: dict[str, NetopsSession] = {}
        self._lock = threading.Lock()

    def create(self) -> tuple[str, NetopsSession]:
        session_id = uuid.uuid4().hex
        session = NetopsSession(store=self.store, log=logger.info)
        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                # Drop the oldest rather than refuse the newest: a client that
                # never says goodbye must not be able to lock everyone else out.
                oldest = next(iter(self._sessions))
                del self._sessions[oldest]
                logger.warning("session table full, evicted %s", oldest)
            self._sessions[session_id] = session
        return session_id, session

    def get(self, session_id: str | None) -> NetopsSession | None:
        if not session_id:
            return None
        with self._lock:
            return self._sessions.get(session_id)

    def drop(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


def create_app(store: NetopsStore | None = None) -> FastAPI:
    """Build the application. Taking the store as an argument keeps it testable."""
    app = FastAPI(
        title=f"{core.SERVER_NAME} MCP server",
        version=core.SERVER_VERSION,
        description="ISP technical support over MCP, Streamable HTTP transport.",
    )
    registry = SessionRegistry(store or NetopsStore())
    app.state.registry = registry

    def error(status: int, code: int, message: str, message_id: Any = None) -> JSONResponse:
        """An exchange-level failure, reported in both layers at once.

        The status code is what an HTTP intermediary understands; the body is
        a JSON-RPC error so a client that only reads the body still learns why.
        """
        return JSONResponse(
            status_code=status,
            content=jsonrpc.build_error(message_id, code, message),
        )

    # Reading a body has to be awaited, so the handlers are async - but the
    # session work is not. The store does blocking file I/O on every ticket, so
    # it is pushed to the thread pool: one slow write must not stall the event
    # loop and every other request with it.

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Liveness probe for the platform, and a quick manual sanity check."""
        return {
            "status": "ok",
            "server": core.SERVER_NAME,
            "version": core.SERVER_VERSION,
            "protocolVersion": core.PROTOCOL_VERSION,
            "transport": "streamable-http",
            "tools": len(core.TOOLS),
            "sessions": len(registry),
        }

    @app.post("/mcp")
    async def mcp(request: Request, response: Response) -> Any:
        raw_body = await request.body()
        try:
            message = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # The id cannot be recovered from a body we could not parse, so the
            # error carries id: null - the one case where that is legal.
            return error(400, jsonrpc.PARSE_ERROR, f"invalid JSON: {exc}")
        if not isinstance(message, dict):
            return error(
                400, jsonrpc.INVALID_REQUEST, "a JSON-RPC message must be a JSON object"
            )

        message_id = message.get("id")
        method = message.get("method")

        version = request.headers.get(PROTOCOL_HEADER)
        if version is not None and version not in SUPPORTED_PROTOCOL_VERSIONS:
            listed = ", ".join(SUPPORTED_PROTOCOL_VERSIONS)
            return error(
                400,
                jsonrpc.INVALID_REQUEST,
                f"unsupported {PROTOCOL_HEADER} {version!r}; this server speaks {listed}",
                message_id,
            )

        session_id = request.headers.get(SESSION_HEADER)
        if method == "initialize":
            # A fresh handshake always starts a fresh session, even if the
            # client sent an id: re-initialising is how a client recovers.
            session_id, session = registry.create()
            response.headers[SESSION_HEADER] = session_id
        else:
            session = registry.get(session_id)
            if session is None:
                # 404 is the specified signal to start a new session, rather
                # than an invitation to retry the same one.
                return error(
                    404,
                    jsonrpc.INVALID_REQUEST,
                    f"unknown or expired {SESSION_HEADER}; send initialize first",
                    message_id,
                )

        reply = await run_in_threadpool(session.handle_message, message)
        if reply is None:
            # A notification, or a response we did not ask for. Nothing is
            # owed, and 202 says so without inventing an empty JSON body.
            return Response(status_code=202, headers=dict(response.headers))

        response.headers["Content-Type"] = "application/json"
        return JSONResponse(content=reply, headers=dict(response.headers))

    @app.delete("/mcp")
    def end_session(request: Request) -> Response:
        """Explicit session teardown. Optional in the spec, cheap to honour."""
        dropped = registry.drop(request.headers.get(SESSION_HEADER))
        return Response(status_code=204 if dropped else 404)

    return app


app = create_app()


def main() -> int:  # pragma: no cover - exercised by running the container
    import uvicorn

    # Cloud Run injects the port; everything else is a local default.
    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    logging.basicConfig(level=logging.INFO, format="[netops] %(message)s")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
