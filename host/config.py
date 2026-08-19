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
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


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
class HostConfig:
    servers: dict[str, ServerConfig]
    api_key: str | None
    model: str

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)


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


def load_config(path: Path | None = None) -> HostConfig:
    """Load .env plus the server declarations."""
    load_dotenv(PROJECT_ROOT / ".env")
    return HostConfig(
        servers=load_servers(path),
        api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        model=os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL,
    )
