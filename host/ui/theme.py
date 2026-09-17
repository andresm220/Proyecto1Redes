"""The visual language, in one place.

Every colour in this project means something, and it means the same thing
everywhere. That is the whole reason this module exists rather than the styles
being written inline where they are used: a palette that drifts between the
live trace and `/log tail` is worse than no palette at all, because the reader
learns a rule and then the rule lies to them.

## The rule

Direction is the primary axis, because in a protocol trace the first question
is always "who said this".

    blue    client -> server      we asked
    green   server -> client      they answered
    red     error                 something failed, either way

Red overrides direction. An error is the thing you are looking for when you are
reading a log at all, so it wins over the question of who sent it.

## Colour is never load-bearing on its own

Every rendering that uses a colour also carries the same information in a form
that survives losing it:

    ->  <-      the arrow says direction
    request / response / error      the type column says what it is
    +  -  !     the status glyph says whether a server is up

Around 8% of men have a red-green colour vision deficiency, which is exactly
the pair this palette leans on hardest. A printed report is monochrome. A
projector in a classroom washes out saturated colour. In all three cases the
trace has to stay readable, and it does, because colour is redundant here -
it makes the structure faster to see, never possible to see.

## Why these three and no more

A palette that distinguishes seven things distinguishes nothing. Three
categories is what a reader can hold while scanning, and it maps exactly onto
the three questions the log answers: did we send it, did they answer, did it
work.
"""

from __future__ import annotations

from dataclasses import dataclass

# -- the semantic palette ---------------------------------------------------

COLOUR_OUT = "blue"  # client -> server
COLOUR_IN = "green"  # server -> client
COLOUR_ERROR = "red"

# Chrome: never competes with the semantic colours above, which is why it is
# all dim or neutral. If the frame were coloured, the palette would stop
# meaning anything.
COLOUR_FRAME = "grey50"
COLOUR_MUTED = "grey62"
COLOUR_HEADING = "bold white"
COLOUR_ACCENT = "cyan"

# -- server state -----------------------------------------------------------


@dataclass(frozen=True)
class StateStyle:
    """How one server state looks, in colour and in shape."""

    glyph: str
    colour: str
    label: str

    def markup(self) -> str:
        return f"[{self.colour}]{self.glyph}[/{self.colour}]"


# The glyph is the point: it survives a monochrome printout, and it is what a
# colour-blind reader actually reads.
STATE_CONNECTED = StateStyle("+", "green", "connected")
STATE_FAILED = StateStyle("!", "red", "failed")
STATE_PENDING = StateStyle("-", "yellow", "connecting")

_STATES = {
    "connected": STATE_CONNECTED,
    "failed": STATE_FAILED,
    "pending": STATE_PENDING,
}


def state_style(state: str) -> StateStyle:
    return _STATES.get(state, STATE_PENDING)


# -- message direction and type ---------------------------------------------

ARROW_OUT = "->"
ARROW_IN = "<-"


def direction_arrow(direction: str) -> str:
    """Direction as a shape, so it survives losing the colour."""
    return ARROW_OUT if direction == "out" else ARROW_IN


def message_colour(direction: str, message_type: str = "") -> str:
    """The colour for one logged message.

    An error is red whichever way it travelled: when you are reading a trace,
    a failure is what you are looking for, so it outranks the question of who
    sent it.
    """
    if message_type == "error":
        return COLOUR_ERROR
    return COLOUR_OUT if direction == "out" else COLOUR_IN


def transport_label(transport: str) -> str:
    """A short tag for the transport, padded so a column of them lines up."""
    return {"stdio": "stdio", "http": "http "}.get(transport, transport[:5])
