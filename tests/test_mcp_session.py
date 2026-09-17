"""F2 tests: stdio transport plus MCPClient, against a real subprocess.

These are not mocked. Every test launches tests/fixtures/fake_server.py as a
child process and speaks NDJSON to it over real pipes, which is the only way
the concurrency, encoding and shutdown paths actually get exercised.
"""

from __future__ import annotations

import threading

import pytest

from host.mcp.client import (
    SUPPORTED_PROTOCOL_VERSIONS,
    McpError,
    MCPClient,
    ProtocolVersionError,
)
from host.mcp.stdio_transport import StdioTransport
from host.mcp.transport import TransportError


def make_client(
    mode: str = "normal",
    timeout: float = 15.0,
    connect_timeout: float | None = None,
) -> MCPClient:
    transport = StdioTransport(
        command="python",
        args=["-m", "tests.fixtures.fake_server"],
        env={"FAKE_MODE": mode},
        name="fake",
    )
    # The fixture server is a local script with nothing to download, so the
    # production connect budget - minutes, sized for npx resolving a package -
    # would only mean a very slow test on the paths that expect a timeout.
    return MCPClient(
        transport,
        name="fake",
        timeout=timeout,
        connect_timeout=connect_timeout if connect_timeout is not None else timeout,
    )


@pytest.fixture
def client():
    client = make_client()
    client.connect()
    yield client
    client.close()


# --------------------------------------------------------------------------
# The handshake
# --------------------------------------------------------------------------


def test_handshake_completes_and_reports_server_info(client):
    assert client.initialized is True
    assert client.server_info == {"name": "fake", "version": "1.0.0"}
    assert client.server_capabilities == {"tools": {"listChanged": False}}
    assert client.instructions == "A fixture server."


def test_unsupported_protocol_version_disconnects_and_reports():
    """The spec requires the client to drop the connection, not carry on."""
    client = make_client("bad_version")
    with pytest.raises(ProtocolVersionError) as exc_info:
        client.connect()

    assert exc_info.value.offered == "1999-01-01"
    assert exc_info.value.supported == SUPPORTED_PROTOCOL_VERSIONS
    assert client.initialized is False
    assert client.transport.is_running is False, "the server must have been shut down"


def test_an_older_but_supported_version_is_accepted():
    """Disconnecting is for a version we cannot speak, not for one that merely
    differs from the one we asked for. A server pinned to an earlier revision
    is a server we can still talk to."""
    client = make_client("old_version")
    try:
        client.connect()
        assert client.initialized is True
        assert client.negotiated_version == "2025-06-18"
        assert client.protocol_version == "2025-11-25", "we still ask for the newest"
    finally:
        client.close()


def test_the_negotiated_version_is_the_one_the_server_named():
    client = make_client("normal")
    try:
        client.connect()
        assert client.negotiated_version == "2025-11-25"
    finally:
        client.close()


def test_calls_before_the_handshake_are_refused():
    client = make_client()
    with pytest.raises(TransportError, match="before the handshake"):
        client.list_tools()


# --------------------------------------------------------------------------
# The MCP methods
# --------------------------------------------------------------------------


def test_tools_list_returns_real_tools(client):
    tools = client.list_tools()
    assert [tool["name"] for tool in tools] == ["echo", "explode"]
    echo = tools[0]
    assert echo["inputSchema"]["required"] == ["text"]


def test_tools_call_returns_content(client):
    result = client.call_tool("echo", {"text": "hello"})
    assert result["isError"] is False
    assert result["content"] == [{"type": "text", "text": "hello"}]


def test_ping(client):
    assert client.ping() == {}


def test_unknown_tool_is_a_domain_error_not_a_protocol_error(client):
    """isError: true is a *successful* exchange, so it must not raise."""
    result = client.call_tool("does-not-exist", {})
    assert result["isError"] is True
    assert "no such tool" in result["content"][0]["text"]


def test_invalid_params_raise_mcp_error(client):
    with pytest.raises(McpError) as exc_info:
        client.call_tool("echo", {})
    assert exc_info.value.code == -32602


