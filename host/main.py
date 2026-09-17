"""Interactive CLI for the MCP host.

Run with:  python -m host.main

Connects to every server declared in config/servers.json, completes the MCP
handshake, exposes their tools to the model, and runs the agentic loop. Every
MCP message in either direction is written to a JSONL session log; `/log`
reads it back without leaving the chat.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from host.agent import Agent
from host.config import ConfigError, HostConfig, load_config
from host.logging.mcp_logger import DIRECTION_OUT, TYPE_ERROR, LogEvent, McpLogger
from host.llm.anthropic_client import AnthropicClient
from host.llm.base import LLMClient, LLMError
from host.llm.openai_compatible import OpenAICompatibleClient
from host.mcp import jsonrpc
from host.mcp.client import McpError
from host.mcp.registry import ServerRegistry, ToolNotFoundError
from host.mcp.transport import TransportError
from host.session import Session, build_system_prompt

def force_utf8_streams() -> None:
    """Make our own stdout and stderr UTF-8, whatever the console defaults to.

    The Windows console encodes as cp1252, and a model answer is not limited
    to that repertoire: several models write "300 Mbps" with a narrow no-break
    space (U+202F), which cp1252 cannot represent. rich raises
    UnicodeEncodeError on the write and the CLI dies mid-answer.

    StdioTransport already forces UTF-8 on every child server for the same
    reason. This is the other half of it, for the host's own streams.
    `errors="replace"` is deliberate: a character we cannot draw is worth a
    replacement glyph, never a crash.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):  # pragma: no cover - a stream that cannot
                pass  # be reconfigured is better than no output at all


force_utf8_streams()
console = Console()

BANNER = "uvg-mcp-host 0.1.0  -  type /help for commands, /quit to exit"

HELP_ROWS = [
    ("/servers", "List the configured MCP servers and their connection state"),
    ("/tools", "List every tool exposed by the connected servers"),
    ("/call <tool> <json>", "Invoke one tool directly, bypassing the LLM"),
    ("/log", "Where the session log is, and how much is in it"),
    ("/log tail <n>", "Replay the last n MCP messages (default 20)"),
    ("/verbose", "Toggle the live JSON-RPC message trace"),
    ("/history", "Show the conversation turn by turn"),
    ("/save <file>", "Write the conversation to a JSON file"),
    ("/reset", "Forget the conversation so far"),
    ("F2 / F3", "Collapse the log panel / refresh the server states (dashboard only)"),
    ("/help", "Show this table"),
    ("/quit", "Close every server and exit"),
]

DEFAULT_TAIL = 20

# The message palette, used by both the live trace and /log tail. Colour is
# semantic and never load-bearing on its own: the arrow carries the same
# information, so the trace survives a colour-blind reader and a printout.
COLOUR_OUT = "blue"  # client -> server
COLOUR_IN = "green"  # server -> client
COLOUR_ERROR = "red"


def event_colour(event: LogEvent) -> str:
    if event.type == TYPE_ERROR:
        return COLOUR_ERROR
    return COLOUR_OUT if event.direction == DIRECTION_OUT else COLOUR_IN


