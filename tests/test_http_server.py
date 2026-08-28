"""F7 tests: the netops server over Streamable HTTP.

The point of this file is that the *protocol* answers are identical to the
stdio adapter's - same handshake, same error codes, same isError semantics -
while HTTP adds exactly one thing on top: a session id, because a request no
longer arrives down a pipe that identifies itself.

The status code and the JSON-RPC error object describe different layers, and
several tests below exist only to keep them from being conflated.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from host.mcp import jsonrpc as rpc
from servers.netops import core
from servers.netops.http_server import (
    PROTOCOL_HEADER,
    SESSION_HEADER,
    create_app,
)
from servers.netops.store import NetopsStore

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": core.PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"},
    },
}


@pytest.fixture
def client(store: NetopsStore) -> TestClient:
    """A test client over a store with its own private data directory."""
    return TestClient(create_app(store=store))


@pytest.fixture
def session(client: TestClient) -> str:
    """A client that has completed the handshake, and its session id."""
    response = client.post("/mcp", json=INITIALIZE)
    session_id = response.headers[SESSION_HEADER]
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={SESSION_HEADER: session_id, PROTOCOL_HEADER: core.PROTOCOL_VERSION},
    )
    return session_id


def call(client: TestClient, session_id: str, message: dict) -> "object":
    return client.post(
        "/mcp",
        json=message,
        headers={SESSION_HEADER: session_id, PROTOCOL_HEADER: core.PROTOCOL_VERSION},
    )


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


def test_health_reports_the_server_and_its_protocol(client: TestClient):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["server"] == core.SERVER_NAME
    assert body["protocolVersion"] == core.PROTOCOL_VERSION
    assert body["tools"] == len(core.TOOLS)


# --------------------------------------------------------------------------
# The handshake
# --------------------------------------------------------------------------


def test_initialize_answers_with_the_same_result_as_stdio(client: TestClient):
    response = client.post("/mcp", json=INITIALIZE)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")

    result = response.json()["result"]
    assert result["protocolVersion"] == core.PROTOCOL_VERSION
    assert result["serverInfo"] == {"name": core.SERVER_NAME, "version": core.SERVER_VERSION}
    assert result["instructions"] == core.INSTRUCTIONS


def test_initialize_hands_out_a_session_id(client: TestClient):
    """The one thing HTTP needs that a pipe does not: requests arrive
    independently and have to say which conversation they belong to."""
    response = client.post("/mcp", json=INITIALIZE)
    assert response.headers[SESSION_HEADER]


def test_each_initialize_starts_a_separate_session(client: TestClient):
    first = client.post("/mcp", json=INITIALIZE).headers[SESSION_HEADER]
    second = client.post("/mcp", json=INITIALIZE).headers[SESSION_HEADER]
    assert first != second


def test_a_notification_is_accepted_with_no_body(client: TestClient):
    """202 says the server owes nothing, without inventing an empty result."""
    session_id = client.post("/mcp", json=INITIALIZE).headers[SESSION_HEADER]
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={SESSION_HEADER: session_id},
    )
    assert response.status_code == 202
    assert response.content == b""


def test_tools_list_before_the_handshake_completes_is_rejected(client: TestClient):
    session_id = client.post("/mcp", json=INITIALIZE).headers[SESSION_HEADER]
    body = call(client, session_id, rpc.build_request(1, "tools/list")).json()
    assert body["error"]["code"] == rpc.INVALID_REQUEST


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def test_a_request_without_a_session_is_404(client: TestClient):
    """404 is the specified signal to start a new session, not to retry."""
    response = client.post("/mcp", json=rpc.build_request(1, "ping"))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == rpc.INVALID_REQUEST


def test_an_unknown_session_id_is_404(client: TestClient):
    response = client.post(
        "/mcp", json=rpc.build_request(1, "ping"), headers={SESSION_HEADER: "does-not-exist"}
    )
    assert response.status_code == 404


def test_two_sessions_do_not_share_handshake_state(client: TestClient, session: str):
    """State that belongs to a connection must not leak between connections."""
    other = client.post("/mcp", json=INITIALIZE).headers[SESSION_HEADER]
    # `session` finished its handshake; `other` did not.
    assert "result" in call(client, session, rpc.build_request(1, "tools/list")).json()
    assert "error" in call(client, other, rpc.build_request(1, "tools/list")).json()


def test_a_session_can_be_ended_explicitly(client: TestClient, session: str):
    assert client.delete("/mcp", headers={SESSION_HEADER: session}).status_code == 204
    assert call(client, session, rpc.build_request(1, "ping")).status_code == 404


def test_ending_an_unknown_session_is_404(client: TestClient):
    assert client.delete("/mcp", headers={SESSION_HEADER: "nope"}).status_code == 404


def test_sessions_share_one_store(client: TestClient, session: str):
    """Two clients of the same server must see the same tickets."""
    other = client.post("/mcp", json=INITIALIZE).headers[SESSION_HEADER]
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={SESSION_HEADER: other},
    )
    created = call(
        client,
        session,
        rpc.build_request(
            1,
            "tools/call",
            {
                "name": "open_ticket",
                "arguments": {
                    "account_id": "GT-10231",
                    "category": "connectivity",
                    "description": "No hay servicio desde la madrugada.",
                },
            },
        ),
    ).json()
    ticket_id = _payload(created)["ticket_id"]

    fetched = call(
        client,
        other,
        rpc.build_request(
            2, "tools/call", {"name": "get_ticket", "arguments": {"ticket_id": ticket_id}}
        ),
    ).json()
    assert _payload(fetched)["ticket_id"] == ticket_id


# --------------------------------------------------------------------------
# The protocol version header
# --------------------------------------------------------------------------


def test_an_unsupported_protocol_version_header_is_refused(client: TestClient, session: str):
    response = client.post(
        "/mcp",
        json=rpc.build_request(1, "ping"),
        headers={SESSION_HEADER: session, PROTOCOL_HEADER: "1999-01-01"},
    )
    assert response.status_code == 400
    assert "1999-01-01" in response.json()["error"]["message"]


def test_the_header_may_be_omitted(client: TestClient, session: str):
    """The specification tells a server to assume a default rather than refuse,
    so a missing header is tolerated even though our client always sends it."""
    response = client.post(
        "/mcp", json=rpc.build_request(1, "ping"), headers={SESSION_HEADER: session}
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------
# Errors, and the layer each one belongs to
# --------------------------------------------------------------------------


def test_a_body_that_is_not_json_is_a_parse_error(client: TestClient):
    response = client.post(
        "/mcp", content=b"{ not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == rpc.PARSE_ERROR
    assert body["id"] is None, "the id is unrecoverable, so it must be null"


def test_a_json_body_that_is_not_an_object_is_rejected(client: TestClient):
    response = client.post("/mcp", json=[1, 2, 3])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == rpc.INVALID_REQUEST


def test_an_unknown_method_is_a_200_carrying_minus_32601(client: TestClient, session: str):
    """The request was well formed; the answer to it is an error object. The
    exchange succeeded, so the status code has no business saying otherwise."""
    response = call(client, session, rpc.build_request(1, "tools/nope"))
    assert response.status_code == 200
    assert response.json()["error"]["code"] == rpc.METHOD_NOT_FOUND


def test_bad_arguments_are_a_200_carrying_minus_32602(client: TestClient, session: str):
    response = call(
        client,
        session,
        rpc.build_request(1, "tools/call", {"name": "lookup_account", "arguments": {}}),
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == rpc.INVALID_PARAMS


def test_a_domain_failure_is_a_successful_result_with_is_error(
    client: TestClient, session: str
):
    """The other side of the same line: the call was fine, the account is not."""
    response = call(
        client,
        session,
        rpc.build_request(
            1,
            "tools/call",
            {"name": "lookup_account", "arguments": {"account_id": "GT-00000"}},
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body, "a missing account is not a protocol error"
    assert body["result"]["isError"] is True


# --------------------------------------------------------------------------
# The tools themselves are the same ones
# --------------------------------------------------------------------------


def test_tools_list_returns_every_tool(client: TestClient, session: str):
    tools = call(client, session, rpc.build_request(1, "tools/list")).json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == [tool["name"] for tool in core.TOOLS]


def test_ping_answers_an_empty_result(client: TestClient, session: str):
    assert call(client, session, rpc.build_request(1, "ping")).json()["result"] == {}


def test_the_seed_and_the_state_can_live_in_different_directories(tmp_path: Path, data_dir):
    """What lets a container keep the read-only seed in its image and write
    the state to /tmp, which is the only writable path on some platforms."""
    state_dir = tmp_path / "state-only"
    store = NetopsStore(data_dir=state_dir, seed_dir=data_dir / "seed")
    assert store.accounts, "the seed must load from the directory it was given"
    assert store.state_file.parent == state_dir


def _payload(body: dict) -> dict:
    import json

    return json.loads(body["result"]["content"][0]["text"])
