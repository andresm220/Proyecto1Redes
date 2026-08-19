"""The agentic loop: model -> tool_use -> tool_result -> model, until it stops.

The loop is capped at MAX_ITERATIONS per user turn. Without a cap, a model that
keeps asking for tools - or a tool that keeps failing in a way the model tries
to work around - would spend tokens indefinitely.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from host.llm.anthropic_client import LLMClient, mcp_tools_to_anthropic
from host.mcp.client import McpError
from host.mcp.registry import ServerRegistry, ToolNotFoundError
from host.mcp.transport import TransportError
from host.session import Session

MAX_ITERATIONS = 10

# (kind, payload) pairs the CLI renders as the turn progresses.
EventHandler = Callable[[str, dict[str, Any]], None]


class Agent:
    """Runs one user turn to completion, executing tools along the way."""

    def __init__(
        self,
        llm: LLMClient,
        registry: ServerRegistry,
        session: Session,
        max_iterations: int = MAX_ITERATIONS,
        on_event: EventHandler | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.session = session
        self.max_iterations = max_iterations
        self._on_event = on_event

    def _emit(self, kind: str, **payload: Any) -> None:
        if self._on_event is not None:
            self._on_event(kind, payload)

    # -- tool execution ----------------------------------------------------

    def _flatten_content(self, content: Any) -> str:
        """Turn MCP content blocks into the plain text a tool_result carries."""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return json.dumps(content, ensure_ascii=False)
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(parts)

    def execute_tool(self, tool_use_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call and shape the outcome as an Anthropic tool_result.

        Every failure is reported back to the model as a tool_result with
        is_error set, rather than raised. The model can then correct itself -
        fix an argument, look an id up first - which is the whole point of
        letting it drive.
        """
        self._emit("tool_call", name=name, arguments=arguments)
        try:
            result = self.registry.call(name, arguments)
        except ToolNotFoundError as exc:
            return self._tool_result(tool_use_id, str(exc), is_error=True)
        except McpError as exc:
            # A protocol-level rejection, most often -32602 for bad arguments.
            return self._tool_result(
                tool_use_id, f"Protocol error {exc.code}: {exc.message}", is_error=True
            )
        except TransportError as exc:
            return self._tool_result(tool_use_id, f"Transport error: {exc}", is_error=True)

        text = self._flatten_content(result.get("content", []))
        # MCP's isError and Anthropic's is_error mean the same thing: the tool
        # ran, and it failed for a domain reason.
        is_error = bool(result.get("isError"))
        self._emit("tool_result", name=name, is_error=is_error, text=text)
        return self._tool_result(tool_use_id, text, is_error=is_error)

    @staticmethod
    def _tool_result(tool_use_id: str, text: str, is_error: bool = False) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": text,
            "is_error": is_error,
        }

    # -- the loop ----------------------------------------------------------

    def run_turn(self, user_text: str) -> str:
        """Send one user message and run until the model stops calling tools."""
        self.session.add_user_text(user_text)
        tools = mcp_tools_to_anthropic(self.registry.tools)
        answer = ""

        for iteration in range(1, self.max_iterations + 1):
            self._emit("model_request", iteration=iteration)
            response = self.llm.create(
                messages=self.session.messages,
                tools=tools,
                system=self.session.system_prompt,
            )
            self.session.add_assistant(response.content)

            answer = self._text_of(response)
            if answer:
                self._emit("assistant_text", text=answer)

            if response.stop_reason != "tool_use":
                return answer

            tool_uses = [block for block in response.content if _block_type(block) == "tool_use"]
            if not tool_uses:
                # stop_reason said tool_use but no block carries one; nothing to
                # execute, so stop rather than loop on an empty turn.
                return answer

            results = [
                self.execute_tool(
                    _block_attr(block, "id"),
                    _block_attr(block, "name"),
                    dict(_block_attr(block, "input") or {}),
                )
                for block in tool_uses
            ]
            # All results in one user turn: splitting them trains the model out
            # of making parallel calls.
            self.session.add_tool_results(results)

        self._emit("max_iterations", limit=self.max_iterations)
        return answer or (
            f"Stopped after {self.max_iterations} tool rounds without a final answer. "
            "Try narrowing the request."
        )

    @staticmethod
    def _text_of(response: Any) -> str:
        parts = [
            _block_attr(block, "text")
            for block in response.content
            if _block_type(block) == "text" and _block_attr(block, "text")
        ]
        return "\n".join(parts).strip()


def _block_attr(block: Any, name: str, default: Any = None) -> Any:
    """Read a field off SDK block objects and plain dicts alike.

    The Anthropic SDK returns attribute-style objects, while the scripted
    client in the tests is easier to write with dicts. Supporting both keeps
    the loop testable without dragging the SDK into every test.
    """
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)


def _block_type(block: Any) -> str:
    return str(_block_attr(block, "type", ""))
