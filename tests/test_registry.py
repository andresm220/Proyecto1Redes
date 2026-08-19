"""F2 tests: the registry that fronts several servers behind namespaced names."""

from __future__ import annotations

import pytest

from host.config import ServerConfig
from host.mcp.registry import NAMESPACE_SEPARATOR, ServerRegistry, ToolNotFoundError


def fake_config(name: str, mode: str = "normal") -> ServerConfig:
    return ServerConfig(
        name=name,
        command="python",
        args=["-m", "tests.fixtures.fake_server"],
        env={"FAKE_MODE": mode},
    )


@pytest.fixture
def registry():
    registry = ServerRegistry({"alpha": fake_config("alpha")})
    registry.connect_all()
    yield registry
    registry.close_all()


def test_tools_are_namespaced_by_server(registry):
    assert sorted(tool.qualified_name for tool in registry) == ["alpha__echo", "alpha__explode"]


def test_namespaced_names_are_valid_anthropic_tool_names(registry):
    """Anthropic requires ^[a-zA-Z0-9_-]{1,128}$, so '.' and ':' are unusable."""
    from host.mcp.registry import ANTHROPIC_TOOL_NAME

    assert NAMESPACE_SEPARATOR == "__"
    for tool in registry:
        assert ANTHROPIC_TOOL_NAME.match(tool.qualified_name)


def test_call_routes_to_the_owning_server(registry):
    result = registry.call("alpha__echo", {"text": "routed"})
    assert result["content"][0]["text"] == "routed"


def test_unknown_tool_lists_what_is_available(registry):
    with pytest.raises(ToolNotFoundError, match="alpha__echo"):
        registry.call("alpha__nope", {})


def test_two_servers_may_expose_the_same_tool_name():
    """The whole point of namespacing: no collision between servers."""
    registry = ServerRegistry({"alpha": fake_config("alpha"), "beta": fake_config("beta")})
    try:
        registry.connect_all()
        names = sorted(tool.qualified_name for tool in registry)
        assert names == ["alpha__echo", "alpha__explode", "beta__echo", "beta__explode"]
        assert registry.call("beta__echo", {"text": "from beta"})["content"][0]["text"] == "from beta"
    finally:
        registry.close_all()


def test_one_failing_server_does_not_stop_the_others():
    registry = ServerRegistry(
        {"good": fake_config("good"), "broken": fake_config("broken", mode="bad_version")}
    )
    try:
        registry.connect_all()
        assert "good" in registry.clients
        assert "broken" not in registry.clients
        assert "protocol version" in registry.failures["broken"]
        assert registry.call("good__echo", {"text": "ok"})["isError"] is False
    finally:
        registry.close_all()


def test_tool_metadata_is_exposed_for_schema_translation(registry):
    tool = registry.get("alpha__echo")
    assert tool.server == "alpha"
    assert tool.name == "echo"
    assert tool.description == "Return the text it was given."
    assert tool.input_schema["required"] == ["text"]
