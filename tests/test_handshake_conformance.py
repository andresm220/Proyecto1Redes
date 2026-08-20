"""F5 conformance tests, from bugs a real client shook loose.

Both of these were found by driving the server from PowerShell rather than from
the host, which is exactly what Claude Desktop or any third-party client does.
"""

from __future__ import annotations

import json

import pytest

from host.mcp import jsonrpc
from host.mcp.stdio_transport import StdioTransport

BOM = "\ufeff"


def make_transport(data_dir) -> StdioTransport:
    return StdioTransport(
        command="python",
        args=["-m", "servers.netops.stdio_server"],
        env={"NETOPS_DATA_DIR": str(data_dir)},
        name="netops",
    )


# --------------------------------------------------------------------------
# A byte order mark must not kill the session
# --------------------------------------------------------------------------


def test_decode_ignores_a_leading_byte_order_mark():
    """PowerShell prepends one on every pipe; RFC 8259 lets a parser ignore it."""
    line = BOM + '{"jsonrpc":"2.0","id":1,"method":"ping"}'
    assert jsonrpc.decode(line)["method"] == "ping"


def test_a_bom_still_round_trips_through_parse_message():
    message = jsonrpc.parse_line(BOM + jsonrpc.encode(jsonrpc.build_request(1, "ping")))
    assert isinstance(message, jsonrpc.Request)


def test_the_server_survives_a_bom_on_the_first_line(data_dir):
    transport = make_transport(data_dir)
    transport.start()
    try:
        request = jsonrpc.encode(
            jsonrpc.build_request(
                0,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "bom-client", "version": "1.0"},
                },
            )
        )
        transport._process.stdin.write(BOM + request + "\n")  # noqa: SLF001
        transport._process.stdin.flush()  # noqa: SLF001

        response = jsonrpc.parse_message(transport.receive())
        assert isinstance(response, jsonrpc.SuccessResponse), response
        assert response.result["serverInfo"]["name"] == "netops"
    finally:
        transport.close()


# --------------------------------------------------------------------------
# The handshake cannot be half-completed
# --------------------------------------------------------------------------


def test_the_initialized_notification_alone_does_not_open_the_session(data_dir):
    """The notification confirms a handshake; it must not be able to start one.

    Sending only notifications/initialized, with no initialize before it, must
    leave tools/list refused.
    """
    transport = make_transport(data_dir)
    transport.start()
    try:
        transport.send(jsonrpc.build_notification("notifications/initialized"))
        transport.send(jsonrpc.build_request(1, "tools/list"))

        response = jsonrpc.parse_message(transport.receive())
        assert isinstance(response, jsonrpc.ErrorResponse), response
        assert response.code == jsonrpc.INVALID_REQUEST
        assert "initialize" in response.message
    finally:
        transport.close()


def test_the_full_handshake_does_open_the_session(data_dir):
    """The same sequence with initialize first must succeed."""
    transport = make_transport(data_dir)
    transport.start()
    try:
        transport.send(
            jsonrpc.build_request(
                0,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "conformance", "version": "1.0"},
                },
            )
        )
        assert isinstance(jsonrpc.parse_message(transport.receive()), jsonrpc.SuccessResponse)

        transport.send(jsonrpc.build_notification("notifications/initialized"))
        transport.send(jsonrpc.build_request(1, "tools/list"))

        response = jsonrpc.parse_message(transport.receive())
        assert isinstance(response, jsonrpc.SuccessResponse), response
        assert len(response.result["tools"]) == 7
    finally:
        transport.close()


def test_ping_works_before_the_handshake(data_dir):
    """ping is explicitly exempt, so a client can check liveness first."""
    transport = make_transport(data_dir)
    transport.start()
    try:
        transport.send(jsonrpc.build_request(1, "ping"))
        response = jsonrpc.parse_message(transport.receive())
        assert isinstance(response, jsonrpc.SuccessResponse)
        assert response.result == {}
    finally:
        transport.close()


# --------------------------------------------------------------------------
# The specification document must match the implementation
# --------------------------------------------------------------------------


def test_spec_documents_every_tool():
    """SPEC.md is a deliverable; drift from the code makes it worthless."""
    from servers.netops import core
    from servers.netops.store import DATA_DIR

    spec = (DATA_DIR.parent / "SPEC.md").read_text(encoding="utf-8")
    for tool in core.TOOLS:
        assert f"`{tool['name']}`" in spec, f"{tool['name']} is missing from SPEC.md"


def test_spec_documents_every_error_code():
    from servers.netops.store import DATA_DIR

    spec = (DATA_DIR.parent / "SPEC.md").read_text(encoding="utf-8")
    for code in (-32700, -32600, -32601, -32602, -32603):
        assert str(code) in spec, f"{code} is missing from SPEC.md"


def test_spec_examples_are_valid_json():
    """Every fenced json block in SPEC.md must actually parse."""
    from servers.netops.store import DATA_DIR

    spec = (DATA_DIR.parent / "SPEC.md").read_text(encoding="utf-8")
    blocks = []
    inside = False
    current: list[str] = []
    for line in spec.splitlines():
        if line.strip() == "```json":
            inside, current = True, []
            continue
        if inside and line.strip() == "```":
            blocks.append("\n".join(current))
            inside = False
            continue
        if inside:
            current.append(line)

    assert blocks, "no json examples found in SPEC.md"
    for block in blocks:
        for line in block.splitlines():
            if not line.strip():
                continue
            # Multi-line blocks are single objects; single-line ones are NDJSON.
            break
        try:
            json.loads(block)
        except json.JSONDecodeError:
            # A block of several NDJSON lines: each line must parse on its own.
            for line in block.splitlines():
                if line.strip():
                    json.loads(line)
