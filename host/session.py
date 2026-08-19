"""In-memory conversation state for one CLI session.

The Messages API is stateless, so the whole history is resent on every request.
This holds that history, including the tool_use and tool_result blocks - the
model needs to see the results of its own earlier calls to follow up on them.
"""

from __future__ import annotations

from typing import Any

BASE_SYSTEM_PROMPT = (
    "You are a technical-support assistant for an internet service provider in "
    "Guatemala. You answer through tools connected over MCP; never invent an "
    "account, a metric, a ticket id or an outage. If a tool reports an error, "
    "read it and tell the user plainly what happened rather than retrying blindly. "
    "Reply in the language the user wrote in. Keep answers short and concrete."
)


class Session:
    """The message list plus the system prompt sent with every request."""

    def __init__(self, system_prompt: str = BASE_SYSTEM_PROMPT) -> None:
        self.system_prompt = system_prompt
        self.messages: list[dict[str, Any]] = []

    def add_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, content: Any) -> None:
        """Store the assistant turn verbatim, tool_use blocks included."""
        self.messages.append({"role": "assistant", "content": content})

    def add_tool_results(self, results: list[dict[str, Any]]) -> None:
        """Tool results go back as a single user turn, all of them together.

        Splitting them across several messages teaches the model to stop making
        parallel calls, so they stay in one turn even when there are many.
        """
        self.messages.append({"role": "user", "content": results})

    def clear(self) -> None:
        self.messages.clear()

    def __len__(self) -> int:
        return len(self.messages)


def build_system_prompt(server_instructions: dict[str, str]) -> str:
    """Fold each server's own `instructions` into the system prompt.

    An MCP server returns `instructions` during initialize precisely so the host
    can tell the model how its tools are meant to be used. Ignoring it would
    throw away guidance the server author wrote for this exact purpose.
    """
    sections = [BASE_SYSTEM_PROMPT]
    for name, instructions in sorted(server_instructions.items()):
        if instructions.strip():
            sections.append(f"Guidance from the '{name}' server:\n{instructions.strip()}")
    return "\n\n".join(sections)