class Cli:
    """Holds the CLI's mutable state so the command handlers stay small."""

    def __init__(
        self,
        config: HostConfig,
        logger: McpLogger,
        verbose: bool = True,
        console: Console | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        # Injectable so the dashboard can capture what the commands print
        # instead of letting it scroll past the layout.
        self.console = console or globals()["console"]
        self.verbose = verbose  # the protocol trace is a deliverable, so start on
        self.registry = ServerRegistry(
            config.servers,
            on_message=self.log_message,
            on_stderr=self.log_stderr,
        )
        self.session: Session | None = None
        self.agent: Agent | None = None

    # -- logging -----------------------------------------------------------

    def log_message(self, server: str, direction: str, message: dict[str, Any]) -> None:
        """Record every JSON-RPC message, and echo it when the trace is on.

        The file log is written unconditionally: the console trace is a view,
        and turning a view off must not put holes in the evidence.

        Payloads are escaped and soft-wrapped for the console: rich would
        otherwise read a bracketed substring as a style tag and swallow it,
        and a hard-wrapped trace is no longer valid JSON to copy out of.
        """
        self.logger.record(
            server, direction, message, transport=self.registry.transport_of(server)
        )
        if not self.verbose:
            return
        arrow = "->" if direction == "send" else "<-"
        colour = COLOUR_ERROR if "error" in message else (
            COLOUR_OUT if direction == "send" else COLOUR_IN
        )
        payload = escape(jsonrpc.encode(message))
        self.console.print(
            f"[dim]{arrow} {escape(server)}[/dim] [{colour}]{payload}[/{colour}]",
            highlight=False,
            soft_wrap=True,
        )

    def log_stderr(self, server: str, text: str) -> None:
        self.console.print(
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
        self.console.print(table)

    def print_servers(self) -> None:
        table = Table(title="Servers")
        table.add_column("Name", style="bold cyan", no_wrap=True)
        table.add_column("Transport", no_wrap=True)
        table.add_column("Command or URL")
        table.add_column("State")
        table.add_column("Server info")
        for name, config in self.config.servers.items():
            launch = config.describe()
            client = self.registry.clients.get(name)
            if client is not None:
                info = client.server_info
                state = "[green]connected[/green]"
                detail = f"{info.get('name', '?')} {info.get('version', '?')}"
            else:
                state = "[red]failed[/red]"
                detail = escape(self.registry.failures.get(name, "not connected"))
            table.add_row(name, config.transport, launch, state, detail)
        self.console.print(table)

    def print_tools(self) -> None:
        if not len(self.registry):
            self.console.print("[yellow]No tools available - no server is connected.[/yellow]")
            return
        table = Table(title=f"Tools ({len(self.registry)})")
        table.add_column("Qualified name", style="bold cyan", no_wrap=True)
        table.add_column("Required arguments")
        table.add_column("Description")
        for tool in sorted(self.registry, key=lambda t: t.qualified_name):
            required = ", ".join(tool.input_schema.get("required", [])) or "-"
            table.add_row(tool.qualified_name, escape(required), escape(tool.description))
        self.console.print(table)

    def call_tool(self, argument: str) -> None:
        parts = argument.split(maxsplit=1)
        name = parts[0]
        raw_arguments = parts[1].strip() if len(parts) > 1 else "{}"

        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            self.console.print(f"[red]Arguments are not valid JSON:[/red] {exc}")
            return
        if not isinstance(arguments, dict):
            self.console.print("[red]Arguments must be a JSON object.[/red]")
            return

        try:
            result = self.registry.call(name, arguments)
        except ToolNotFoundError as exc:
            self.console.print(f"[red]{escape(str(exc))}[/red]")
            return
        except McpError as exc:
            # A protocol-level failure: the exchange itself was rejected.
            self.console.print(f"[red]Protocol error {exc.code}:[/red] {escape(exc.message)}")
            return
        except TransportError as exc:
            self.console.print(f"[red]Transport error:[/red] {escape(str(exc))}")
            return

        self.print_tool_result(result)

    def print_tool_result(self, result: dict[str, Any]) -> None:
        # isError marks a domain failure inside a successful exchange.
        if result.get("isError"):
            self.console.print("[yellow]Tool reported an error:[/yellow]")
        for block in result.get("content", []):
            # Escaped: tool output is data, not markup. A ticket description
            # containing brackets must render verbatim.
            if block.get("type") == "text":
                self.console.print(escape(block.get("text", "")), highlight=False)
            else:
                self.console.print(escape(json.dumps(block, ensure_ascii=False, indent=2)))

    # -- the session log ---------------------------------------------------

    def handle_log(self, argument: str) -> None:
        """`/log` reports where the evidence is; `/log tail n` replays it."""
        parts = argument.split()
        if not parts:
            self.print_log_info()
            return
        if parts[0] != "tail":
            self.console.print("[red]Usage: /log  |  /log tail <n>[/red]")
            return
        count = DEFAULT_TAIL
        if len(parts) > 1:
            try:
                count = int(parts[1])
            except ValueError:
                self.console.print(f"[red]Not a number: {escape(parts[1])}[/red]")
                return
        self.print_log_tail(count)

    def print_log_info(self) -> None:
        self.console.print(f"Session log: [cyan]{escape(str(self.logger.path))}[/cyan]")
        self.console.print(
            f"{len(self.logger)} message(s) buffered, live trace "
            f"{'on' if self.verbose else 'off'}. Try [bold]/log tail 20[/bold]."
        )

    def print_log_tail(self, count: int) -> None:
        events = self.logger.tail(count)
        if not events:
            self.console.print("[yellow]Nothing logged yet.[/yellow]")
            return
        table = Table(title=f"Last {len(events)} MCP message(s)")
        table.add_column("Time", style="dim", no_wrap=True)
        table.add_column("", no_wrap=True)  # the arrow: direction without colour
        table.add_column("Server", style="bold", no_wrap=True)
        table.add_column("Type", no_wrap=True)
        table.add_column("Method")
        table.add_column("Id", justify="right", no_wrap=True)
        table.add_column("Duration", justify="right", no_wrap=True)
        for event in events:
            colour = event_colour(event)
            duration = f"{event.duration_ms:.1f} ms" if event.duration_ms is not None else ""
            table.add_row(
                event.ts[11:23],  # the time of day; the date is in the filename
                f"[{colour}]{event.arrow}[/{colour}]",
                escape(event.server),
                f"[{colour}]{event.type}[/{colour}]",
                escape(event.label),
                "" if event.id is None else str(event.id),
                duration,
            )
        self.console.print(table)

    # -- conversation ------------------------------------------------------

    def print_history(self) -> None:
        if self.session is None or not len(self.session):
            self.console.print("[yellow]No conversation yet.[/yellow]")
            return
        table = Table(title=f"Conversation ({len(self.session)} turn(s))")
        table.add_column("#", justify="right", style="dim", no_wrap=True)
        table.add_column("Role", style="bold cyan", no_wrap=True)
        table.add_column("Content")
        for index, (role, summary) in enumerate(self.session.outline(), start=1):
            table.add_row(str(index), role, escape(summary))
        self.console.print(table)

    def save_history(self, argument: str) -> None:
        path_text = argument.strip().strip('"')
        if not path_text:
            self.console.print("[red]Usage: /save <file>[/red]")
            return
        if self.session is None or not len(self.session):
            self.console.print("[yellow]Nothing to save - the conversation is empty.[/yellow]")
            return
        path = Path(path_text)
        document = {
            "model": self.config.llm.model,
            "provider": self.config.llm.provider,
            "system_prompt": self.session.system_prompt,
            "messages": self.session.to_jsonable(),
            "mcp_log": str(self.logger.path),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            self.console.print(f"[red]Could not write {escape(str(path))}:[/red] {escape(str(exc))}")
            return
        self.console.print(f"Saved {len(self.session)} turn(s) to [cyan]{escape(str(path))}[/cyan].")

    # -- the agentic loop --------------------------------------------------

    def build_llm(self) -> LLMClient:
        """Pick the adapter for the configured provider.

        Everything except Anthropic speaks the OpenAI dialect, so one adapter
        covers Groq, OpenRouter and any local runtime.
        """
        llm = self.config.llm
        if llm.is_openai_compatible:
            return OpenAICompatibleClient(
                api_key=llm.api_key or "",
                model=llm.model,
                base_url=llm.base_url or "",
                on_retry=lambda delay, reason: self.console.print(
                    f"[yellow]  {escape(reason)}[/yellow]"
                ),
            )
        return AnthropicClient(api_key=llm.api_key or "", model=llm.model)

    def build_agent(self) -> None:
        """Wire the model to the connected servers. Needs a backend and a server."""
        if not self.config.llm.is_usable or not self.registry.clients:
            return
        instructions = {
            name: client.instructions for name, client in self.registry.clients.items()
        }
        self.session = Session(build_system_prompt(instructions))
        self.agent = Agent(
            llm=self.build_llm(),
            registry=self.registry,
            session=self.session,
            on_event=self.on_agent_event,
        )

    def on_agent_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "tool_call":
            arguments = json.dumps(payload["arguments"], ensure_ascii=False)
            self.console.print(
                f"[dim]  calling[/dim] [bold]{escape(payload['name'])}[/bold] "
                f"[dim]{escape(arguments)}[/dim]",
                highlight=False,
                soft_wrap=True,
            )
        elif kind == "tool_result" and payload["is_error"]:
            self.console.print(f"[yellow]  tool reported an error[/yellow]", highlight=False)
        elif kind == "max_iterations":
            self.console.print(
                f"[yellow]  stopped at the {payload['limit']}-iteration cap[/yellow]"
            )

    def ask(self, question: str) -> None:
        if self.agent is None:
            reason = self.config.llm.why_unusable()
            if reason:
                self.console.print(
                    f"[yellow]Assistant disabled: {escape(reason)}. Copy .env.example to .env "
                    "and configure a provider, or use /call to invoke tools directly.[/yellow]"
                )
            else:
                self.console.print("[yellow]No server is connected, so there are no tools.[/yellow]")
            return
        try:
            answer = self.agent.run_turn(question)
        except LLMError as exc:
            self.console.print(f"[red]{escape(str(exc))}[/red]")
            return
        except Exception as exc:  # noqa: BLE001 - one bad turn must not end the session
            self.console.print(f"[red]{type(exc).__name__}:[/red] {escape(str(exc))}")
            return
        if answer:
            self.console.print(escape(answer), highlight=False)

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
            self.console.print(f"JSON-RPC trace {'on' if self.verbose else 'off'}.")
        elif command == "/reset":
            if self.session is not None:
                self.session.clear()
            self.console.print("Conversation cleared.")
        elif command == "/call":
            if not argument:
                self.console.print("[red]Usage: /call <tool> <json-arguments>[/red]")
            else:
                self.call_tool(argument)
        elif command == "/log":
            self.handle_log(argument)
        elif command == "/history":
            self.print_history()
        elif command == "/save":
            self.save_history(argument)
        else:
            self.console.print(f"[red]Unknown command: {command}[/red]  (try /help)")
        return True

    # -- loop --------------------------------------------------------------

    def run(self) -> int:
        self.console.print(f"[bold]{BANNER}[/bold]")
        self.registry.connect_all()
        self.build_agent()

        connected = len(self.registry.clients)
        self.console.print(
            f"Connected to {connected}/{len(self.config.servers)} server(s), "
            f"{len(self.registry)} tool(s) available."
        )
        self.console.print(f"[dim]Logging every MCP message to {escape(str(self.logger.path))}[/dim]")
        for name, reason in self.registry.failures.items():
            self.console.print(f"[red]{escape(name)}: {escape(reason)}[/red]")
        llm = self.config.llm
        if self.agent is not None:
            self.console.print(
                f"Model [cyan]{escape(llm.model)}[/cyan] via "
                f"[cyan]{escape(llm.provider)}[/cyan] ready. "
                "Ask a question, or use /call to invoke a tool directly."
            )
        elif not llm.is_usable:
            self.console.print(
                f"[yellow]Assistant disabled: {escape(llm.why_unusable())}. "
                "See .env.example. /call works without it.[/yellow]"
            )

        while True:
            try:
                # Strip a leading BOM: piping commands in on Windows prepends one,
                # which would otherwise hide the '/' and misroute the first command.
                line = self.console.input("[bold green]> [/bold green]").lstrip("\ufeff").strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print()
                break
            if not line:
                continue
            if line.startswith("/"):
                if not self.handle_command(line):
                    break
            else:
                self.ask(line)

        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m host.main",
        description="MCP host: an agentic chatbot over hand-written JSON-RPC 2.0.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="server declarations to load (default: config/servers.json)",
    )
    parser.add_argument(
        "--log-dir",
        metavar="PATH",
        help="where to write the JSONL session log (default: logs/)",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="scrolling transcript instead of the full-screen dashboard",
    )
    trace = parser.add_mutually_exclusive_group()
    trace.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show the live JSON-RPC trace (the default)",
    )
    trace.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="start with the live trace off; the file log is written either way",
    )
    return parser