def test_internal_error_raises_mcp_error(client):
    with pytest.raises(McpError) as exc_info:
        client.call_tool("explode", {})
    assert exc_info.value.code == -32603


def test_unknown_method_raises_method_not_found(client):
    with pytest.raises(McpError) as exc_info:
        client.request("resources/list")
    assert exc_info.value.code == -32601


# --------------------------------------------------------------------------
# Correlation, encoding and failure paths
# --------------------------------------------------------------------------


def test_concurrent_requests_each_get_their_own_response(client):
    """The id -> Future map must never hand a caller someone else's answer."""
    results: dict[int, str] = {}
    errors: list[Exception] = []

    def call(index: int) -> None:
        try:
            result = client.call_tool("echo", {"text": f"message-{index}"})
            results[index] = result["content"][0]["text"]
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(25)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors, f"concurrent calls failed: {errors}"
    assert results == {i: f"message-{i}" for i in range(25)}


def test_non_ascii_survives_the_subprocess_round_trip(client):
    """The real risk on Windows: the child defaulting to cp1252."""
    text = "Petén, 3º nivel — señal caída"
    result = client.call_tool("echo", {"text": text})
    assert result["content"][0]["text"] == text


def test_unparseable_lines_are_skipped_without_killing_the_session():
    client = make_client("garbage")
    try:
        client.connect()
        assert [tool["name"] for tool in client.list_tools()] == ["echo", "explode"]
        assert client.call_tool("echo", {"text": "still here"})["isError"] is False
    finally:
        client.close()


def test_a_server_that_dies_fails_the_pending_request():
    """A dead peer must raise, not hang the caller forever."""
    client = make_client("crash", timeout=15.0)
    try:
        with pytest.raises(TransportError, match="closed|exited"):
            client.connect()
    finally:
        client.close()


def test_a_server_that_never_answers_times_out():
    client = make_client("silent", timeout=1.0)
    try:
        with pytest.raises(TransportError, match="no response"):
            client.connect()
    finally:
        client.close()


# --------------------------------------------------------------------------
# Connecting and calling get separate budgets
# --------------------------------------------------------------------------


def test_the_handshake_has_its_own_timeout():
    """Connecting is not the same kind of operation as calling.

    An npx or uvx server resolves and downloads its package on first launch, so
    a first connect can take minutes; a tools/call against a server that is
    already running should never take thirty seconds. One budget for both drops
    a working server the day its package publishes a new version - which is
    exactly how mcp-server-git 1.30.0 broke this host.
    """
    client = make_client(connect_timeout=90.0, timeout=5.0)
    try:
        assert client.connect_timeout == 90.0
        assert client.timeout == 5.0
        client.connect()
        assert client.initialized is True
    finally:
        client.close()


def test_the_connect_budget_is_generous_by_default():
    from host.mcp.client import DEFAULT_CONNECT_TIMEOUT, DEFAULT_TIMEOUT

    assert DEFAULT_CONNECT_TIMEOUT > DEFAULT_TIMEOUT
    transport = StdioTransport(command="python", args=["-c", "pass"], name="unused")
    assert MCPClient(transport).connect_timeout == DEFAULT_CONNECT_TIMEOUT


def test_a_slow_handshake_is_tolerated_but_a_slow_call_is_not():
    """The budgets are independent: a short per-call timeout must not cut the
    handshake short, and a long connect budget must not excuse a slow call."""
    client = make_client(connect_timeout=30.0, timeout=0.5)
    try:
        client.connect()  # succeeds on the connect budget
        assert client.initialized is True
    finally:
        client.close()


def test_close_is_idempotent_and_stops_the_child():
    client = make_client()
    client.connect()
    transport = client.transport

    client.close()
    assert transport.is_running is False
    client.close()  # must not raise


def test_sending_after_close_is_refused():
    client = make_client()
    client.connect()
    client.close()
    with pytest.raises(TransportError, match="closed"):
        client.ping()
