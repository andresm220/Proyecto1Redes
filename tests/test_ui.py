"""F9 tests: the theme and the dashboard.

The dashboard builds renderables and performs no I/O, which is what makes it
testable: every panel can be rendered into a string buffer and asserted on.

Several tests here exist to pin down accessibility rather than appearance. A
palette that quietly starts relying on colour alone would still look fine to
whoever wrote it and would be unreadable to a colour-blind reader, in a printed
report, or on a washed-out projector - so the redundancy is a property worth
failing a build over.
"""

from __future__ import annotations

import io
import sys

import pytest
from rich.console import Console

from host.logging.mcp_logger import LogEvent
from host.ui import keyboard, theme
from host.ui.dashboard import (
    DashboardModel,
    ServerRow,
    TurnState,
    build_layout,
    log_line,
    render_conversation,
    render_footer,
    render_header,
    render_log,
    render_servers,
    truncate,
)


def draw(renderable, width: int = 100, height: int = 40) -> str:
    """Render to plain text, the way a monochrome terminal would show it."""
    buffer = io.StringIO()
    console = Console(file=buffer, width=width, height=height, color_system=None, legacy_windows=False)
    console.print(renderable)
    return buffer.getvalue()


@pytest.fixture
def model() -> DashboardModel:
    return DashboardModel(
        model="gpt-oss-120b",
        provider="groq",
        protocol_version="2025-11-25",
        servers=[
            ServerRow("netops", "stdio", "connected", "netops 1.0.0", tools=7),
            ServerRow("filesystem", "stdio", "connected", "fs 0.2.0", tools=14),
            ServerRow("netops-remote", "http", "connected", "netops 1.0.0", tools=7),
            ServerRow("git", "stdio", "failed", "no response to initialize"),
        ],
        transcript=[("user", "hola"), ("assistant", "buenas")],
        message_count=22,
    )


# --------------------------------------------------------------------------
# Accessibility: colour is never the only carrier
# --------------------------------------------------------------------------


def test_direction_is_carried_by_a_shape_not_only_a_colour():
    assert theme.direction_arrow("out") == "->"
    assert theme.direction_arrow("in") == "<-"
    assert theme.direction_arrow("out") != theme.direction_arrow("in")


def test_server_state_is_carried_by_a_glyph_not_only_a_colour():
    """Around 8% of men cannot separate red from green, which is exactly the
    pair this palette leans on hardest."""
    glyphs = {
        theme.state_style(state).glyph for state in ("connected", "failed", "pending")
    }
    assert len(glyphs) == 3, "each state needs its own shape, not just its own colour"


def test_the_sidebar_is_readable_without_any_colour(model: DashboardModel):
    output = draw(render_servers(model), width=30)
    assert "+" in output, "a connected server must be marked by a glyph"
    assert "!" in output, "a failed server must be marked by a glyph"
    assert "netops" in output and "git" in output


def test_a_log_line_names_its_direction_and_type_in_words():
    event = LogEvent(
        ts="2026-09-16T22:00:00.000+00:00",
        direction="in",
        server="netops",
        transport="http",
        type="error",
        method=None,
        id=4,
        duration_ms=12.5,
    )
    _style, text = log_line(event)
    assert "<-" in text, "direction must survive losing the colour"
    assert "error" in text, "the type must be spelled out, not only coloured"
    assert "netops" in text
    assert "12.5" in text


def test_an_error_is_red_whichever_way_it_travelled():
    """A failure is what you are scanning for, so it outranks direction."""
    assert theme.message_colour("out", "error") == theme.COLOUR_ERROR
    assert theme.message_colour("in", "error") == theme.COLOUR_ERROR


def test_direction_decides_the_colour_when_nothing_failed():
    assert theme.message_colour("out", "request") == theme.COLOUR_OUT
    assert theme.message_colour("in", "response") == theme.COLOUR_IN
    assert theme.COLOUR_OUT != theme.COLOUR_IN


def test_the_frame_never_uses_a_semantic_colour():
    """If the borders were blue, blue would stop meaning 'client to server'."""
    semantic = {theme.COLOUR_OUT, theme.COLOUR_IN, theme.COLOUR_ERROR}
    assert theme.COLOUR_FRAME not in semantic
    assert theme.COLOUR_MUTED not in semantic


# --------------------------------------------------------------------------
# Visibility of system status
# --------------------------------------------------------------------------


def test_the_header_names_the_model_and_the_protocol(model: DashboardModel):
    output = draw(render_header(model))
    assert "gpt-oss-120b" in output
    assert "groq" in output
    assert "2025-11-25" in output