def use_dashboard(args: argparse.Namespace) -> bool:
    """Whether to run the full-screen dashboard.

    It needs a real terminal: raw key reading has nothing to put into raw mode
    when stdin is a pipe, which is how the CLI is driven by scripts and by the
    test suite. Falling back is not a degraded mode, it is the only mode that
    works there.
    """
    if getattr(args, "plain", False):
        return False
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(Path(args.config) if args.config else None)
    except ConfigError as exc:
        console.print(f"[red]Configuration error:[/red] {exc}")
        return 1

    try:
        logger = McpLogger(log_dir=Path(args.log_dir) if args.log_dir else None)
    except OSError as exc:
        console.print(f"[red]Could not open the session log:[/red] {exc}")
        return 1

    cli = Cli(config, logger, verbose=not args.quiet)
    try:
        if use_dashboard(args):
            from host.ui.app import run_dashboard

            cli.registry.connect_all()
            cli.build_agent()
            return run_dashboard(cli)
        return cli.run()
    finally:
        # Order matters: the servers have to stop tracing before the log
        # closes, or a daemon reader thread can trace into a closed file.
        cli.registry.close_all()
        logger.close()
        console.print(f"[dim]MCP log: {escape(str(logger.path))}[/dim]")
        console.print("bye")


if __name__ == "__main__":
    sys.exit(main())
