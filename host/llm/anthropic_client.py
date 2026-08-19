"""Anthropic Messages API wrapper, plus the MCP -> Anthropic schema translation.

This is the only module that imports the Anthropic SDK. Everything above it
works against the small surface defined here, which is what lets the tests
drive the agent loop with a scripted client and no API key.
"""

from __future__ import annotations

from typing import Any, Protocol

import anthropic

from host.mcp.registry import RegisteredTool

DEFAULT_MAX_TOKENS = 16000


class LLMError(Exception):
    """A model call failed, described in terms the operator can act on.

    The SDK's own errors are accurate but unhelpful at a prompt - "credit
    balance is too low" arrives wrapped in a raw 400 body. Translating here
    also keeps the Anthropic SDK from leaking into the CLI.
    """


def mcp_tool_to_anthropic(tool: RegisteredTool) -> dict[str, Any]:
    """Translate one MCP tool descriptor into an Anthropic tool definition.

    The two formats are nearly identical, and the difference is exactly the
    kind of thing that fails silently: MCP spells the schema `inputSchema`,
    the Messages API spells it `input_schema`. The name is the namespaced one,
    which already satisfies Anthropic's ^[a-zA-Z0-9_-]{1,128}$ rule.
    """
    return {
        "name": tool.qualified_name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def mcp_tools_to_anthropic(tools: list[RegisteredTool]) -> list[dict[str, Any]]:
    return [mcp_tool_to_anthropic(tool) for tool in sorted(tools, key=lambda t: t.qualified_name)]


class LLMResponse(Protocol):
    """The slice of a Messages API response the agent loop actually reads."""

    content: list[Any]
    stop_reason: str | None


class LLMClient(Protocol):
    """What the agent needs from a model. Implemented for real below, and by a
    scripted double in the tests."""

    def create(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> LLMResponse: ...


class AnthropicClient:
    """Thin wrapper over the Messages API."""

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = anthropic.Anthropic(api_key=api_key)

    def create(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> LLMResponse:
        try:
            return self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                tools=tools,
                messages=messages,
            )
        # Most specific first: each of these needs a different fix.
        except anthropic.AuthenticationError as exc:
            raise LLMError(
                "The API key was rejected. Check ANTHROPIC_API_KEY in .env."
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(
                f"The API key is not allowed to use {self.model!r}."
            ) from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(
                f"Model {self.model!r} does not exist. Check ANTHROPIC_MODEL in .env."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Rate limited by the API. Wait a moment and retry.") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(describe_bad_request(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}: {exc.message}") from exc


def describe_bad_request(exc: Exception) -> str:
    """Turn a 400 into something the operator can act on.

    A spent account is by far the most common 400 in this project, and the raw
    body buries it, so it gets named explicitly.
    """
    text = str(exc)
    if "credit balance" in text.lower():
        return (
            "The Anthropic account has no credit left. Add credit at "
            "console.anthropic.com under Plans & Billing, or ask for the course "
            "credits to be assigned. Tool calls with /call keep working without it."
        )
    return f"The API rejected the request: {text}"
