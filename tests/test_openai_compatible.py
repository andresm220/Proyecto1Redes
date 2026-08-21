"""Tests for the OpenAI-compatible adapter.

The adapter's whole job is translation in both directions, so that is what these
exercise: our normalised blocks out to the OpenAI wire format, and a chat
completion back into blocks the agent loop understands.
"""

from __future__ import annotations

import json

import httpx
import pytest

from host.llm.base import LLMError
from host.llm.openai_compatible import (
    OpenAICompatibleClient,
    describe_http_error,
    from_openai_response,
    mcp_tool_to_openai,
    retry_delay,
    to_openai_messages,
)
from host.mcp.registry import RegisteredTool

REQUEST = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def response_with(status: int, body: dict | None = None, headers: dict | None = None):
    return httpx.Response(
        status_code=status, request=REQUEST, json=body or {}, headers=headers or {}
    )


# --------------------------------------------------------------------------
# Tool translation
# --------------------------------------------------------------------------


def test_the_schema_is_nested_under_function_parameters():
    """MCP says inputSchema, Anthropic says input_schema, OpenAI nests it."""
    tool = RegisteredTool(
        server="netops",
        name="check_service_status",
        definition={
            "name": "check_service_status",
            "description": "Read the link state.",
            "inputSchema": {
                "type": "object",
                "properties": {"account_id": {"type": "string"}},
                "required": ["account_id"],
            },
        },
    )
    translated = mcp_tool_to_openai(tool)

    assert translated["type"] == "function"
    assert translated["function"]["name"] == "netops__check_service_status"
    assert translated["function"]["description"] == "Read the link state."
    assert translated["function"]["parameters"]["required"] == ["account_id"]
    assert "inputSchema" not in json.dumps(translated)


# --------------------------------------------------------------------------
# History: ours -> OpenAI
# --------------------------------------------------------------------------


def test_the_system_prompt_becomes_the_first_message():
    converted = to_openai_messages([], "you are a support agent")
    assert converted == [{"role": "system", "content": "you are a support agent"}]


def test_plain_user_text_passes_through():
    converted = to_openai_messages([{"role": "user", "content": "hola"}], "")
    assert converted == [{"role": "user", "content": "hola"}]


def test_an_assistant_tool_call_becomes_tool_calls_with_string_arguments():
    """OpenAI carries the arguments as a JSON string, not as an object."""
    history = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "voy a revisar"},
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "netops__lookup_account",
                    "input": {"account_id": "GT-10231"},
                },
            ],
        }
    ]
    message = to_openai_messages(history, "")[0]

    assert message["role"] == "assistant"
    assert message["content"] == "voy a revisar"
    call = message["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["function"]["name"] == "netops__lookup_account"
    assert json.loads(call["function"]["arguments"]) == {"account_id": "GT-10231"}


def test_tool_results_become_one_tool_message_each():
    """We keep results as blocks in a user turn; OpenAI wants role: tool."""
    history = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "uno", "is_error": False},
                {"type": "tool_result", "tool_use_id": "call_2", "content": "dos", "is_error": True},
            ],
        }
    ]
    converted = to_openai_messages(history, "")

    assert [m["role"] for m in converted] == ["tool", "tool"]
    assert [m["tool_call_id"] for m in converted] == ["call_1", "call_2"]
    assert [m["content"] for m in converted] == ["uno", "dos"]


def test_a_full_round_trip_keeps_the_turns_in_order():
    history = [
        {"role": "user", "content": "revisa GT-10231"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "c1", "name": "netops__lookup_account", "input": {}}
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "{}"}]},
    ]
    assert [m["role"] for m in to_openai_messages(history, "sys")] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]


# --------------------------------------------------------------------------
# Response: OpenAI -> ours
# --------------------------------------------------------------------------


def test_a_text_answer_becomes_a_text_block():
    result = from_openai_response(
        {"choices": [{"message": {"content": "listo"}, "finish_reason": "stop"}]}
    )
    assert result.content == [{"type": "text", "text": "listo"}]
    assert result.stop_reason == "end_turn"


