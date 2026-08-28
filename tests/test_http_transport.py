"""F7 tests: the HTTP transport, and the claim that rests on it.

The claim is requirement 6's: the same server consumed the same way, local or
remote. The way to prove it is not to inspect the code but to run the
unmodified `MCPClient` against a real uvicorn process and watch it complete the
handshake, list tools and call one - with nothing in the client aware that the
answer came off a socket.

Two shapes of test live here. The ones against a live server are integration
tests and cost a port and a thread. The ones against a scripted `httpx`
transport are unit tests of the awkward parts: headers, retries, and the fit
between a request/response protocol and a queue-shaped interface.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn

from host.mcp.client import MCPClient
from host.mcp.http_transport import (
    PROTOCOL_HEADER,
    SESSION_HEADER,
    HttpTransport,
)
from host.mcp.transport import TransportError
from servers.netops import core
from servers.netops.http_server import create_app
from servers.netops.store import NetopsStore


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def live_server(store: NetopsStore):
    """A real uvicorn serving the real app, on a real socket."""
    port = free_port()
    config = uvicorn.Config(
        create_app(store=store), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover - CI safety net
            raise RuntimeError("uvicorn did not start in time")
        time.sleep(0.05)

    yield f"http://127.0.0.1:{port}/mcp"

    server.should_exit = True
    thread.join(timeout=10)


# --------------------------------------------------------------------------
# The whole point: the unmodified client, over HTTP
# --------------------------------------------------------------------------


def test_the_unmodified_client_completes_the_handshake(live_server: str):
    client = MCPClient(HttpTransport(url=live_server, name="netops-remote"), name="netops-remote")
    try:
        client.connect()
        assert client.initialized is True
        assert client.negotiated_version == core.PROTOCOL_VERSION
        assert client.server_info == {
            "name": core.SERVER_NAME,
            "version": core.SERVER_VERSION,
        }
    finally:
        client.close()


def test_the_same_client_lists_and_calls_tools_over_http(live_server: str):
    client = MCPClient(HttpTransport(url=live_server), name="netops-remote")
    try:
        client.connect()
        tools = client.list_tools()
        assert [tool["name"] for tool in tools] == [tool["name"] for tool in core.TOOLS]

        result = client.call_tool("lookup_account", {"account_id": "GT-10231"})
        payload = json.loads(result["content"][0]["text"])
        assert payload["account_id"] == "GT-10231"
        assert payload["plan"] == "Fibra 300"
    finally:
        client.close()


def test_a_domain_error_survives_the_trip_unchanged(live_server: str):
    """isError has to mean the same thing over HTTP as over a pipe."""
    client = MCPClient(HttpTransport(url=live_server), name="netops-remote")
    try:
        client.connect()
        result = client.call_tool("lookup_account", {"account_id": "GT-00000"})
        assert result["isError"] is True
    finally:
        client.close()


def test_a_protocol_error_still_raises_over_http(live_server: str):
    """And so does the other side of the line: -32602 is not a result."""
    from host.mcp.client import McpError

    client = MCPClient(HttpTransport(url=live_server), name="netops-remote")
    try:
        client.connect()
        with pytest.raises(McpError) as exc_info:
            client.call_tool("lookup_account", {})
        assert exc_info.value.code == -32602
    finally:
        client.close()


def test_ping_works_over_http(live_server: str):
    client = MCPClient(HttpTransport(url=live_server))
    try:
        client.connect()
        assert client.ping() == {}
    finally:
        client.close()


def test_the_transport_reports_itself_as_http(live_server: str):
    """The session log tags every message with the transport that carried it."""
    transport = HttpTransport(url=live_server)
    assert transport.kind == "http"


# --------------------------------------------------------------------------
# Headers the transport learns rather than being told
# --------------------------------------------------------------------------


class Recorder:
    """A scripted httpx transport that records the requests it is handed."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def initialize_response(session_id: str = "abc123") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": core.PROTOCOL_VERSION,
                "capabilities": {},
                "serverInfo": {"name": "netops", "version": "1.0.0"},
            },
        },
        headers={SESSION_HEADER: session_id},
    )


def test_the_session_id_is_taken_from_the_initialize_response(monkeypatch):
    recorder = Recorder([initialize_response("session-42")])
    transport = HttpTransport(url="http://x/mcp", client=recorder.client())
    transport.start()

    transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert transport.session_id == "session-42"


def test_the_session_id_is_echoed_on_every_later_request():
    ok = httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {}})
    recorder = Recorder([initialize_response("session-42"), ok])
    transport = HttpTransport(url="http://x/mcp", client=recorder.client())
    transport.start()

    transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    transport.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})

    assert SESSION_HEADER not in recorder.requests[0].headers
    assert recorder.requests[1].headers[SESSION_HEADER] == "session-42"


