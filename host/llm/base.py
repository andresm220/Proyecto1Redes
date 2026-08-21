"""What the agent needs from a model, independent of any provider.

The agent talks to this surface and nothing else. Each provider adapter takes
the same inputs - the conversation, our own tool descriptors, a system prompt -
and normalises its answer back into the block shape below. That is what lets the
same loop run against providers whose wire formats have nothing in common.

The normalised block shape, which is also what Session stores:

    {"type": "text", "text": "..."}
    {"type": "tool_use", "id": "...", "name": "...", "input": {...}}

and the tool results the agent sends back:

    {"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": bool}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from host.mcp.registry import RegisteredTool


class LLMError(Exception):
    """A model call failed, described in terms the operator can act on.

    Provider SDKs and HTTP APIs report failures in their own vocabulary. This
    is the single type the CLI catches, so a spent account reads the same
    whichever provider produced it.
    """


class LLMResponse(Protocol):
    """The slice of a model response the agent loop actually reads."""

    content: list[Any]
    stop_reason: str | None


@dataclass
class NormalisedResponse:
    """A response built by an adapter, in the shape the agent expects."""

    content: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = "end_turn"


class LLMClient(Protocol):
    """Implemented for real by each adapter, and by a scripted double in tests."""

    def create(
        self,
        messages: list[dict[str, Any]],
        tools: list[RegisteredTool],
        system: str,
    ) -> LLMResponse: ...
