"""Every MCP message, in both directions, on one line of JSON.

This log is a deliverable, not a debugging aid: it is the evidence shown in the
demo and the raw material the report's protocol analysis is written from. Three
consequences follow from that, and they explain most of the design here.

First, the payload is stored whole. A truncated message cannot be replayed or
quoted, so the size of a `tools/list` response is not a reason to trim it.

Second, every line is flushed as it is written. A session that ends in a crash
is exactly the session worth reading afterwards, so nothing may sit in a
buffer waiting for a clean shutdown.

Third, the message type is derived from the envelope rather than declared by
the caller - `method` plus `id` is a request, `method` alone a notification,
`result` a response, `error` an error. The classification is the same one
`jsonrpc.parse_message` makes, applied to a message that has already been
accepted, so it must not raise on a malformed one.

Durations are correlated by id, not measured per line. A request starts a
timer; the response carrying the same id stops it. That pairing is what makes
the log answer "how long did this tool take" instead of merely "when did these
bytes move".

The timer is `perf_counter`, not `monotonic`: on Windows `monotonic` advances
in steps of about 15 ms, which rounds a local stdio round trip to either 0 ms
or 15 ms and makes the field worthless for exactly the calls it should
measure.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable

from host.mcp.jsonrpc import MessageId

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"

# The direction a message travelled, as the assignment names it.
DIRECTION_OUT = "out"
DIRECTION_IN = "in"

# MCPClient's trace callback speaks the transport's verbs; the log format
# speaks of direction. Both spellings are accepted so neither side has to
# translate at the call site.
_DIRECTIONS = {
    "send": DIRECTION_OUT,
    "out": DIRECTION_OUT,
    "recv": DIRECTION_IN,
    "in": DIRECTION_IN,
}
_OPPOSITE = {DIRECTION_OUT: DIRECTION_IN, DIRECTION_IN: DIRECTION_OUT}

# The four message types, which are the categories the capture analysis uses.
TYPE_REQUEST = "request"
TYPE_RESPONSE = "response"
TYPE_NOTIFICATION = "notification"
TYPE_ERROR = "error"
TYPE_UNKNOWN = "unknown"

TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"

DEFAULT_BUFFER_SIZE = 500

# A correlation key: ids restart at 1 in every session, so two servers can
# legitimately have a request 1 in flight at the same time.
_PendingKey = tuple[str, str, MessageId]


def classify(message: dict[str, Any]) -> str:
    """Name the JSON-RPC message type from the fields the envelope carries.

    Deliberately total: an envelope this function cannot recognise is still
    worth a log line, so it returns TYPE_UNKNOWN rather than raising.
    """
    if "method" in message:
        return TYPE_REQUEST if "id" in message else TYPE_NOTIFICATION
    if "error" in message:
        return TYPE_ERROR
    if "result" in message:
        return TYPE_RESPONSE
    return TYPE_UNKNOWN


@dataclass(frozen=True)
class LogEvent:
    """One logged message. Immutable: the record of what happened cannot move."""

    ts: str
    direction: str
    server: str
    transport: str
    type: str
    method: str | None
    id: MessageId | None
    payload: dict[str, Any] = field(default_factory=dict)
    duration_ms: float | None = None
    # The method of the request this message answers. Derived, not received,
    # so it stays out of the file and only feeds the console view - a response
    # rendered as "response id=4" is unreadable in a live trace.
    request_method: str | None = None

    @property
    def arrow(self) -> str:
        """Direction as a shape.

        Colour alone would exclude a colour-blind reader and vanish in a
        printed report, so every rendering of an event carries this too.
        """
        return "->" if self.direction == DIRECTION_OUT else "<-"

    @property
    def label(self) -> str:
        """The most informative name available for this message."""
        return self.method or self.request_method or self.type

    def summary(self) -> str:
        """One compact line, for `/log tail` and the live trace panel."""
        parts = [self.arrow, self.server, self.label]
        if self.id is not None:
            parts.append(f"id={self.id}")
        if self.duration_ms is not None:
            parts.append(f"{self.duration_ms:.1f} ms")
        return "  ".join(parts)

    def to_json(self) -> dict[str, Any]:
        """The on-disk shape, with the fields in the order the spec lists them."""
        entry: dict[str, Any] = {
            "ts": self.ts,
            "direction": self.direction,
            "server": self.server,
            "transport": self.transport,
            "type": self.type,
            "method": self.method,
            "id": self.id,
            "payload": self.payload,
        }
        # Only responses have one; a null on every other line would be noise.
        if self.duration_ms is not None:
            entry["duration_ms"] = self.duration_ms
        return entry


class McpLogger:
    """Writes the JSONL session log and keeps the tail of it in memory.

    Safe to call from several threads: each MCPClient traces from its own
    reader thread while the thread that sent the request traces from there.
    """

    def __init__(
        self,
        log_dir: Path | None = None,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        now: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
        on_event: Callable[[LogEvent], None] | None = None,
    ) -> None:
        self._now = now or _utc_now
        self._clock = clock or perf_counter
        self._on_event = on_event

        self.log_dir = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / f"mcp-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"

        self._events: deque[LogEvent] = deque(maxlen=buffer_size)
        self._pending: dict[_PendingKey, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self._file = self.path.open("a", encoding="utf-8")
        self._closed = False

    # -- recording ---------------------------------------------------------

    def record(
        self,
        server: str,
        direction: str,
        message: dict[str, Any],
        transport: str = TRANSPORT_STDIO,
    ) -> LogEvent:
        """Log one message and return the event, timing it against its request."""
        normalised = _DIRECTIONS.get(direction, direction)
        message_type = classify(message)
        method = message.get("method")
        message_id = message.get("id")

        with self._lock:
            duration_ms, request_method = self._correlate(
                server, normalised, message_type, message_id, method
            )
            event = LogEvent(
                ts=self._now(),
                direction=normalised,
                server=server,
                transport=transport,
                type=message_type,
                method=method if isinstance(method, str) else None,
                id=message_id,
                payload=message,
                duration_ms=duration_ms,
                request_method=request_method,
            )
            self._events.append(event)
            self._write(event)

        # Outside the lock: a slow renderer must not stall the reader thread.
        if self._on_event is not None:
            self._on_event(event)
        return event

    def _correlate(
        self,
        server: str,
        direction: str,
        message_type: str,
        message_id: Any,
        method: Any,
    ) -> tuple[float | None, str | None]:
        """Start a timer on a request, stop it on the response with that id.

        The key includes the direction the *request* travelled, so a request
        the server sends us can never be mistaken for one we sent it.
        """
        if message_id is None:
            return None, None

        if message_type == TYPE_REQUEST:
            self._pending[(server, direction, message_id)] = (
                self._clock(),
                method if isinstance(method, str) else "",
            )
            return None, None

        if message_type in (TYPE_RESPONSE, TYPE_ERROR):
            started = self._pending.pop((server, _OPPOSITE[direction], message_id), None)
            if started is None:
                # A response to a request that predates this logger, or a
                # duplicate. Neither is a reason to lose the line.
                return None, None
            began_at, request_method = started
            return round((self._clock() - began_at) * 1000, 3), request_method or None

        return None, None

    def _write(self, event: LogEvent) -> None:
        """Append one line and flush it. Caller holds the lock."""
        if self._closed:
            return
        line = json.dumps(event.to_json(), ensure_ascii=False, separators=(",", ":"))
        self._file.write(line + "\n")
        self._file.flush()

    # -- reading -----------------------------------------------------------

    def tail(self, count: int = 20) -> list[LogEvent]:
        """The most recent `count` events, oldest first."""
        with self._lock:
            events = list(self._events)
        return events[-count:] if count > 0 else []

    @property
    def events(self) -> Iterable[LogEvent]:
        with self._lock:
            return list(self._events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    # -- shutdown ----------------------------------------------------------

    def close(self) -> None:
        """Close the file. Safe to call more than once, and safe to race.

        Reader threads are daemons that may trace one last message while the
        CLI is exiting, so `record` after `close` has to be a no-op rather
        than an exception on the way out.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._file.close()

    def __enter__(self) -> "McpLogger":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<McpLogger {self.path.name} {len(self._events)} event(s) buffered>"


def _utc_now() -> str:
    """ISO-8601 with milliseconds, which is the resolution the log needs."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
