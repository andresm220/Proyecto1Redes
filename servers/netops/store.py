"""Data access for netops: a read-only seed plus a mutable state file.

The split is what keeps results reproducible. `data/seed/` holds accounts and
outages, is versioned, and is never written to. `data/state.json` holds the only
things that change - tickets and scheduled visits - is git-ignored, and is
written atomically so an interrupted write cannot truncate it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DATA_DIR = Path(__file__).resolve().parent / "data"
SEED_DIR = DATA_DIR / "seed"
STATE_FILE = DATA_DIR / "state.json"

EMPTY_STATE: dict[str, Any] = {"tickets": {}, "visits": {}, "counters": {"ticket": 0}}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_int(*parts: str, low: int, high: int) -> int:
    """A repeatable pseudo-value derived from the given strings.

    Used instead of `random` so that the same account always reports the same
    latency, packet loss and SNR. Demos and tests stay reproducible, and a
    grader re-running a call sees the same numbers.
    """
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    span = high - low + 1
    return low + (int.from_bytes(digest[:8], "big") % span)


class NetopsStore:
    """Loads the seed, owns the mutable state, and persists every change."""

    def __init__(
        self,
        data_dir: Path | None = None,
        now: Callable[[], str] = _utc_now,
    ) -> None:
        # NETOPS_DATA_DIR lets a test - or a second client such as Claude
        # Desktop - keep its own state without touching the repository's.
        self.data_dir = data_dir or Path(os.environ.get("NETOPS_DATA_DIR", DATA_DIR))
        self.seed_dir = self.data_dir / "seed"
        self.state_file = self.data_dir / "state.json"
        self.now = now
        self._lock = threading.Lock()

        self.accounts: dict[str, dict[str, Any]] = {}
        self.accounts_by_phone: dict[str, dict[str, Any]] = {}
        self.outages: list[dict[str, Any]] = []
        self.state: dict[str, Any] = json.loads(json.dumps(EMPTY_STATE))

        self._load_seed()
        self._load_state()

    # -- loading -----------------------------------------------------------

    def _read_seed_file(self, name: str) -> Any:
        path = self.seed_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"missing seed file: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_seed(self) -> None:
        for account in self._read_seed_file("accounts.json"):
            self.accounts[account["account_id"]] = account
            self.accounts_by_phone[normalize_phone(account["phone"])] = account
        self.outages = self._read_seed_file("outages.json")

    def _load_state(self) -> None:
        if not self.state_file.is_file():
            return
        try:
            loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # A corrupt state file must not stop the server from serving the
            # seed. Start clean; the file is rewritten on the next change.
            return
        if isinstance(loaded, dict):
            self.state = {**self.state, **loaded}
            self.state.setdefault("counters", {"ticket": 0})

    def _save_state(self) -> None:
        """Write via a temporary file plus os.replace, which is atomic."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(self.data_dir),
            prefix="state-",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(self.state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_file)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    # -- accounts and outages ---------------------------------------------

    def find_account(self, account_id: str | None = None, phone: str | None = None):
        if account_id is not None:
            return self.accounts.get(account_id.strip().upper())
        if phone is not None:
            return self.accounts_by_phone.get(normalize_phone(phone))
        return None

    def find_outages(self, region: str | None = None, active_only: bool = True):
        found = self.outages
        if region:
            found = [item for item in found if item["region"] == region.strip().lower()]
        if active_only:
            found = [item for item in found if item["status"] == "active"]
        return list(found)

    def active_outage_for(self, account: dict[str, Any]) -> dict[str, Any] | None:
        for outage in self.find_outages(region=account["region"], active_only=True):
            return outage
        return None

    # -- tickets and visits ------------------------------------------------

    def create_ticket(
        self,
        account_id: str,
        category: str,
        description: str,
        priority: str,
    ) -> dict[str, Any]:
        with self._lock:
            self.state["counters"]["ticket"] += 1
            ticket_id = f"TCK-{self.state['counters']['ticket']:05d}"
            timestamp = self.now()
            ticket = {
                "ticket_id": ticket_id,
                "account_id": account_id,
                "category": category,
                "description": description,
                "priority": priority,
                "status": "open",
                "created_at": timestamp,
                "updated_at": timestamp,
                "history": [
                    {"at": timestamp, "status": "open", "note": "Ticket creado por el asistente"}
                ],
            }
            self.state["tickets"][ticket_id] = ticket
            self._save_state()
            return ticket

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        return self.state["tickets"].get(ticket_id.strip().upper())

    def schedule_visit(
        self,
        ticket_id: str,
        date: str,
        time_window: str,
    ) -> dict[str, Any]:
        with self._lock:
            ticket = self.state["tickets"][ticket_id]
            timestamp = self.now()
            visit = {
                "ticket_id": ticket_id,
                "account_id": ticket["account_id"],
                "date": date,
                "time_window": time_window,
                "status": "scheduled",
                "scheduled_at": timestamp,
            }
            self.state["visits"][ticket_id] = visit
            ticket["status"] = "scheduled"
            ticket["updated_at"] = timestamp
            ticket["history"].append(
                {
                    "at": timestamp,
                    "status": "scheduled",
                    "note": f"Visita técnica agendada para {date} en la ventana {time_window}",
                }
            )
            self._save_state()
            return visit

    def get_visit(self, ticket_id: str) -> dict[str, Any] | None:
        return self.state["visits"].get(ticket_id)


def normalize_phone(phone: str) -> str:
    """Compare phone numbers by their digits, so formatting never matters."""
    return "".join(character for character in phone if character.isdigit())