def test_an_idle_host_says_so(model: DashboardModel):
    assert "ready" in draw(render_header(model))


def test_a_running_tool_is_named_not_just_spun(model: DashboardModel):
    """A spinner says 'something is happening'; the point is which server is
    being waited on."""
    model.turn = TurnState(status="calling", detail="netops__lookup_account")
    output = draw(render_header(model))
    assert "netops__lookup_account" in output


def test_the_iteration_counter_shows_how_much_budget_is_left(model: DashboardModel):
    model.turn = TurnState(status="thinking", iteration=3, max_iterations=10)
    assert "3/10" in draw(render_header(model))


def test_the_server_count_is_visible_at_a_glance(model: DashboardModel):
    output = draw(render_servers(model), width=30)
    assert "3/4" in output, "three of four servers are connected"
    assert "28 tools" in output


def test_a_failed_server_shows_why(model: DashboardModel):
    output = draw(render_servers(model), width=40)
    assert "no response" in output


# --------------------------------------------------------------------------
# Recognition rather than recall
# --------------------------------------------------------------------------


def test_the_shortcuts_are_always_on_screen(model: DashboardModel):
    output = draw(render_footer(model))
    for hint in ("F2", "F3", "/help", "/quit"):
        assert hint in output


def test_what_is_being_typed_is_echoed_in_the_prompt(model: DashboardModel):
    model.input_buffer = "/call netops__ping"
    assert "/call netops__ping" in draw(render_footer(model))


# --------------------------------------------------------------------------
# Minimalist design: detail on demand
# --------------------------------------------------------------------------


def region_names(model: DashboardModel) -> set[str]:
    return {child.name for child in build_layout(model).children}


def test_the_log_panel_can_be_collapsed(model: DashboardModel):
    model.show_log = False
    assert "log" not in region_names(model)


def test_the_log_panel_is_present_when_expanded(model: DashboardModel):
    model.show_log = True
    assert "log" in region_names(model)


def test_collapsing_the_log_gives_the_room_to_the_conversation(model: DashboardModel):
    """Detail on demand: the space has to go somewhere useful."""
    model.show_log = False
    collapsed = draw(build_layout(model), width=100, height=30)
    model.show_log = True
    expanded = draw(build_layout(model), width=100, height=30)
    assert collapsed.count("\n") == expanded.count("\n"), "the screen is the same height"
    assert "MCP log" in expanded and "MCP log" not in collapsed


def test_the_log_shows_only_its_tail(model: DashboardModel):
    model.log_lines = [("blue", f"line {n}") for n in range(50)]
    output = draw(render_log(model))
    assert "line 49" in output
    assert "line 0 " not in output


def test_an_empty_log_says_so_rather_than_showing_a_blank_box(model: DashboardModel):
    assert "No MCP traffic yet" in draw(render_log(model))


# --------------------------------------------------------------------------
# The conversation
# --------------------------------------------------------------------------


def test_an_empty_conversation_tells_the_user_what_to_do(model: DashboardModel):
    model.transcript = []
    output = draw(render_conversation(model))
    assert "/call" in output


def test_user_and_assistant_turns_are_distinguishable(model: DashboardModel):
    output = draw(render_conversation(model))
    assert "> hola" in output
    assert "buenas" in output


def test_tool_activity_is_subordinate_to_the_answer(model: DashboardModel):
    """A tool call is something the assistant did on the way to an answer."""
    model.transcript = [("tool", "calling netops__ping")]
    output = draw(render_conversation(model))
    assert "    calling netops__ping" in output


def test_markup_in_content_is_escaped_not_interpreted(model: DashboardModel):
    """Tool output is data. A ticket description containing brackets must not
    be able to restyle the screen - or to blank it."""
    model.transcript = [("assistant", "el ticket dice [bold red]urgente[/]")]
    output = draw(render_conversation(model))
    assert "[bold red]urgente" in output


# --------------------------------------------------------------------------
# The layout holds together
# --------------------------------------------------------------------------


def test_the_whole_screen_renders(model: DashboardModel):
    output = draw(build_layout(model), width=100, height=40)
    assert "uvg-mcp-host" in output
    assert "conversation" in output
    assert "servers" in output


def test_the_screen_survives_a_narrow_terminal(model: DashboardModel):
    """A classroom projector is not 200 columns wide."""
    output = draw(build_layout(model), width=60, height=24)
    assert output.strip(), "a narrow terminal must still render something"