def test_tool_calls_become_tool_use_blocks():
    """finish_reason 'tool_calls' must arrive as the 'tool_use' the loop expects."""
    result = from_openai_response(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_9",
                                "function": {
                                    "name": "netops__list_outages",
                                    "arguments": '{"region": "peten"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    assert result.stop_reason == "tool_use"
    assert result.content == [
        {
            "type": "tool_use",
            "id": "call_9",
            "name": "netops__list_outages",
            "input": {"region": "peten"},
        }
    ]


def test_malformed_arguments_do_not_crash_the_turn():
    """A model that emits broken JSON should fail through the tool-error path."""
    result = from_openai_response(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "t", "arguments": "{not json"}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    assert result.content[0]["type"] == "tool_use"
    assert "__malformed_arguments__" in result.content[0]["input"]


def test_an_empty_choices_list_is_an_error():
    with pytest.raises(LLMError, match="no choices"):
        from_openai_response({"choices": []})


# --------------------------------------------------------------------------
# Rate limits and error reporting
# --------------------------------------------------------------------------


def test_retry_delay_prefers_the_standard_header():
    assert retry_delay(response_with(429, headers={"retry-after": "12"})) == 12.0


def test_retry_delay_is_parsed_out_of_the_message_when_there_is_no_header():
    """Groq writes the wait into the body rather than the header."""
    body = {"error": {"message": "Rate limit reached ... Please try again in 16.08s."}}
    delay = retry_delay(response_with(429, body))
    assert delay is not None and 17.0 <= delay <= 18.0


def test_retry_delay_is_none_when_the_provider_says_nothing():
    assert retry_delay(response_with(429, {"error": {"message": "slow down"}})) is None


def test_a_rate_limited_call_waits_and_succeeds(monkeypatch):
    slept: list[float] = []
    attempts = {"count": 0}

    def fake_post(url, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return response_with(
                429, {"error": {"message": "Please try again in 2s."}}
            )
        return response_with(
            200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    client = OpenAICompatibleClient(
        api_key="k", model="m", base_url="https://x/v1", sleep=slept.append
    )

    result = client.create(messages=[{"role": "user", "content": "hi"}], tools=[], system="")
    assert result.content == [{"type": "text", "text": "ok"}]
    assert attempts["count"] == 2
    assert slept == [3.0]


def test_retries_are_bounded(monkeypatch):
    attempts = {"count": 0}

    def always_limited(url, **kwargs):
        attempts["count"] += 1
        return response_with(429, {"error": {"message": "Please try again in 1s."}})

    monkeypatch.setattr(httpx, "post", always_limited)
    client = OpenAICompatibleClient(
        api_key="k", model="m", base_url="https://x/v1", max_retries=2, sleep=lambda _: None
    )

    with pytest.raises(LLMError, match="Rate limited"):
        client.create(messages=[], tools=[], system="")
    assert attempts["count"] == 3, "one initial attempt plus two retries"


def test_only_rate_limits_are_retried(monkeypatch):
    attempts = {"count": 0}

    def server_error(url, **kwargs):
        attempts["count"] += 1
        return response_with(500, {"error": {"message": "boom"}})

    monkeypatch.setattr(httpx, "post", server_error)
    client = OpenAICompatibleClient(
        api_key="k", model="m", base_url="https://x/v1", sleep=lambda _: None
    )

    with pytest.raises(LLMError, match="server error"):
        client.create(messages=[], tools=[], system="")
    assert attempts["count"] == 1, "a 500 must not be retried"


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "key was rejected"),
        (403, "not allowed"),
        (404, "not found"),
        (429, "Rate limited"),
        (500, "server error"),
    ],
)
def test_http_errors_are_described_in_actionable_terms(status, expected):
    assert expected in describe_http_error(response_with(status, {"error": {"message": "d"}}))


def test_a_local_runtime_needs_no_api_key():
    """Ollama and LM Studio authenticate nobody."""
    client = OpenAICompatibleClient(api_key="", model="m", base_url="http://localhost:11434/v1")
    assert "Authorization" not in client._headers  # noqa: SLF001


def test_tools_are_omitted_when_there_are_none(monkeypatch):
    """Several providers reject an empty tools array outright."""
    captured: dict = {}

    def capture(url, **kwargs):
        captured.update(kwargs["json"])
        return response_with(
            200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    monkeypatch.setattr(httpx, "post", capture)
    OpenAICompatibleClient(api_key="k", model="m", base_url="https://x/v1").create(
        messages=[], tools=[], system=""
    )
    assert "tools" not in captured
