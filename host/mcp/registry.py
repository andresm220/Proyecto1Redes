"""Registry: several MCP servers behind one namespaced tool table.

Tool names are namespaced from the start, as `<server>__<tool>`, because two
servers may legitimately expose the same tool name. The separator is a double
underscore rather than a dot or a colon: Anthropic tool names must match
`^[a-zA-Z0-9_-]{1,128}$`, which admits `_` and `-` but neither `.` nor `:`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from host.config import ServerConfig
from host.mcp.client import MCPClient, ProtocolVersionError
from host.mcp.stdio_transport import StdioTransport
from host.mcp.transport import TransportError

NAMESPACE_SEPARATOR = "__"
ANTHROPIC_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


class ToolNotFoundError(KeyError):
    """No connected server exposes that tool."""


@dataclass(frozen=True)
class RegisteredTool:
    """One tool, tagged with the server that owns it."""

    server: str
    name: str
    definition: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        return f"{self.server}{NAMESPACE_SEPARATOR}{self.name}"

    @property
    def description(self) -> str:
        return self.definition.get("description", "")

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.definition.get("inputSchema", {"type": "object", "properties": {}})


class ServerRegistry:
    """Connects to every configured server and resolves qualified tool names."""

    def __init__(
        self,
        configs: dict[str, ServerConfig],
        on_message: Callable[[str, str, dict[str, Any]], None] | None = None,
        on_stderr: Callable[[str, str], None] | None = None,
    ) -> None:
        self.configs = configs
        self._on_message = on_message
        self._on_stderr = on_stderr
        self.clients: dict[str, MCPClient] = {}
        self.failures: dict[str, str] = {}
        self._tools: dict[str, RegisteredTool] = {}
        self._transports: dict[str, str] = {}

    # -- lifecycle ---------------------------------------------------------

    def connect_all(self) -> None:
        """Connect to every server. One bad server does not stop the others."""
        for name, config in self.configs.items():
            try:
                self._connect_one(name, config)
            except (TransportError, ProtocolVersionError, OSError) as exc:
                self.failures[name] = str(exc)

    def _connect_one(self, name: str, config: ServerConfig) -> None:
        transport = StdioTransport(
            command=config.command,
            args=config.args,
            env=config.env,
            name=name,
            on_stderr=self._on_stderr,
        )
        # Recorded before connecting, not after: the handshake is itself
        # traffic worth logging, and it happens inside connect().
        self._transports[name] = transport.kind
        client = MCPClient(transport, name=name, on_message=self._on_message)
        client.connect()
        self.clients[name] = client
        for definition in client.list_tools():
            tool_name = definition.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                continue
            tool = RegisteredTool(server=name, name=tool_name, definition=definition)
            if not ANTHROPIC_TOOL_NAME.match(tool.qualified_name):
                # Better to drop it than to have the LLM API reject the whole
                # request because of one malformed name.
                self._report_stderr(name, f"skipping tool with unusable name: {tool_name!r}")
                continue
            self._tools[tool.qualified_name] = tool

    def _report_stderr(self, server: str, text: str) -> None:
        if self._on_stderr is not None:
            self._on_stderr(server, text)

    def close_all(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()
        self._tools.clear()
        self._transports.clear()

    def transport_of(self, server: str) -> str:
        """Which transport carries this server's traffic, for the log."""
        return self._transports.get(server, "unknown")

    def __enter__(self) -> "ServerRegistry":
        self.connect_all()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close_all()

    # -- lookup ------------------------------------------------------------

    @property
    def tools(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def __iter__(self) -> Iterator[RegisteredTool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, qualified_name: str) -> RegisteredTool:
        try:
            return self._tools[qualified_name]
        except KeyError:
            known = ", ".join(sorted(self._tools)) or "none"
            raise ToolNotFoundError(f"unknown tool {qualified_name!r}; available: {known}") from None

    def call(self, qualified_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        tool = self.get(qualified_name)
        client = self.clients[tool.server]
        return client.call_tool(tool.name, arguments or {})