def test_truncate_marks_what_it_cut():
    assert truncate("short", 20) == "short"
    cut = truncate("a very long line indeed that will not fit", 12)
    assert len(cut) == 12
    assert cut.endswith("…")


def test_truncate_flattens_newlines():
    """A multi-line tool result must not break the row it is drawn in."""
    assert "\n" not in truncate("one\ntwo\nthree", 40)


# --------------------------------------------------------------------------
# Keyboard decoding
# --------------------------------------------------------------------------


def test_an_ordinary_character_is_itself():
    assert keyboard.decode("a") == "a"
    assert keyboard.decode("/") == "/"
    assert keyboard.decode("á") == "á"


def test_enter_and_backspace_are_named_on_both_platforms():
    assert keyboard.decode("\r") == keyboard.ENTER
    assert keyboard.decode("\n") == keyboard.ENTER
    assert keyboard.decode("\x08") == keyboard.BACKSPACE, "Windows backspace"
    assert keyboard.decode("\x7f") == keyboard.BACKSPACE, "POSIX backspace"


def test_interrupt_and_eof_are_distinguished():
    assert keyboard.decode("\x03") == keyboard.INTERRUPT
    assert keyboard.decode("\x04") == keyboard.EOF


@pytest.mark.parametrize(
    "sequence,expected",
    [
        ("\x00<", keyboard.TOGGLE_LOG),      # Windows F2
        ("\xe0<", keyboard.TOGGLE_LOG),      # the other lead byte
        ("\x00=", keyboard.TOGGLE_SERVERS),  # Windows F3
        ("\x1bOQ", keyboard.TOGGLE_LOG),     # POSIX F2
        ("\x1bOR", keyboard.TOGGLE_SERVERS), # POSIX F3
        ("\x1b[12~", keyboard.TOGGLE_LOG),
    ],
)
def test_function_keys_decode_on_either_platform(sequence, expected):
    """The Windows sequences are covered even when the suite runs on Linux,
    which is the only way they would ever be tested at all."""
    assert keyboard.decode(sequence) == expected


def test_an_unknown_special_key_is_dropped_not_typed():
    """A stray escape sequence must never land inside what the user is typing."""
    assert keyboard.decode("\x00\x99") == keyboard.IGNORE
    assert keyboard.decode("\x1b[99~") == keyboard.IGNORE
    assert keyboard.decode("") == keyboard.IGNORE


def test_control_characters_are_never_inserted_as_text():
    for code in ("\x01", "\x0b", "\x1f"):
        assert keyboard.decode(code) == keyboard.IGNORE


def test_typing_builds_the_buffer():
    buffer = ""
    for char in "/help":
        buffer = keyboard.apply(buffer, keyboard.decode(char))
    assert buffer == "/help"


def test_backspace_removes_the_last_character():
    assert keyboard.apply("abc", keyboard.BACKSPACE) == "ab"


def test_backspace_on_an_empty_buffer_is_harmless():
    assert keyboard.apply("", keyboard.BACKSPACE) == ""


def test_a_named_key_does_not_change_the_buffer():
    for key in (keyboard.ENTER, keyboard.TOGGLE_LOG, keyboard.INTERRUPT, keyboard.IGNORE):
        assert keyboard.apply("hola", key) == "hola"


def test_raw_reading_is_refused_when_stdin_is_not_a_terminal(monkeypatch):
    """Piped input - a script, or this very test suite - has no terminal to
    put into raw mode, so the CLI has to fall back rather than crash."""
    class NotATerminal:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", NotATerminal())
    assert keyboard.KeyReader().available() is False


def test_a_long_model_name_never_pushes_the_protocol_version_off_screen(model: DashboardModel):
    """The version is the field a grader looks for; the model name gives way."""
    model.model = "some-extremely-long-vendor/model-identifier-v3-instruct-preview"
    output = draw(render_header(model), width=100)
    assert "2025-11-25" in output


def test_the_header_still_names_the_provider_after_truncation(model: DashboardModel):
    model.model = "some-extremely-long-vendor/model-identifier-v3-instruct-preview"
    assert "groq" in draw(render_header(model), width=100)


def test_a_long_tool_name_does_not_crowd_out_the_header(model: DashboardModel):
    model.turn = TurnState(
        status="calling",
        detail="some-server__a_very_long_tool_name_indeed_beyond_reason",
        iteration=2,
        max_iterations=10,
    )
    output = draw(render_header(model), width=100)
    assert "2025-11-25" in output, "the version survives a long tool name too"
    assert "2/10" in output


