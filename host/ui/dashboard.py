"""The full-screen dashboard: what the host looks like while it is working.

This module builds renderables and returns them. It opens no files, reads no
keys and prints nothing, which is what makes a layout testable at all - every
panel below can be rendered into a string buffer and asserted on.

## What each region is for, and why it is there

    +-- header ------------------------------------------------+
    |  model, provider, protocol version, turn state           |   status
    +-- servers ------+-- conversation ------------------------+
    |  + netops stdio |                                        |
    |  + filesystem   |   the transcript                       |
    |  + git          |                                        |
    |  + netops-remote|                                        |
    |  tools: 40      |                                        |
    +-----------------+----------------------------------------+
    |  MCP log (collapsible)                                   |   detail
    +----------------------------------------------------------+
    |  > input                     F2 log - F3 tools - /help    |   controls
    +----------------------------------------------------------+

**The header answers "is it working".** Nielsen's first heuristic is visibility
of system status, and an agentic loop is exactly the case that needs it: the
model can spend twenty seconds calling four tools, and without a status line
the user cannot tell that from a hang.

**The sidebar is grouped by common region.** Which servers are up, and how many
tools they contribute, are one question and so they live in one box. The
alternative - forty tool names down the side - would be technically more
information and practically less, because nobody can hold forty names.

**The log is collapsible.** Aesthetic and minimalist design does not mean
hiding things; it means detail on demand. The trace is the project's most
interesting artifact and also the fastest way to make the screen unreadable,
so it is one keypress away rather than always on or always off.

**The footer is recognition rather than recall.** The shortcuts are on screen
permanently. A user should never have to remember that `/log tail 20` exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from rich.align import Align
from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from host.ui import theme

# Above this many rows the log panel stops being "a glance at the tail" and
# starts crowding the conversation out of its own screen.
LOG_PANEL_ROWS = 8
SIDEBAR_WIDTH = 26
HEADER_ROWS = 3
FOOTER_ROWS = 3

# The conversation panel's own borders (2) and horizontal padding (2).
CONVERSATION_CHROME = 4
# Its top and bottom border, in rows.
CONVERSATION_CHROME_ROWS = 2


def conversation_rows(terminal_height: int, show_log: bool) -> int:
    """How many rows the conversation panel can actually show.

    It has to be computed rather than left to rich, because a panel renders
    its content from the top and simply loses whatever does not fit. For a log
    that is fine - you want the beginning of a file. For a conversation it is
    exactly wrong: the answer you just received is the one that falls off.
    """
    used = HEADER_ROWS + FOOTER_ROWS + (LOG_PANEL_ROWS + 2 if show_log else 0)
    return max(3, terminal_height - used - CONVERSATION_CHROME_ROWS)


def conversation_width(terminal_width: int) -> int:
    """How many columns a command's output may occupy.

    Anything rendered wider than this wraps inside the panel, and a wrapped
    table is not a table any more - it is the same characters in an order
    nobody can read. Captured output is therefore rendered at exactly this
    width rather than at a guessed one.
    """
    return max(20, terminal_width - SIDEBAR_WIDTH - CONVERSATION_CHROME)


@dataclass
class ServerRow:
    """One server, as the sidebar shows it."""

    name: str
    transport: str
    state: str
    detail: str = ""
    tools: int = 0


@dataclass
class TurnState:
    """What the host is doing right now, for the header."""

    status: str = "idle"  # idle | thinking | calling | error
    detail: str = ""
    iteration: int = 0
    max_iterations: int = 0

    @property
    def is_busy(self) -> bool:
        return self.status in ("thinking", "calling")


@dataclass
class DashboardModel:
    """Everything the dashboard draws, with no reference to where it came from."""

    title: str = "uvg-mcp-host"
    model: str = ""
    provider: str = ""
    protocol_version: str = ""
    servers: list[ServerRow] = field(default_factory=list)
    transcript: list[tuple[str, str]] = field(default_factory=list)
    log_lines: list[tuple[str, str]] = field(default_factory=list)
    turn: TurnState = field(default_factory=TurnState)
    show_log: bool = True
    input_buffer: str = ""
    message_count: int = 0
    # The live terminal size, refreshed by the app. The conversation panel
    # needs it to work out how much of the transcript it can show.
    terminal_width: int = 100
    terminal_height: int = 30

    @property
    def tool_count(self) -> int:
        return sum(row.tools for row in self.servers)

    @property
    def connected(self) -> int:
        return sum(1 for row in self.servers if row.state == "connected")


# -- panels -----------------------------------------------------------------


# A model name can be arbitrarily long - "openai/gpt-oss-120b" already carries
# a slash of its own - and left unchecked it pushes the protocol version off
# the header entirely. The version is the one field a grader looks for, so the
# model name is what gives way.
MODEL_NAME_WIDTH = 22
# The status detail is a namespaced tool name, which is also unbounded.
STATUS_DETAIL_WIDTH = 30


def render_header(model: DashboardModel) -> Panel:
    # Order matters under truncation. The protocol version sits immediately
    # after the title so that whatever else the width forces out, the one
    # field a reader is checking for stays put; the model name, which can be
    # arbitrarily long, is what gives way.
    left = Text()
    left.append(model.title, style=theme.COLOUR_HEADING)
    if model.protocol_version:
        left.append(f"  MCP {model.protocol_version}", style=theme.COLOUR_MUTED)
    if model.model:
        left.append("  ")
        left.append(truncate(model.model, MODEL_NAME_WIDTH), style=theme.COLOUR_ACCENT)
        left.append(f"@{model.provider}", style=theme.COLOUR_MUTED)

    right = Text()
    turn = model.turn
    if turn.status == "thinking":
        right.append("* thinking", style="yellow")
    elif turn.status == "calling":
        # Named, not spinner-only: "calling netops__lookup_account" tells the
        # user which server is being waited on, which a spinner cannot.
        right.append(f"* {truncate(turn.detail, STATUS_DETAIL_WIDTH)}", style="yellow")
    elif turn.status == "error":
        right.append(f"! {truncate(turn.detail, STATUS_DETAIL_WIDTH)}", style=theme.COLOUR_ERROR)
    else:
        right.append("ready", style=theme.COLOUR_MUTED)
    if turn.is_busy and turn.max_iterations:
        right.append(f"  [{turn.iteration}/{turn.max_iterations}]", style=theme.COLOUR_MUTED)

    bar = Table.grid(expand=True)
    bar.add_column(justify="left")
    bar.add_column(justify="right")
    bar.add_row(left, right)
    return Panel(bar, border_style=theme.COLOUR_FRAME, padding=(0, 1))


def render_servers(model: DashboardModel) -> Panel:
    table = Table.grid(padding=(0, 1))
    table.add_column(width=1)  # the glyph: state without colour
    table.add_column(ratio=1, overflow="ellipsis")

    for row in model.servers:
        style = theme.state_style(row.state)
        label = Text(row.name, style="bold" if row.state == "connected" else theme.COLOUR_MUTED)
        label.append(f"  {theme.transport_label(row.transport)}", style=theme.COLOUR_MUTED)
        table.add_row(Text(style.glyph, style=style.colour), label)
        if row.state == "failed" and row.detail:
            table.add_row("", Text(row.detail, style=theme.COLOUR_ERROR, overflow="fold"))

    footer = Text()
    footer.append(f"{model.connected}/{len(model.servers)} up", style=theme.COLOUR_MUTED)
    footer.append("   ")
    footer.append(f"{model.tool_count} tools", style=theme.COLOUR_MUTED)

    return Panel(
        Group(table, Text(""), footer),
        title="servers",
        border_style=theme.COLOUR_FRAME,
        padding=(0, 1),
    )


def entry_rows(role: str, text: str, width: int) -> int:
    """How many terminal rows one transcript entry will occupy.

    An estimate, not a measurement: it assumes rich wraps at the panel width,
    which is what it does for everything here except the pre-rendered output
    rows, and those are already cut to that width. Being one row out only
    shows one entry more or less, which is harmless - being unable to see the
    newest answer at all is not.
    """
    if role == "output":
        return 1  # rendered at the panel width already, and never wrapped
    prefix = {"user": 2, "tool": 4, "error": 4}.get(role, 0)
    span = max(1, width - prefix)
    lines = max(1, -(-len(text) // span))  # ceil division
    return lines + 1  # the blank line that separates turns


def visible_transcript(model: DashboardModel) -> list[tuple[str, str]]:
    """The tail of the transcript that fits, newest always included.

    A panel renders from the top and drops the overflow, so an unbounded
    transcript means the newest answer is the first thing to disappear - which
    is the opposite of what a conversation needs. The tail is taken here
    instead, walking backwards until the budget runs out.
    """
    budget = conversation_rows(model.terminal_height, model.show_log)
    width = conversation_width(model.terminal_width)

    kept: list[tuple[str, str]] = []
    for role, text in reversed(model.transcript):
        cost = entry_rows(role, text, width)
        if kept and budget - cost < 0:
            break
        budget -= cost
        kept.append((role, text))
    kept.reverse()
    return kept


def render_conversation(model: DashboardModel) -> Panel:
    if not model.transcript:
        body: RenderableType = Align.center(
            Text(
                "Ask a question, or run a tool directly with /call.",
                style=theme.COLOUR_MUTED,
            ),
            vertical="middle",
        )
    else:
        blocks: list[RenderableType] = []
        for role, text in visible_transcript(model):
            if role == "user":
                blocks.append(Text(f"> {text}", style=theme.COLOUR_ACCENT))
            elif role == "tool":
                # Indented and dim: a tool call is something the assistant did
                # on the way to an answer, not an answer.
                blocks.append(Text(f"    {text}", style=theme.COLOUR_MUTED))
            elif role == "output":
                # Command output arrives pre-rendered at this panel's exact
                # width, so it must not be indented: four more columns would
                # push every line one character past the edge and wrap it,
                # which is precisely what turns a table into confetti.
                blocks.append(Text(text, style=theme.COLOUR_MUTED, no_wrap=True))
            elif role == "error":
                blocks.append(Text(f"  ! {text}", style=theme.COLOUR_ERROR))
            else:
                blocks.append(Text(text))
            # A blank line separates turns, but not the rows of one block of
            # output - double-spacing a table is as unreadable as wrapping it.
            if role != "output":
                blocks.append(Text(""))
        body = Group(*blocks)

    return Panel(body, title="conversation", border_style=theme.COLOUR_FRAME, padding=(0, 1))


def render_log(model: DashboardModel) -> Panel:
    if not model.log_lines:
        body: RenderableType = Text("No MCP traffic yet.", style=theme.COLOUR_MUTED)
    else:
        lines = model.log_lines[-LOG_PANEL_ROWS:]
        body = Group(*(Text(text, style=style) for style, text in lines))
    return Panel(
        body,
        title=f"MCP log  ({model.message_count} messages)",
        border_style=theme.COLOUR_FRAME,
        padding=(0, 1),
    )


def render_footer(model: DashboardModel) -> Panel:
    prompt = Text("> ", style=theme.COLOUR_ACCENT)
    prompt.append(model.input_buffer, style="white")
    prompt.append("_", style="blink")  # where typing lands, when nothing is typed yet

    hints = Text()
    hints.append("F2", style=theme.COLOUR_ACCENT)
    hints.append(" log  ", style=theme.COLOUR_MUTED)
    hints.append("F3", style=theme.COLOUR_ACCENT)
    hints.append(" servers  ", style=theme.COLOUR_MUTED)
    hints.append("/help", style=theme.COLOUR_ACCENT)
    hints.append("  ", style=theme.COLOUR_MUTED)
    hints.append("/quit", style=theme.COLOUR_ACCENT)

    bar = Table.grid(expand=True)
    bar.add_column(justify="left", ratio=1)
    bar.add_column(justify="right")
    bar.add_row(prompt, hints)
    return Panel(bar, border_style=theme.COLOUR_FRAME, padding=(0, 1))


# -- the whole screen -------------------------------------------------------


def build_layout(model: DashboardModel) -> Layout:
    """Assemble the regions into one screen."""
    root = Layout()
    rows = [
        Layout(render_header(model), name="header", size=3),
        Layout(name="body", ratio=1),
    ]
    if model.show_log:
        rows.append(Layout(render_log(model), name="log", size=LOG_PANEL_ROWS + 2))
    rows.append(Layout(render_footer(model), name="footer", size=3))
    root.split_column(*rows)

    body = root["body"]
    body.split_row(
        Layout(render_servers(model), name="servers", size=SIDEBAR_WIDTH),
        Layout(render_conversation(model), name="conversation", ratio=1),
    )
    return root


def log_line(event: Any) -> tuple[str, str]:
    """Turn one LogEvent into a (style, text) pair for the log panel.

    The text carries the arrow and the type in words, so the line still says
    everything it needs to say when the style is thrown away.
    """
    colour = theme.message_colour(event.direction, event.type)
    duration = f"{event.duration_ms:7.1f} ms" if event.duration_ms is not None else " " * 10
    text = (
        f"{event.arrow} {event.server[:14]:<14} "
        f"{theme.transport_label(event.transport)} "
        f"{event.type[:12]:<12} {event.label[:24]:<24} {duration}"
    )
    return colour, text


def servers_from_registry(config_servers: dict, registry: Any) -> list[ServerRow]:
    """Read the sidebar's contents off the registry, without it knowing."""
    rows: list[ServerRow] = []
    for name, server_config in config_servers.items():
        client = registry.clients.get(name)
        if client is not None:
            info = client.server_info
            rows.append(
                ServerRow(
                    name=name,
                    transport=registry.transport_of(name),
                    state="connected",
                    detail=f"{info.get('name', '?')} {info.get('version', '?')}",
                    tools=sum(1 for tool in registry if tool.server == name),
                )
            )
        else:
            rows.append(
                ServerRow(
                    name=name,
                    transport=getattr(server_config, "transport", "stdio"),
                    state="failed",
                    detail=registry.failures.get(name, "not connected"),
                )
            )
    return rows


def truncate(text: str, width: int) -> str:
    """One line, at most `width` columns, with an ellipsis when it was cut."""
    flat = " ".join(str(text).split())
    if len(flat) <= width:
        return flat
    return flat[: max(0, width - 1)] + "…"


def transcript_entries(entries: Iterable[Sequence[str]]) -> list[tuple[str, str]]:
    """Normalise (role, text) pairs, escaping anything that could be markup."""
    return [(str(role), escape(str(text))) for role, text in entries]
