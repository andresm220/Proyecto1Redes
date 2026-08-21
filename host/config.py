"""Configuration loading for the MCP host.

Reads two things:
  - the process environment (via .env), for the Anthropic API key and model
  - config/servers.json, which declares the MCP servers to launch over stdio
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SERVERS_CONFIG = PROJECT_ROOT / "config" / "servers.json"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

# Providers that speak OpenAI's /chat/completions, and where they live.
OPENAI_COMPATIBLE_PRESETS = {
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
}
# A local runtime authenticates nobody, so a missing key is not a misconfiguration.
KEYLESS_PROVIDERS = {"ollama", "lmstudio"}


class ConfigError(Exception):
    """Raised when configuration is missing or malformed."""


@dataclass(frozen=True)
class ServerConfig:
    """How to launch one MCP server as a subprocess."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMConfig:
    """Which model backend the agentic loop should use, and how to reach it."""

    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None

    @property
    def is_openai_compatible(self) -> bool:
        return self.provider != "anthropic"

    @property
    def is_usable(self) -> bool:
        """Enough configuration to attempt a call."""
        if not self.model:
            return False
        if self.provider in KEYLESS_PROVIDERS:
            return bool(self.base_url)
        if self.is_openai_compatible:
            return bool(self.api_key and self.base_url)
        return bool(self.api_key)

    def why_unusable(self) -> str:
        """What the operator has to fix, named precisely."""
        if self.provider in KEYLESS_PROVIDERS:
            if not self.base_url:
                return f"LLM_BASE_URL is not set for provider {self.provider!r}"
        elif not self.api_key:
            variable = "ANTHROPIC_API_KEY" if self.provider == "anthropic" else "the provider's API key"
            return f"{variable} is not set"
        if self.is_openai_compatible and not self.base_url:
            return f"LLM_BASE_URL is not set for provider {self.provider!r}"
        if not self.model:
            return f"no model configured for provider {self.provider!r}"
        return ""


@dataclass(frozen=True)
class HostConfig:
    servers: dict[str, ServerConfig]
    llm: LLMConfig


def load_servers(path: Path | None = None) -> dict[str, ServerConfig]:
    """Parse config/servers.json into ServerConfig objects."""
    config_path = path or DEFAULT_SERVERS_CONFIG
    if not config_path.is_file():
        raise ConfigError(f"server config not found: {config_path}")

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid JSON: {exc}") from exc

    entries = raw.get("mcpServers")
    if not isinstance(entries, dict):
        raise ConfigError(f"{config_path}: expected an 'mcpServers' object")

    servers: dict[str, ServerConfig] = {}
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"{config_path}: server '{name}' must be an object")
        command = entry.get("command")
        if not isinstance(command, str) or not command:
            raise ConfigError(f"{config_path}: server '{name}' needs a 'command' string")
        args = entry.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ConfigError(f"{config_path}: server '{name}' args must be a list of strings")
        env = entry.get("env", {})
        if not isinstance(env, dict):
            raise ConfigError(f"{config_path}: server '{name}' env must be an object")
        servers[name] = ServerConfig(
            name=name,
            command=command,
            args=list(args),
            env={str(k): str(v) for k, v in env.items()},
        )

    if not servers:
        raise ConfigError(f"{config_path}: no servers declared")
    return servers


def load_llm() -> LLMConfig:
    """Resolve the model backend from the environment.

    Each provider reads its own variables so several can sit in .env at once and
    switching is a one-line change to LLM_PROVIDER.
    """
    provider = (os.environ.get("LLM_PROVIDER") or "anthropic").strip().lower()

    if provider == "anthropic":
        return LLMConfig(
            provider=provider,
            model=os.environ.get("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL,
            api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        )

    # Every other provider speaks the OpenAI dialect; only the address differs.
    prefix = provider.upper()
    return LLMConfig(
        provider=provider,
        model=os.environ.get(f"{prefix}_MODEL") or os.environ.get("LLM_MODEL") or "",
        api_key=(
            os.environ.get(f"{prefix}_API_KEY") or os.environ.get("LLM_API_KEY") or None
        ),
        base_url=(
            os.environ.get(f"{prefix}_BASE_URL")
            or os.environ.get("LLM_BASE_URL")
            or OPENAI_COMPATIBLE_PRESETS.get(provider)
        ),
    )


def load_config(path: Path | None = None) -> HostConfig:
    """Load .env plus the server declarations."""
    load_dotenv(PROJECT_ROOT / ".env")
    return HostConfig(servers=load_servers(path), llm=load_llm())