def test_the_protocol_version_header_is_the_one_the_server_named():
    """Not the one the client asked for: the server chooses, and the header
    has to state what the session actually runs on."""
    ok = httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {}})
    recorder = Recorder([initialize_response(), ok])
    transport = HttpTransport(url="http://x/mcp", client=recorder.client())
    transport.start()

    transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    transport.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})

    assert recorder.requests[1].headers[PROTOCOL_HEADER] == core.PROTOCOL_VERSION


def test_the_version_header_is_absent_during_initialize():
    """There is nothing truthful to claim while the version is being agreed."""
    recorder = Recorder([initialize_response()])
    transport = HttpTransport(url="http://x/mcp", client=recorder.client())
    transport.start()
    transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    assert PROTOCOL_HEADER not in recorder.requests[0].headers


def test_both_response_shapes_are_advertised():
    recorder = Recorder([initialize_response()])
    transport = HttpTransport(url="http://x/mcp", client=recorder.client())
    transport.start()
    transport.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    accept = recorder.requests[0].headers["Accept"]
    assert "application/json" in accept and "text/event-stream" in accept


# --------------------------------------------------------------------------
# Fitting request/response into a queue
# --------------------------------------------------------------------------


def test_a_response_body_is_queued_for_the_reader():
    recorder = Recorder([httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})])
    transport = HttpTransport(url="http://x/mcp", client=recorder.client())
    transport.start()

    transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert transport.receive() == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_a_notification_queues_nothing():
    """202 means nothing is owed. Queueing an empty message would hand the
    reader thread something it could never correlate."""
    recorder = Recorder([httpx.Response(202)])
    transport = HttpTransport(url="http://x/mcp", client=recorder.client())
    transport.start()

    transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    transport.close()
    assert transport.receive() is None, "only the close marker should be waiting"


def test_closing_wakes_a_blocked_reader():
    transport = HttpTransport(url="http://x/mcp", client=Recorder([]).client())
    transport.start()
    transport.close()
    assert transport.receive() is None


def test_close_is_idempotent():
    transport = HttpTransport(url="http://x/mcp", client=Recorder([]).client())
    transport.start()
    transport.close()
    transport.close()


def test_sending_before_start_is_refused():
    transport = HttpTransport(url="http://x/mcp")
    with pytest.raises(TransportError, match="not started"):
        transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})


def test_sending_after_close_is_refused():
    transport = HttpTransport(url="http://x/mcp", client=Recorder([]).client())
    transport.start()
    transport.close()
    with pytest.raises(TransportError, match="closed"):
        transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})


# --------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------


def test_an_http_error_becomes_a_transport_error_carrying_the_reason():
    """A 404 never became a JSON-RPC conversation, so it cannot be pushed onto
    the inbox where it would sit uncorrelated forever."""
    body = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "unknown session"}}
    recorder = Recorder([httpx.Response(404, json=body)])
    transport = HttpTransport(url="http://x/mcp", client=recorder.client())
    transport.start()

    with pytest.raises(TransportError, match="unknown session"):
        transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})


def test_a_network_failure_is_retried_once():
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    transport = HttpTransport(
        url="http://x/mcp",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    transport.start()
    transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})

    assert len(attempts) == 2
    assert transport.receive()["result"] == {}


def test_a_persistent_network_failure_gives_up_and_reports():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = HttpTransport(
        url="http://x/mcp",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    transport.start()
    with pytest.raises(TransportError, match="failed"):
        transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})


def test_an_http_answer_is_not_retried():
    """The server replied; asking again would only get the same no."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, json={"error": {"code": -32600, "message": "no"}})

    transport = HttpTransport(
        url="http://x/mcp",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    transport.start()
    with pytest.raises(TransportError):
        transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})

    assert len(attempts) == 1


def test_a_non_json_response_is_reported_clearly():
    recorder = Recorder([httpx.Response(200, text="<html>gateway error</html>")])
    transport = HttpTransport(url="http://x/mcp", client=recorder.client())
    transport.start()
    with pytest.raises(TransportError, match="not JSON"):
        transport.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})


def test_connecting_to_a_dead_endpoint_fails_rather_than_hanging():
    """A caller must never block forever on a peer that is not there."""
    port = free_port()  # nothing is listening on it
    client = MCPClient(
        HttpTransport(url=f"http://127.0.0.1:{port}/mcp", retries=0, timeout=5.0),
        name="dead",
    )
    with pytest.raises(TransportError):
        client.connect()
    client.close()
