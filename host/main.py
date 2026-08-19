"""Interactive CLI for the MCP host.

Run with:  python -m host.main

Phase F0 wires up configuration and the command loop. Server connections land
in F2, real tool calls in F3, and the agentic loop in F4; until then the
corresponding commands report what is still missing instead of failing.
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.table import Table

from host.config import ConfigError, HostConfig, load_config

console = Console()

BANNER = "uvg-mcp-host 0.1.0  -  type /help for commands, /quit to exit"

HELP_ROWS = [
    ("/servers", "List the configured MCP servers and their connection state"),
    ("/tools", "List every tool exposed by the connected servers"),
    ("/call <tool> <json>", "Invoke one tool directly, bypassing the LLM"),
    ("/help", "Show this table"),
    ("/quit", "Close every server and exit"),
]


def print_help() -> None:
    table = Table(title="Commands", show_lines=False)
    table.add_column("Command", style="bold cyan", no_wrap=True)
    table.add_column("Description")
    for command, description in HELP_ROWS:
        table.add_row(command, description)
    console.print(table)


def print_servers(config: HostConfig) -> None:
    table = Table(title="Configured servers")
    table.add_column("Name", style="bold cyan", no_wrap=True)
    table.add_column("Command")
    table.add_column("State")
    for server in config.servers.values():
        launch = " ".join([server.command, *server.args])
        table.add_row(server.name, launch, "not connected (F2)")
    console.print(table)


def handle_command(line: str, config: HostConfig) -> bool:
    """Run one slash command. Returns False when the loop should stop."""
    parts = line.split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1] if len(parts) > 1 else ""

    if command in ("/quit", "/exit"):
        return False
    if command == "/help":
        print_help()
    elif command == "/servers":
        print_servers(config)
    elif command == "/tools":
        console.print("[yellow]No servers connected yet - tool discovery lands in F2.[/yellow]")
    elif command == "/call":
        if not argument:
            console.print("[red]Usage: /call <tool> <json-arguments>[/red]")
        else:
            console.print("[yellow]Direct tool calls land in F3.[/yellow]")
    else:
        console.print(f"[red]Unknown command: {command}[/red]  (try /help)")
    return True


def repl(config: HostConfig) -> int:
    console.print(f"[bold]{BANNER}[/bold]")
    console.print(
        f"Loaded {len(config.servers)} server(s) from config; model [cyan]{config.model}[/cyan]."
    )
    if not config.has_api_key:
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
            if not handle_command(line, config):
                break
        else:
            console.print("[yellow]The agentic loop lands in F4. Use /call for now.[/yellow]")

    console.print("bye")
    return 0


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        return 1
    return repl(config)


if __name__ == "__main__":
    sys.exit(main())