# --------------------------------------------------------------------------
# Command output has to fit the panel it lands in
# --------------------------------------------------------------------------


def test_the_capture_width_matches_the_conversation_panel():
    """The bug this pins down: output rendered wider than the panel wraps
    inside it, and a wrapped table is not a table any more - it is the same
    characters in an order nobody can read."""
    from host.ui.dashboard import CONVERSATION_CHROME, SIDEBAR_WIDTH, conversation_width

    assert conversation_width(110) == 110 - SIDEBAR_WIDTH - CONVERSATION_CHROME
    assert conversation_width(110) == 80


def test_the_capture_width_never_collapses_to_nothing():
    """A terminal narrower than the sidebar must not produce a zero or
    negative width, which rich would reject."""
    from host.ui.dashboard import conversation_width

    assert conversation_width(20) >= 20
    assert conversation_width(1) >= 20


def test_command_output_is_not_indented(model: DashboardModel):
    """Output arrives pre-rendered at the panel's exact width, so four columns
    of indent would push every line one character past the edge."""
    model.transcript = [("output", "|" + "-" * 60 + "|")]
    output = draw(render_conversation(model), width=70)
    assert "\n     |" not in output, "an indent would have been added"


def test_command_output_is_not_double_spaced(model: DashboardModel):
    """Blank lines between the rows of one table are as unreadable as wrapping."""
    model.transcript = [("output", "row one"), ("output", "row two")]
    output = draw(render_conversation(model), width=60)
    body = [line for line in output.splitlines() if "row " in line]
    assert len(body) == 2
    joined = output[output.index("row one"):output.index("row two")]
    assert joined.count("\n") == 1, "the rows must be adjacent"


def test_a_conversation_turn_is_still_separated_by_a_blank_line(model: DashboardModel):
    model.transcript = [("user", "uno"), ("user", "dos")]
    output = draw(render_conversation(model), width=60)
    between = output[output.index("uno"):output.index("dos")]
    assert between.count("\n") >= 2, "turns stay separated"


# --------------------------------------------------------------------------
# The conversation shows its tail, not its head
# --------------------------------------------------------------------------


def test_the_newest_turn_is_always_visible(model: DashboardModel):
    """The bug this pins down: a panel renders from the top and drops the
    overflow, so after one long answer every later turn fell off the screen.
    Asking a second question appeared to do nothing at all."""
    from host.ui.dashboard import visible_transcript

    model.terminal_width, model.terminal_height = 110, 30
    model.transcript = [
        ("user", "pregunta uno"),
        ("assistant", "X" * 600),
        ("user", "pregunta dos"),
        ("assistant", "la respuesta que importa"),
    ]
    kept = visible_transcript(model)
    assert kept[-1] == ("assistant", "la respuesta que importa")
    assert ("assistant", "X" * 600) not in kept, "the old answer gives way, not the new one"


def test_the_newest_entry_survives_even_alone(model: DashboardModel):
    """A single answer taller than the whole panel must still be shown."""
    from host.ui.dashboard import visible_transcript

    model.terminal_width, model.terminal_height = 80, 20
    model.transcript = [("assistant", "Y" * 5000)]
    assert len(visible_transcript(model)) == 1


def test_a_short_conversation_is_shown_whole(model: DashboardModel):
    from host.ui.dashboard import visible_transcript

    model.terminal_width, model.terminal_height = 110, 40
    model.transcript = [("user", "hola"), ("assistant", "buenas")]
    assert visible_transcript(model) == model.transcript


def test_collapsing_the_log_makes_room_for_more_conversation():
    from host.ui.dashboard import conversation_rows

    assert conversation_rows(30, show_log=False) > conversation_rows(30, show_log=True)


def test_the_panel_never_asks_for_a_negative_number_of_rows():
    from host.ui.dashboard import conversation_rows

    assert conversation_rows(4, show_log=True) >= 3
    assert conversation_rows(1, show_log=True) >= 3


def test_the_newest_answer_is_rendered_not_just_selected(model: DashboardModel):
    """End to end through the panel, which is where the user would see it."""
    model.terminal_width, model.terminal_height = 110, 30
    model.transcript = [
        ("user", "pregunta uno"),
        ("assistant", "Z" * 600),
        ("user", "/servers"),
        ("output", "netops   stdio   connected"),
    ]
    output = draw(render_conversation(model), width=80)
    assert "netops   stdio   connected" in output
