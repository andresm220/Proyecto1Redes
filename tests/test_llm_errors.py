"""F4 tests: turning SDK failures into messages an operator can act on."""

from __future__ import annotations

import anthropic
import httpx
import pytest

from host.llm.anthropic_client import AnthropicClient, LLMError, describe_bad_request

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def response_with(status: int) -> httpx.Response:
    return httpx.Response(status_code=status, request=REQUEST)


def test_a_spent_account_is_named_explicitly():
    """The most common 400 here, and the raw body buries it."""
    exc = anthropic.BadRequestError(
        "Error code: 400 - {'error': {'message': 'Your credit balance is too low "
        "to access the Anthropic API.'}}",
        response=response_with(400),
        body=None,
    )
    described = describe_bad_request(exc)
    assert "no credit left" in described
    assert "/call keep working" in described


def test_other_bad_requests_keep_their_detail():
    exc = anthropic.BadRequestError(
        "max_tokens must be positive", response=response_with(400), body=None
    )
    assert "max_tokens must be positive" in describe_bad_request(exc)


class FailingClient(AnthropicClient):
    """An AnthropicClient whose underlying SDK call always raises."""

    def __init__(self, error: Exception) -> None:
        self.model = "claude-haiku-4-5-20251001"
        self.max_tokens = 1024
        self._error = error
        self._client = self  # create() below stands in for the SDK

    class _Messages:
        def __init__(self, error: Exception) -> None:
            self._error = error

        def create(self, **_kwargs):
            raise self._error

    @property
    def messages(self):
        return self._Messages(self._error)


@pytest.mark.parametrize(
    "error,expected",
    [
        (
            anthropic.AuthenticationError("bad key", response=response_with(401), body=None),
            "key was rejected",
        ),
        (
            anthropic.NotFoundError("no model", response=response_with(404), body=None),
            "does not exist",
        ),
        (
            anthropic.RateLimitError("slow down", response=response_with(429), body=None),
            "Rate limited",
        ),
        (
            anthropic.APIConnectionError(request=REQUEST),
            "Could not reach the API",
        ),
    ],
)
def test_sdk_errors_become_actionable_llm_errors(error, expected):
    client = FailingClient(error)
    with pytest.raises(LLMError, match=expected):
        client.create(messages=[], tools=[], system="")
