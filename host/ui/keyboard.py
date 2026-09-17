"""Reading one keystroke at a time, so the dashboard can stay on screen.

A full-screen layout and a blocking `input()` cannot share a terminal: one
owns the cursor and repaints, the other owns the cursor and scrolls. The usual
workaround is to stop the live display, prompt, and start it again, which makes
the whole screen flash away between every turn.

Reading keys ourselves avoids that. The layout draws the prompt as part of
itself, characters land in a buffer, and the screen is repainted the same way
it is repainted for anything else. Nothing has to be torn down to ask a
question.

The decoding is a pure function so it can be tested on any platform - the
Windows key sequences below are verified by the suite even when it runs on
Linux, which is the only way they would ever be covered.

Terminals disagree about special keys, so the rule here is: what cannot be
decoded is ignored rather than inserted. A stray escape sequence must never end
up inside the text the user is typing.
"""

from __future__ import annotations

import sys

# What a decoded keystroke can be. Anything else is a literal character.
ENTER = "enter"
BACKSPACE = "backspace"
INTERRUPT = "interrupt"  # Ctrl+C
EOF = "eof"  # Ctrl+D / Ctrl+Z
TOGGLE_LOG = "toggle_log"  # F2
TOGGLE_SERVERS = "toggle_servers"  # F3
IGNORE = "ignore"

# Windows returns a lead byte for a special key, then the scan code on the next
# read. These are the scan codes we act on; every other one is ignored.
WINDOWS_LEAD_BYTES = ("\x00", "\xe0")
_WINDOWS_SCANCODES = {
    ";": "f1",
    "<": TOGGLE_LOG,  # F2
    "=": TOGGLE_SERVERS,  # F3
}

_CONTROL_KEYS = {
    "\r": ENTER,
    "\n": ENTER,
    "\x08": BACKSPACE,  # Windows
    "\x7f": BACKSPACE,  # POSIX
    "\x03": INTERRUPT,
    "\x04": EOF,
    "\x1a": EOF,  # Ctrl+Z on Windows
}

# POSIX escape sequences for the function keys, in both the common flavours.
_POSIX_SEQUENCES = {
    "\x1bOQ": TOGGLE_LOG,
    "\x1b[12~": TOGGLE_LOG,
    "\x1bOR": TOGGLE_SERVERS,
    "\x1b[13~": TOGGLE_SERVERS,
}


def decode(sequence: str) -> str:
    """Name one keystroke, or return the literal character it stands for.

    `sequence` is what a reader collected for a single key press: one character
    for an ordinary key, two for a Windows special key, three or more for a
    POSIX escape sequence.
    """
    if not sequence:
        return IGNORE

    if sequence in _POSIX_SEQUENCES:
        return _POSIX_SEQUENCES[sequence]

    first = sequence[0]
    if first in WINDOWS_LEAD_BYTES:
        # A special key. Unknown scan codes are dropped rather than typed.
        scancode = sequence[1] if len(sequence) > 1 else ""
        return _WINDOWS_SCANCODES.get(scancode, IGNORE)

    if first in _CONTROL_KEYS:
        return _CONTROL_KEYS[first]

    if len(sequence) > 1:
        # An escape sequence we do not recognise. Never insert it as text.
        return IGNORE

    if first.isprintable():
        return first

    return IGNORE


def apply(buffer: str, key: str) -> str:
    """Apply one decoded key to the input buffer.

    Only ordinary characters and backspace change it; everything else is the
    caller's business.
    """
    if key == BACKSPACE:
        return buffer[:-1]
    if len(key) == 1 and key.isprintable():
        return buffer + key
    return buffer


class KeyReader:
    """Reads single keystrokes from the terminal.

    Windows and POSIX need different machinery, and neither is available on the
    other, so the import happens inside the branch that uses it.
    """

    def __init__(self) -> None:
        self.is_windows = sys.platform == "win32"

    def available(self) -> bool:
        """Whether raw key reading can work here at all.

        A piped or redirected stdin has no terminal to put into raw mode, which
        is exactly the case when the CLI is driven by a script or a test.
        """
        try:
            return sys.stdin.isatty()
        except (AttributeError, ValueError):
            return False

    def read_key(self) -> str:
        """Block until one key is pressed and return its decoded name."""
        if self.is_windows:
            return decode(self._read_windows())
        return decode(self._read_posix())

    # -- platforms ---------------------------------------------------------

    def _read_windows(self) -> str:
        import msvcrt

        char = msvcrt.getwch()
        if char in WINDOWS_LEAD_BYTES:
            return char + msvcrt.getwch()
        return char

    def _read_posix(self) -> str:
        import termios
        import tty

        descriptor = sys.stdin.fileno()
        saved = termios.tcgetattr(descriptor)
        try:
            tty.setraw(descriptor)
            char = sys.stdin.read(1)
            if char != "\x1b":
                return char
            # An escape sequence: read the rest without blocking forever if it
            # was a bare Escape key.
            import select

            collected = char
            while select.select([sys.stdin], [], [], 0.05)[0]:
                collected += sys.stdin.read(1)
                if collected in _POSIX_SEQUENCES or len(collected) > 6:
                    break
            return collected
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
