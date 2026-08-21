"""Tests for provider selection and the CLI paths that depend on configuration.

These exist because a refactor of HostConfig left a stale attribute lookup in
Cli.ask(), which every other test walked straight past: nothing exercised the
CLI with the assistant unconfigured, so a real crash shipped green.
"""

from __future__ import annotations

import pytest

from host.config import (
    OPENAI_COMPATIBLE_PRESETS,
    HostConfig,
    LLMConfig,
    ServerConfig,
    load_llm,
)
from host.llm.anthropic_client import AnthropicClient
from host.llm.openai_compatible import OpenAICompatibleClient
from host.main import Cli


def host_config(llm: LLMConfig) -> HostConfig:
    return HostConfig(
        servers={"netops": ServerConfig(name="netops", command="python", args=[])},
        llm=llm,
    )


# --------------------------------------------------------------------------
# Reading the environment
# --------------------------------------------------------------------------


def test_the_default_provider_is_anthropic(monkeypatch):
    for name in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert load_llm().provider == "anthropic"


def test_groq_reads_its_own_variables_and_gets_a_built_in_base_url(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")

    llm = load_llm()
    assert llm.provider == "groq"
    assert llm.api_key == "gsk-test"
    assert llm.model == "openai/gpt-oss-120b"
    assert llm.base_url == OPENAI_COMPATIBLE_PRESETS["groq"]
    assert llm.is_usable


def test_provider_variables_win_over_the_generic_ones(monkeypatch):
    """Several providers can sit in .env at once without shadowing each other."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_MODEL", "specific")
    monkeypatch.setenv("LLM_MODEL", "generic")
    assert load_llm().model == "specific"


def test_an_explicit_base_url_overrides_the_preset(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:9999/v1")
    assert load_llm().base_url == "http://localhost:9999/v1"


# --------------------------------------------------------------------------
# Whether a configuration can be used, and why not
# --------------------------------------------------------------------------


def test_anthropic_needs_only_a_key():
    assert LLMConfig("anthropic", "claude-haiku-4-5-20251001", api_key="k").is_usable
    assert not LLMConfig("anthropic", "claude-haiku-4-5-20251001").is_usable


def test_a_local_runtime_needs_no_key():
    """Ollama authenticates nobody, so a missing key is not a misconfiguration."""
    llm = LLMConfig("ollama", "qwen2.5", base_url="http://localhost:11434/v1")
    assert llm.is_usable
    assert llm.why_unusable() == ""


def test_a_hosted_provider_without_a_key_is_unusable():
    llm = LLMConfig("groq", "some-model", base_url="https://api.groq.com/openai/v1")
    assert not llm.is_usable
    assert "API key" in llm.why_unusable()


def test_a_provider_without_a_model_is_unusable():
    llm = LLMConfig("groq", "", api_key="k", base_url="https://api.groq.com/openai/v1")
    assert not llm.is_usable
    assert "model" in llm.why_unusable()


def test_why_unusable_is_empty_when_it_is_usable():
    assert LLMConfig("anthropic", "m", api_key="k").why_unusable() == ""


# --------------------------------------------------------------------------
# The CLI
# --------------------------------------------------------------------------


def test_asking_without_a_configured_provider_explains_rather_than_crashing():
    """The regression this file exists for: ask() read an attribute that no
    longer existed, so the CLI died on the first question."""
    cli = Cli(host_config(LLMConfig("anthropic", "claude-haiku-4-5-20251001")))
    assert cli.agent is None

    cli.ask("hola")  # must not raise


def test_asking_with_a_provider_but_no_server_says_so():
    cli = Cli(host_config(LLMConfig("groq", "m", api_key="k", base_url="https://x/v1")))
    cli.ask("hola")  # no server connected, so still no agent; must not raise


@pytest.mark.parametrize(
    "llm,expected",
    [
        (LLMConfig("anthropic", "claude-haiku-4-5-20251001", api_key="k"), AnthropicClient),
        (LLMConfig("groq", "m", api_key="k", base_url="https://x/v1"), OpenAICompatibleClient),
        (LLMConfig("ollama", "m", base_url="http://localhost:11434/v1"), OpenAICompatibleClient),
    ],
)
def test_the_right_adapter_is_built_for_each_provider(llm, expected):
    assert isinstance(Cli(host_config(llm)).build_llm(), expected)


def test_the_openai_adapter_is_given_the_configured_endpoint():
    llm = LLMConfig("groq", "openai/gpt-oss-120b", api_key="k", base_url="https://x/v1")
    client = Cli(host_config(llm)).build_llm()
    assert client.base_url == "https://x/v1"
    assert client.model == "openai/gpt-oss-120b"
