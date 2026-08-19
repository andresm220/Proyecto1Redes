"""Interactive CLI for the MCP host.

Run with:  python -m host.main

Connects to every server declared in config/servers.json, completes the MCP
handshake, and exposes their tools. The agentic loop lands in F4; until then
`/call` invokes tools directly.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from host.config import ConfigError, HostConfig, load_config
from host.mcp import jsonrpc
from host.mcp.client import McpError
from host.mcp.registry import ServerRegistry, ToolNotFoundError
from host.mcp.transport import TransportError

console = Console()

BANNER = "uvg-mcp-host 0.1.0  -  type /help for commands, /quit to exit"

HELP_ROWS = [
    ("/servers", "List the configured MCP servers and their connection state"),
    ("/tools", "List every tool exposed by the connected servers"),
    ("/call <tool> <json>", "Invoke one tool directly, bypassing the LLM"),
    ("/verbose", "Toggle the JSON-RPC message trace"),
    ("/help", "Show this table"),
    ("/quit", "Close every server and exit"),
]


class Cli:
    """Holds the CLI's mutable state so the command handlers stay small."""

    def __init__(self, config: HostConfig) -> None:
        self.config = config
        self.verbose = True  # the protocol trace is a deliverable, so start on
        self.registry = ServerRegistry(
            config.servers,
            on_message=self.log_message,
            on_stderr=self.log_stderr,
        )

    # -- logging -----------------------------------------------------------

    def log_message(self, server: str, direction: str, message: dict[str, Any]) -> None:
        """Print every JSON-RPC message sent and received.

        Payloads are escaped and soft-wrapped: rich would otherwise read a
        bracketed substring as a style tag and swallow it, and a hard-wrapped
        trace is no longer valid JSON to copy out of.
        """
        if not self.verbose:
            return
        arrow = "->" if direction == "send" else "<-"
        colour = "cyan" if direction == "send" else "magenta"
        payload = escape(jsonrpc.encode(message))
        console.print(
            f"[dim]{arrow} {escape(server)}[/dim] [{colour}]{payload}[/{colour}]",
            highlight=False,
            soft_wrap=True,
        )

    def log_stderr(self, server: str, text: str) -> None:
        console.print(
            f"[dim yellow]{escape(f'[{server} stderr]')}[/dim yellow] {escape(text)}",
            highlight=False,
            soft_wrap=True,
        )

    # -- commands ----------------------------------------------------------

    def print_help(self) -> None:
        table = Table(title="Commands")
        table.add_column("Command", style="bold cyan", no_wrap=True)
        table.add_column("Description")
        for command, description in HELP_ROWS:
            table.add_row(command, description)
        console.print(table)

    def print_servers(self) -> None:
        table = Table(title="Servers")
        table.add_column("Name", style="bold cyan", no_wrap=True)
        table.add_column("Command")
        table.add_column("State")
        table.add_column("Server info")
        for name, config in self.config.servers.items():
            launch = " ".join([config.command, *config.args])
            client = self.registry.clients.get(name)
            if client is not None:
                info = client.server_info
                state = "[green]connected[/green]"
                detail = f"{info.get('name', '?')} {info.get('version', '?')}"
            else:
                state = "[red]failed[/red]"
                detail = escape(self.registry.failures.get(name, "not connected"))
            table.add_row(name, launch, state, detail)
        console.print(table)

    def print_tools(self) -> None:
        if not len(self.registry):
            console.print("[yellow]No tools available - no server is connected.[/yellow]")
            return
        table = Table(title=f"Tools ({len(self.registry)})")
        table.add_column("Qualified name", style="bold cyan", no_wrap=True)
        table.add_column("Required arguments")
        table.add_column("Description")
        for tool in sorted(self.registry, key=lambda t: t.qualified_name):
            required = ", ".join(tool.input_schema.get("required", [])) or "-"
            table.add_row(tool.qualified_name, escape(required), escape(tool.description))
        console.print(table)

    def call_tool(self, argument: str) -> None:
        parts = argument.split(maxsplit=1)
        name = parts[0]
        raw_arguments = parts[1].strip() if len(parts) > 1 else "{}"

        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            console.print(f"[red]Arguments are not valid JSON:[/red] {exc}")
            return
        if not isinstance(arguments, dict):
            console.print("[red]Arguments must be a JSON object.[/red]")
            return

        try:
            result = self.registry.call(name, arguments)
        except ToolNotFoundError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            return
        except McpError as exc:
            # A protocol-level failure: the exchange itself was rejected.
            console.print(f"[red]Protocol error {exc.code}:[/red] {escape(exc.message)}")
            return
        except TransportError as exc:
            console.print(f"[red]Transport error:[/red] {escape(str(exc))}")
            return

        self.print_tool_result(result)

    def print_tool_result(self, result: dict[str, Any]) -> None:
        # isError marks a domain failure inside a successful exchange.
        if result.get("isError"):
            console.print("[yellow]Tool reported an error:[/yellow]")
        for block in result.get("content", []):
            # Escaped: tool output is data, not markup. A ticket description
            # containing brackets must render verbatim.
            if block.get("type") == "text":
                console.print(escape(block.get("text", "")), highlight=False)
            else:
                console.print(escape(json.dumps(block, ensure_ascii=False, indent=2)))

    def handle_command(self, line: str) -> bool:
        parts = line.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1] if len(parts) > 1 else ""

        if command in ("/quit", "/exit"):
            return False
        if command == "/help":
            self.print_help()
        elif command == "/servers":
            self.print_servers()
        elif command == "/tools":
            self.print_tools()
        elif command == "/verbose":
            self.verbose = not self.verbose
            console.print(f"JSON-RPC trace {'on' if self.verbose else 'off'}.")
        elif command == "/call":
            if not argument:
                console.print("[red]Usage: /call <tool> <json-arguments>[/red]")
            else:
                self.call_tool(argument)
        else:
            console.print(f"[red]Unknown command: {command}[/red]  (try /help)")
        return True

    # -- loop --------------------------------------------------------------

    def run(self) -> int:
        console.print(f"[bold]{BANNER}[/bold]")
        self.registry.connect_all()

        connected = len(self.registry.clients)
        console.print(
            f"Connected to {connected}/{len(self.config.servers)} server(s), "
            f"{len(self.registry)} tool(s) available."
        )
        for name, reason in self.registry.failures.items():
            console.print(f"[red]{escape(name)}: {escape(reason)}[/red]")
        if not self.config.has_api_key:
            console.print(
                "[yellow]ANTHROPIC_API_KEY is not set - copy .env.example to .env "
                "before running the agentic loop (F4).[/yellow]"
            )

        while True:
            try:
                # Strip a leading BOM: piping commands in on Windows prepends one,
                # which would otherwise hide the '/' and misroute the first command.
                line = console.input("[bold green]> [/bold green]").lstrip("\ufeff").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not line:
                continue
            if line.startswith("/"):
                if not self.handle_command(line):
                    break
            else:
                console.print("[yellow]The agentic loop lands in F4. Use /call for now.[/yellow]")

        return 0


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        return 1

    cli = Cli(config)
    try:
        return cli.run()
    finally:
        cli.registry.close_all()
        console.print("bye")


if __name__ == "__main__":
    sys.exit(main())
