"""Shared fixtures.

Every test that touches netops state gets its own data directory, so a test run
never writes to the repository's own state file.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from servers.netops.store import SEED_DIR, NetopsStore

FIXED_NOW = "2026-08-19T12:00:00+00:00"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A private copy of the seed, with no state file yet."""
    target = tmp_path / "data"
    (target / "seed").mkdir(parents=True)
    for name in ("accounts.json", "outages.json"):
        shutil.copy(SEED_DIR / name, target / "seed" / name)
    return target


@pytest.fixture
def store(data_dir: Path) -> NetopsStore:
    """A store on the private data directory, with a frozen clock."""
    return NetopsStore(data_dir=data_dir, now=lambda: FIXED_NOW)


def read_payload(result: dict) -> dict:
    """Pull the JSON payload back out of an MCP text content block."""
    return json.loads(result["content"][0]["text"])
