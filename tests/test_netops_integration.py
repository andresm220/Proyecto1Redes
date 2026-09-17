"""F3 integration tests: the real netops server, over a real stdio pipe.

The core tests exercise the logic directly. These launch the server as a
subprocess and drive it through MCPClient, so a failure here means the wiring
is wrong rather than the logic.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from host.mcp import jsonrpc
from host.mcp.client import McpError, MCPClient
from host.mcp.stdio_transport import StdioTransport
from tests.conftest import read_payload


def make_transport(data_dir) -> StdioTransport:
    return StdioTransport(
        command="python",
        args=["-m", "servers.netops.stdio_server"],
        env={"NETOPS_DATA_DIR": str(data_dir)},
        name="netops",
    )


@pytest.fixture
def client(data_dir):
    client = MCPClient(make_transport(data_dir), name="netops", timeout=20.0)
    client.connect()
    yield client
    client.close()


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_handshake(client):
    assert client.initialized is True
    assert client.server_info == {"name": "netops", "version": "1.0.0"}
    assert client.server_capabilities == {"tools": {"listChanged": False}}
    assert "outage" in client.instructions.lower()


def test_ping(client):
    assert client.ping() == {}


def test_tools_list_returns_the_seven_tools(client):
    tools = client.list_tools()
    assert len(tools) == 7
    assert {tool["name"] for tool in tools} == {
        "lookup_account",
        "check_service_status",
        "list_outages",
        "run_diagnostic",
        "open_ticket",
        "get_ticket",
        "schedule_visit",
    }
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"


def test_calls_before_initialize_are_refused(data_dir):
    """Drive the transport directly, skipping the handshake MCPClient does."""
    transport = make_transport(data_dir)
    transport.start()
    try:
        transport.send(jsonrpc.build_request(1, "tools/list", {}))
        response = jsonrpc.parse_message(transport.receive())
        assert isinstance(response, jsonrpc.ErrorResponse)
        assert response.code == jsonrpc.INVALID_REQUEST
    finally:
        transport.close()


def test_unknown_method_is_method_not_found(client):
    with pytest.raises(McpError) as exc_info:
        client.request("resources/list")
    assert exc_info.value.code == jsonrpc.METHOD_NOT_FOUND


def test_malformed_json_gets_a_parse_error_with_a_null_id(data_dir):
    """A line that is not JSON has no recoverable id, so id must be null."""
    transport = make_transport(data_dir)
    transport.start()
    try:
        transport.send(jsonrpc.build_request(1, "initialize", {"protocolVersion": "2025-11-25"}))
        transport.receive()

        # Bypass encode(), which would refuse to emit this.
        transport._process.stdin.write("{not json at all\n")  # noqa: SLF001
        transport._process.stdin.flush()  # noqa: SLF001

        response = jsonrpc.parse_message(transport.receive())
        assert isinstance(response, jsonrpc.ErrorResponse)
        assert response.code == jsonrpc.PARSE_ERROR
        assert response.id is None
    finally:
        transport.close()


# --------------------------------------------------------------------------
# Tools over the wire
# --------------------------------------------------------------------------


def test_lookup_account_over_the_wire(client):
    result = client.call_tool("lookup_account", {"account_id": "GT-10231"})
    assert result["isError"] is False
    assert read_payload(result)["plan"] == "Fibra 300"


def test_non_ascii_survives_the_whole_stack(client):
    """Holder names and addresses carry accents; cp1252 would mangle them."""
    payload = read_payload(client.call_tool("lookup_account", {"account_id": "GT-10233"}))
    assert payload["holder"] == "Ana Lucía Peralta"
    assert payload["service_address"].endswith("Flores, Petén")


def test_invalid_arguments_are_a_protocol_error(client):
    with pytest.raises(McpError) as exc_info:
        client.call_tool("check_service_status", {})
    assert exc_info.value.code == jsonrpc.INVALID_PARAMS
    assert exc_info.value.data == {"field": "account_id"}


def test_unknown_tool_is_a_protocol_error(client):
    with pytest.raises(McpError) as exc_info:
        client.call_tool("drop_database", {})
    assert exc_info.value.code == jsonrpc.INVALID_PARAMS


def test_missing_account_is_a_tool_error_not_a_protocol_error(client):
    """The exchange succeeded; the domain outcome is the failure."""
    result = client.call_tool("lookup_account", {"account_id": "GT-99999"})
    assert result["isError"] is True
    assert "No se encontró" in read_payload(result)["error"]


def test_state_persists_between_calls(client):
    """open_ticket then get_ticket, as two separate round trips."""
    opened = read_payload(
        client.call_tool(
            "open_ticket",
            {
                "account_id": "GT-10231",
                "category": "connectivity",
                "description": "El servicio se cae cada noche desde el lunes.",
                "priority": "high",
            },
        )
    )
    ticket_id = opened["ticket_id"]

    fetched = read_payload(client.call_tool("get_ticket", {"ticket_id": ticket_id}))
    assert fetched["ticket_id"] == ticket_id
    assert fetched["priority"] == "high"


def test_state_persists_across_two_server_processes(data_dir):
    """The strongest persistence check: a brand new process must still see it."""
    first = MCPClient(make_transport(data_dir), name="netops", timeout=20.0)
    first.connect()
    try:
        opened = read_payload(
            first.call_tool(
                "open_ticket",
                {
                    "account_id": "GT-10234",
                    "category": "equipment",
                    "description": "El router reinicia solo varias veces al día.",
                },
            )
        )
    finally:
        first.close()

    second = MCPClient(make_transport(data_dir), name="netops", timeout=20.0)
    second.connect()
    try:
        fetched = read_payload(
            second.call_tool("get_ticket", {"ticket_id": opened["ticket_id"]})
        )
        assert fetched["account_id"] == "GT-10234"
        assert fetched["status"] == "open"
    finally:
        second.close()


def test_a_full_support_workflow(client):
    """The path a demo would follow: complaint, outage check, ticket, visit."""
    status = read_payload(client.call_tool("check_service_status", {"account_id": "GT-10233"}))
    assert status["reason"] == "mass_outage"

    outages = read_payload(client.call_tool("list_outages", {"region": "peten"}))
    assert outages["outages"][0]["outage_id"] == status["outage_id"]

    ticket = read_payload(
        client.call_tool(
            "open_ticket",
            {
                "account_id": "GT-10233",
                "category": "connectivity",
                "description": "Sigue sin servicio después del ETA publicado.",
                "priority": "critical",
            },
        )
    )
    # Computed, not hardcoded. Everywhere else in the suite the store's clock
    # is frozen at FIXED_NOW, so a literal date is safe; here the server runs
    # as a real subprocess on the real clock, and schedule_visit refuses a date
    # in the past. A literal would pass until that day arrived and fail every
    # run after it - which is exactly what happened to "2026-09-01".
    appointment = (date.today() + timedelta(days=7)).isoformat()
    visit = read_payload(
        client.call_tool(
            "schedule_visit",
            {
                "ticket_id": ticket["ticket_id"],
                "date": appointment,
                "time_window": "12:00-16:00",
            },
        )
    )
    assert visit["status"] == "scheduled"
    assert visit["date"] == appointment

    final = read_payload(client.call_tool("get_ticket", {"ticket_id": ticket["ticket_id"]}))
    assert final["status"] == "scheduled"
    assert final["visit"]["time_window"] == "12:00-16:00"
    assert [entry["status"] for entry in final["history"]] == ["open", "scheduled"]
