"""Adapter for any OpenAI-compatible chat completions endpoint.

Groq, OpenRouter, Ollama, LM Studio and several others all expose the same
`/chat/completions` shape, so one adapter covers them and switching provider is
a change of base URL. It is written against raw HTTP with `httpx` rather than a
vendor SDK, which keeps the dependency list unchanged.

Everything provider-specific is confined here. The adapter converts in both
directions: our normalised blocks out to the OpenAI wire format on the way in,
and the OpenAI response back to normalised blocks on the way out, so the agent
loop cannot tell which provider answered.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import httpx

from host.llm.base import LLMError, NormalisedResponse
from host.mcp.registry import RegisteredTool

DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RETRIES = 3
MAX_RETRY_WAIT = 60.0

# Base URL and a sensible model for each provider that speaks this dialect.
PROVIDER_PRESETS: dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
}


def mcp_tool_to_openai(tool: RegisteredTool) -> dict[str, Any]:
    """Translate one MCP tool descriptor into an OpenAI function definition.

    All three formats carry the same JSON Schema; only the envelope differs.
    MCP calls it `inputSchema`, the Anthropic Messages API calls it
    `input_schema`, and here it is nested as `function.parameters`.
    """
    return {
        "type": "function",
        "function": {
            "name": tool.qualified_name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _text_of(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks
    parts = []
    for block in blocks or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(part for part in parts if part)


def to_openai_messages(messages: list[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    """Convert our normalised history into OpenAI's message list.

    The two disagree about where tool results live. We keep them as blocks in a
    user turn, the way MCP and Anthropic do; OpenAI wants one message per result
    with `role: "tool"`. This is where that is reconciled.
    """
    converted: list[dict[str, Any]] = []
    if system:
        converted.append({"role": "system", "content": system})

    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if role == "user":
            if isinstance(content, str):
                converted.append({"role": "user", "content": content})
                continue
            # A user turn carrying tool results becomes one `tool` message each.
            leftover_text = []
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    converted.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id"),
                            "content": str(block.get("content", "")),
                        }
                    )
                elif block.get("type") == "text":
                    leftover_text.append(block.get("text", ""))
            if leftover_text:
                converted.append({"role": "user", "content": "\n".join(leftover_text)})

        elif role == "assistant":
            tool_calls = []
            for block in content or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                # OpenAI carries the arguments as a JSON string,
                                # not as an object.
                                "arguments": json.dumps(
                                    block.get("input") or {}, ensure_ascii=False
                                ),
                            },
                        }
                    )
            entry: dict[str, Any] = {"role": "assistant", "content": _text_of(content) or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            converted.append(entry)

    return converted


def from_openai_response(payload: dict[str, Any]) -> NormalisedResponse:
    """Convert one chat completion back into normalised blocks."""
    choices = payload.get("choices") or []
    if not choices:
        raise LLMError("the model returned no choices")

    choice = choices[0]
    message = choice.get("message") or {}
    blocks: list[dict[str, Any]] = []

    text = message.get("content")
    if isinstance(text, str) and text.strip():
        blocks.append({"type": "text", "text": text})

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError:
            # A model that emits malformed arguments should be told so through
            # the normal tool-error path, not crash the turn here.
            arguments = {"__malformed_arguments__": raw_arguments}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or "call_0",
                "name": function.get("name", ""),
                "input": arguments if isinstance(arguments, dict) else {},
            }
        )

    # OpenAI says "tool_calls" where the agent loop expects "tool_use".
    finish_reason = choice.get("finish_reason")
    stop_reason = "tool_use" if finish_reason == "tool_calls" else "end_turn"
    return NormalisedResponse(content=blocks, stop_reason=stop_reason)


class OpenAICompatibleClient:
    """Talks to any endpoint exposing OpenAI's /chat/completions."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        on_retry: Callable[[float, str], None] | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep
        self._on_retry = on_retry
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            # Local runtimes such as Ollama need no key at all.
            self._headers["Authorization"] = f"Bearer {api_key}"

    def create(
        self,
        messages: list[dict[str, Any]],
        tools: list[RegisteredTool],
        system: str,
    ):
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": to_openai_messages(messages, system),
        }
        if tools:
            # Several providers reject an empty tools array outright.
            body["tools"] = [
                mcp_tool_to_openai(tool)
                for tool in sorted(tools, key=lambda t: t.qualified_name)
            ]
            body["tool_choice"] = "auto"

        response = self._post_with_retries(body)

        if response.status_code != 200:
            raise LLMError(describe_http_error(response))

        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMError(f"The endpoint returned a non-JSON body: {response.text[:200]}") from exc

        return from_openai_response(payload)

    def _post_with_retries(self, body: dict[str, Any]) -> httpx.Response:
        """POST the request, waiting out rate limits when the provider asks.

        Only 429 is retried, and only for as long as the provider itself says
        to wait. Anything else is a real failure and is reported immediately
        rather than hidden behind repeated attempts.
        """
        for attempt in range(self.max_retries + 1):
            try:
                response = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers,
                    json=body,
                    timeout=self.timeout,
                )
            except httpx.TimeoutException as exc:
                raise LLMError(f"The model did not answer within {self.timeout:.0f}s.") from exc
            except httpx.HTTPError as exc:
                raise LLMError(f"Could not reach {self.base_url}: {exc}") from exc

            if response.status_code != 429 or attempt == self.max_retries:
                return response

            delay = retry_delay(response)
            if delay is None:
                return response
            if self._on_retry is not None:
                self._on_retry(delay, f"rate limited, waiting {delay:.0f}s")
            self._sleep(delay)

        return response  # pragma: no cover - loop always returns first


def retry_delay(response: httpx.Response) -> float | None:
    """How long to wait before retrying a 429, if the provider said so.

    Free tiers rate-limit on tokens per minute, and a request carrying seven
    tool schemas is large enough to trip that regularly. Providers answer with
    the exact wait, either in the standard header or written into the message,
    so honouring it turns a hard failure into a pause.
    """
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), MAX_RETRY_WAIT)
        except ValueError:
            pass
    try:
        body = response.json()
        message = str((body.get("error") or {}).get("message", ""))
    except (ValueError, AttributeError):
        message = response.text or ""
    match = re.search(r"try again in ([\d.]+)\s*s", message, re.IGNORECASE)
    if match:
        # A small margin, because the window is measured on the provider's clock.
        return min(float(match.group(1)) + 1.0, MAX_RETRY_WAIT)
    return None


def describe_http_error(response: httpx.Response) -> str:
    """Turn a failed HTTP response into something the operator can act on."""
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message", ""))
            elif isinstance(error, str):
                detail = error
    except ValueError:
        detail = response.text[:200]

    status = response.status_code
    if status == 401:
        return "The API key was rejected. Check LLM_API_KEY in .env."
    if status == 403:
        return f"The API key is not allowed to use this model. {detail}".strip()
    if status == 404:
        return f"Model or endpoint not found. Check LLM_MODEL and LLM_BASE_URL. {detail}".strip()
    if status == 429:
        return f"Rate limited by the provider. Wait a moment and retry. {detail}".strip()
    if status >= 500:
        return f"The provider returned a server error ({status}). {detail}".strip()
    return f"The request was rejected ({status}). {detail}".strip()
