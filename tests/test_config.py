"""F0 scaffolding tests: the package imports and the shipped config parses."""

from __future__ import annotations

import json

import pytest

from host.config import ConfigError, load_servers


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
