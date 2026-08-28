"""F0 scaffolding tests: the package imports and the shipped config parses."""

from __future__ import annotations

import json

import pytest

from host.config import TRANSPORT_HTTP, TRANSPORT_STDIO, ConfigError, load_servers


def test_shipped_config_declares_netops():
    servers = load_servers()
    assert "netops" in servers
    netops = servers["netops"]
    assert netops.command == "python"
    assert netops.args == ["-m", "servers.netops.stdio_server"]


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_servers(tmp_path / "nope.json")


def test_malformed_json_is_reported(tmp_path):
    path = tmp_path / "servers.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_servers(path)


def test_server_without_command_is_rejected(tmp_path):
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"mcpServers": {"broken": {}}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="needs a 'command'"):
        load_servers(path)


# --------------------------------------------------------------------------
# Transport declaration
# --------------------------------------------------------------------------


def write_config(tmp_path, entry):
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"mcpServers": {"s": entry}}), encoding="utf-8")
    return path


def test_transport_defaults_to_stdio(tmp_path):
    """Every server in this project started life as a subprocess, so a
    declaration that says nothing means stdio."""
    servers = load_servers(write_config(tmp_path, {"command": "python"}))
    assert servers["s"].transport == TRANSPORT_STDIO
    assert servers["s"].is_stdio and not servers["s"].is_http


def test_an_http_server_is_declared_with_a_url(tmp_path):
    servers = load_servers(
        write_config(tmp_path, {"transport": "http", "url": "https://x.run.app/mcp"})
    )
    server = servers["s"]
    assert server.transport == TRANSPORT_HTTP
    assert server.url == "https://x.run.app/mcp"
    assert server.is_http and not server.is_stdio


def test_an_http_server_needs_no_command(tmp_path):
    """There is no subprocess to launch, so requiring one would be nonsense."""
    servers = load_servers(
        write_config(tmp_path, {"transport": "http", "url": "http://localhost:8080/mcp"})
    )
    assert servers["s"].command == ""


def test_an_http_server_without_a_url_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="needs a 'url'"):
        load_servers(write_config(tmp_path, {"transport": "http"}))


def test_a_url_without_a_scheme_is_rejected(tmp_path):
    """A bare host would be posted to as a relative path and fail obscurely."""
    with pytest.raises(ConfigError, match="http:// or https://"):
        load_servers(write_config(tmp_path, {"transport": "http", "url": "x.run.app/mcp"}))


def test_an_unknown_transport_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="expected one of"):
        load_servers(write_config(tmp_path, {"transport": "carrier-pigeon", "url": "http://x"}))


def test_describe_names_where_the_server_lives(tmp_path):
    stdio = load_servers(
        write_config(tmp_path, {"command": "npx", "args": ["-y", "pkg"]})
    )["s"]
    assert stdio.describe() == "npx -y pkg"

    http = load_servers(
        write_config(tmp_path, {"transport": "http", "url": "https://x/mcp"})
    )["s"]
    assert http.describe() == "https://x/mcp"


def test_requirements_has_no_mcp_sdk():
    """The assignment forbids every MCP SDK; guard it with a test."""
    from host.config import PROJECT_ROOT

    text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    packages = [
        line.split("#")[0].strip().lower()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    forbidden = {"mcp", "fastmcp", "modelcontextprotocol"}
    assert not (forbidden & set(packages)), f"MCP SDK found in requirements: {packages}"
