"""The full-screen application loop.

## The one design decision this file exists to make

A live layout and a blocking prompt cannot share a terminal. The usual
compromise is to tear the screen down to ask a question and rebuild it
afterwards, which makes the interface flash away at exactly the moment the
user is deciding what to do next.

Here the layout never comes down. `Live` owns the screen for the whole
session; keystrokes are read one at a time and drawn into the footer as part
of the same repaint as everything else. Asking a question is not a different
mode - it is just another frame.

That choice is what makes two heuristics hold at once. **Visibility of system
status**: while the model works, the header names the tool being called and
the log fills in underneath, rather than the screen being frozen or blank.
**User control**: the prompt, the shortcuts and the state of every server are
on screen while the user types, so nothing has to be remembered.

## What this loop does not do

It does not reimplement the commands. `Cli` already owns `/servers`, `/tools`,
`/call`, `/log` and the rest, and duplicating that table here would guarantee
the two copies drift. Instead the `Cli` writes into a captured console and the
loop folds whatever it printed into the transcript. One implementation, two
presentations.
"""

from __future__ import annotations

import io
from typing import Any

from rich.console import Console
from rich.live import Live

from host.logging.mcp_logger import LogEvent
from host.main import Cli
from host.mcp.client import PROTOCOL_VERSION
from host.ui import keyboard
from host.ui.dashboard import (
    DashboardModel,
    TurnState,
    build_layout,
    log_line,
    servers_from_registry,
    truncate,
)

# Eight frames a second is enough to feel immediate and cheap enough that a
# busy log does not spend the session repainting.
REFRESH_PER_SECOND = 8

# How much of one captured command's output goes into the transcript. A
# `tools/list` dump is thousands of characters and would bury the conversation.
MAX_CAPTURED_LINES = 40


class DashboardApp:
    """Runs the host behind a full-screen dashboard."""

    def __init__(self, cli: Cli) -> None:
        self.cli = cli
        # A console that renders into a buffer rather than onto the screen, so
        # the commands can print exactly as they always have.
        self.capture = Console(file=io.StringIO(), width=100, color_system=None)
        cli.console = self.capture

        self.model = DashboardModel(
            model=cli.config.llm.model,
            provider=cli.config.llm.provider,
            protocol_version=PROTOCOL_VERSION,
        )
        self.reader = keyboard.KeyReader()
        self.live: Live | None = None
        self.running = True

        # The logger feeds the log panel directly, so the panel is a view of
        # the same events the JSONL file receives - not a second recording
        # that could disagree with it.
        cli.logger._on_event = self.on_log_event

    # -- rendering ---------------------------------------------------------

    def refresh(self) -> None:
        if self.live is not None:
            self.live.update(build_layout(self.model))

    def on_log_event(self, event: LogEvent) -> None:
        """Called from the MCPClient reader threads, so it only appends."""
        self.model.log_lines.append(log_line(event))
        self.model.message_count += 1
        # Bounded: the panel shows a tail, and an unbounded list would grow
        # for the life of the session with nothing reading the older entries.
        if len(self.model.log_lines) > 200:
            del self.model.log_lines[:100]
        self.refresh()

    def on_agent_event(self, kind: str, payload: dict[str, Any]) -> None:
        turn = self.model.turn
        if kind == "model_request":
            turn.status = "thinking"
            turn.detail = ""
            turn.iteration = payload.get("iteration", 0)
            turn.max_iterations = self.cli.agent.max_iterations if self.cli.agent else 0
        elif kind == "tool_call":
            turn.status = "calling"
            turn.detail = payload.get("name", "")
            self.say("tool", f"calling {payload.get('name', '')}")
        elif kind == "tool_result" and payload.get("is_error"):
            self.say("error", f"{payload.get('name', 'tool')} reported an error")
        elif kind == "max_iterations":
            self.say("error", f"stopped at the {payload.get('limit')}-iteration cap")
        self.refresh()

    def say(self, role: str, text: str) -> None:
        self.model.transcript.append((role, text))
        # The conversation panel scrolls by dropping the top, which is what a
        # reader expects: the newest turn stays where the eye already is.
        if len(self.model.transcript) > 60:
            del self.model.transcript[:20]
        self.refresh()

    def drain_capture(self) -> str:
        """Take whatever the Cli printed since the last time we looked."""
        text = self.capture.file.getvalue()  # type: ignore[union-attr]
        self.capture.file = io.StringIO()  # type: ignore[union-attr]
        return text

    def show_captured(self) -> None:
        lines = [line.rstrip() for line in self.drain_capture().splitlines()]
        lines = [line for line in lines if line.strip()]
        if not lines:
            return
        if len(lines) > MAX_CAPTURED_LINES:
            hidden = len(lines) - MAX_CAPTURED_LINES
            lines = lines[:MAX_CAPTURED_LINES] + [f"... {hidden} more line(s)"]
        for line in lines:
            self.say("tool", line)

    # -- the turn ----------------------------------------------------------

    def submit(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self.say("user", line)

        if line.startswith("/"):
            if line.split()[0].lower() in ("/quit", "/exit"):
                self.running = False
                return
            self.cli.handle_command(line)
            self.show_captured()
            return

        self.model.turn = TurnState(status="thinking")
        self.refresh()
        try:
            self.cli.ask(line)
        finally:
            self.model.turn = TurnState()
        captured = self.drain_capture().strip()
        if captured:
            self.say("assistant", captured)
        self.refresh()

    # -- the loop ----------------------------------------------------------

    def run(self) -> int:
        self.model.servers = servers_from_registry(self.cli.config.servers, self.cli.registry)
        if self.cli.agent is not None:
            self.cli.agent.set_event_handler(self.on_agent_event)

        with Live(
            build_layout(self.model),
            console=Console(),
            screen=True,
            refresh_per_second=REFRESH_PER_SECOND,
            transient=False,
        ) as live:
            self.live = live
            while self.running:
                key = self.reader.read_key()

                if key == keyboard.ENTER:
                    line, self.model.input_buffer = self.model.input_buffer, ""
                    self.refresh()
                    self.submit(line)
                elif key == keyboard.TOGGLE_LOG:
                    self.model.show_log = not self.model.show_log
                    self.refresh()
                elif key == keyboard.TOGGLE_SERVERS:
                    # Re-read rather than toggle visibility: the useful thing
                    # mid-session is knowing whether a server has since died.
                    self.model.servers = servers_from_registry(
                        self.cli.config.servers, self.cli.registry
                    )
                    self.refresh()
                elif key in (keyboard.INTERRUPT, keyboard.EOF):
                    # Ctrl+C clears the line rather than killing the session;
                    # on an empty line it exits. Destructive only when the user
                    # has already confirmed there is nothing to lose.
                    if self.model.input_buffer:
                        self.model.input_buffer = ""
                        self.refresh()
                    else:
                        self.running = False
                else:
                    self.model.input_buffer = keyboard.apply(self.model.input_buffer, key)
                    self.refresh()

        self.live = None
        return 0


def run_dashboard(cli: Cli) -> int:
    """Entry point used by host.main when the dashboard is available."""
    return DashboardApp(cli).run()


def summarise(text: str, width: int = 88) -> str:
    """One readable line out of arbitrary tool output."""
    return truncate(text, width)
