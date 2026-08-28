"""F4 tests: the structured MCP log.

The log is a deliverable in its own right - it is the evidence for the demo and
the raw material for the report - so its shape is pinned down here: one JSON
object per line, every message in both directions, and a duration correlated
back to the request that earned it.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from host.logging.mcp_logger import (
    DIRECTION_IN,
    DIRECTION_OUT,
    TYPE_ERROR,
    TYPE_NOTIFICATION,
    TYPE_REQUEST,
    TYPE_RESPONSE,
    LogEvent,
    McpLogger,
    classify,
)
from host.mcp import jsonrpc as rpc

FIXED_TS = "2026-08-28T12:00:00.000+00:00"


@pytest.fixture
def logger(tmp_path: Path) -> McpLogger:
    """A logger writing into a private directory, with both clocks frozen."""
    ticks = iter(range(0, 100_000))
    return McpLogger(
        log_dir=tmp_path,
        now=lambda: FIXED_TS,
        # One tick per call, in seconds, so durations are exact and readable.
        clock=lambda: float(next(ticks)),
    )


def read_lines(logger: McpLogger) -> list[dict]:
    """Read the JSONL file back, which is how a grader would read it."""
    text = logger.path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Classification: the type is deduced from the envelope, nothing else
# --------------------------------------------------------------------------


def test_a_message_with_method_and_id_is_a_request():
    assert classify(rpc.build_request(1, "tools/list", {})) == TYPE_REQUEST


def test_a_message_with_method_and_no_id_is_a_notification():
    assert classify(rpc.build_notification("notifications/initialized")) == TYPE_NOTIFICATION


def test_a_message_carrying_result_is_a_response():
    assert classify(rpc.build_response(1, {"tools": []})) == TYPE_RESPONSE


def test_a_message_carrying_error_is_an_error():
    assert classify(rpc.build_error(1, rpc.METHOD_NOT_FOUND)) == TYPE_ERROR


def test_an_error_response_is_classified_as_error_not_response():
    """The two are distinguishable only by which field is present. Guard it."""
    assert classify(rpc.build_error(1, rpc.INVALID_PARAMS)) != TYPE_RESPONSE


def test_an_unrecognisable_envelope_is_not_a_crash():
    """A malformed message from a server still deserves a log line."""
    assert classify({"jsonrpc": "2.0"}) == "unknown"


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def test_record_writes_one_json_line_per_message(logger: McpLogger):
    logger.record("netops", "send", rpc.build_request(1, "tools/list", {}))
    logger.record("netops", "recv", rpc.build_response(1, {"tools": []}))

    lines = read_lines(logger)
    assert len(lines) == 2


def test_a_line_carries_every_field_the_assignment_asks_for(logger: McpLogger):
    logger.record("netops", "send", rpc.build_request(7, "tools/call", {"name": "ping"}))

    entry = read_lines(logger)[0]
    assert entry["ts"] == FIXED_TS
    assert entry["direction"] == DIRECTION_OUT
    assert entry["server"] == "netops"
    assert entry["transport"] == "stdio"
    assert entry["type"] == TYPE_REQUEST
    assert entry["method"] == "tools/call"
    assert entry["id"] == 7
    assert entry["payload"] == {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "ping"},
    }


def test_the_payload_is_the_whole_raw_message(logger: McpLogger):
    """Truncating the payload would make the log useless as evidence."""
    result = {"content": [{"type": "text", "text": "x" * 5000}], "isError": False}
    logger.record("netops", "recv", rpc.build_response(1, result))

    assert read_lines(logger)[0]["payload"]["result"] == result


def test_send_and_recv_are_normalised_to_out_and_in(logger: McpLogger):
    """The client's trace callback says send/recv; the log format says out/in."""
    logger.record("netops", "send", rpc.build_notification("ping"))
    logger.record("netops", "recv", rpc.build_notification("ping"))

    assert [entry["direction"] for entry in read_lines(logger)] == [DIRECTION_OUT, DIRECTION_IN]


def test_the_transport_is_recorded_per_server(logger: McpLogger):
    """stdio and http servers share one log, so each line has to say which."""
    logger.record("netops", "send", rpc.build_request(1, "ping"), transport="stdio")
    logger.record("netops-remote", "send", rpc.build_request(1, "ping"), transport="http")

    assert [entry["transport"] for entry in read_lines(logger)] == ["stdio", "http"]


def test_a_notification_carries_no_id(logger: McpLogger):
    logger.record("netops", "send", rpc.build_notification("notifications/initialized"))

    entry = read_lines(logger)[0]
    assert entry["id"] is None
    assert entry["method"] == "notifications/initialized"


def test_a_response_carries_no_method(logger: McpLogger):
    """A response has no method of its own; it is identified by its id."""
    logger.record("netops", "recv", rpc.build_response(4, {}))

    entry = read_lines(logger)[0]
    assert entry["method"] is None
    assert entry["id"] == 4


# --------------------------------------------------------------------------
# Duration: correlated by id, across the pair of messages
# --------------------------------------------------------------------------


def test_a_request_has_no_duration_of_its_own(logger: McpLogger):
    event = logger.record("netops", "send", rpc.build_request(1, "tools/list"))
    assert event.duration_ms is None


def test_a_response_is_timed_from_its_request(logger: McpLogger):
    logger.record("netops", "send", rpc.build_request(1, "tools/list"))  # clock 0
    event = logger.record("netops", "recv", rpc.build_response(1, {}))  # clock 1

    assert event.duration_ms == pytest.approx(1000.0)


def test_an_error_response_is_timed_too(logger: McpLogger):
    logger.record("netops", "send", rpc.build_request(1, "nope"))
    event = logger.record("netops", "recv", rpc.build_error(1, rpc.METHOD_NOT_FOUND))

    assert event.duration_ms == pytest.approx(1000.0)


def test_interleaved_requests_are_timed_against_their_own_request(logger: McpLogger):
    """Ids exist precisely so responses can arrive out of order. Prove it works."""
    logger.record("netops", "send", rpc.build_request(1, "slow"))  # clock 0
    logger.record("netops", "send", rpc.build_request(2, "fast"))  # clock 1
    second = logger.record("netops", "recv", rpc.build_response(2, {}))  # clock 2
    first = logger.record("netops", "recv", rpc.build_response(1, {}))  # clock 3

    assert second.duration_ms == pytest.approx(1000.0)
    assert first.duration_ms == pytest.approx(3000.0)


def test_ids_are_scoped_per_server(logger: McpLogger):
    """Every session numbers its requests from 1, so ids collide across servers."""
    logger.record("netops", "send", rpc.build_request(1, "slow"))  # clock 0
    logger.record("git", "send", rpc.build_request(1, "fast"))  # clock 1
    event = logger.record("git", "recv", rpc.build_response(1, {}))  # clock 2

    assert event.duration_ms == pytest.approx(1000.0)


def test_a_response_with_no_matching_request_has_no_duration(logger: McpLogger):
    event = logger.record("netops", "recv", rpc.build_response(99, {}))
    assert event.duration_ms is None


def test_duration_is_omitted_from_the_line_when_absent(logger: McpLogger):
    """A null duration on every request would be noise in the JSONL."""
    logger.record("netops", "send", rpc.build_request(1, "tools/list"))
    assert "duration_ms" not in read_lines(logger)[0]


# --------------------------------------------------------------------------
# The in-memory buffer behind /log tail
# --------------------------------------------------------------------------


def test_tail_returns_the_most_recent_events_in_order(logger: McpLogger):
    for index in range(1, 6):
        logger.record("netops", "send", rpc.build_request(index, f"method/{index}"))

    assert [event.method for event in logger.tail(2)] == ["method/4", "method/5"]


def test_tail_asks_for_more_than_exists(logger: McpLogger):
    logger.record("netops", "send", rpc.build_request(1, "ping"))
    assert len(logger.tail(20)) == 1


def test_the_buffer_is_bounded_but_the_file_is_not(tmp_path: Path):
    """The file is the deliverable; the buffer only backs /log tail."""
    logger = McpLogger(log_dir=tmp_path, buffer_size=3, now=lambda: FIXED_TS)
    for index in range(1, 11):
        logger.record("netops", "send", rpc.build_request(index, "ping"))

    assert len(logger.tail(100)) == 3
    assert len(read_lines(logger)) == 10


# --------------------------------------------------------------------------
# The file itself
# --------------------------------------------------------------------------


def test_the_file_is_named_for_the_session_start(tmp_path: Path):
    logger = McpLogger(log_dir=tmp_path)
    assert logger.path.parent == tmp_path
    assert logger.path.name.startswith("mcp-")
    assert logger.path.suffix == ".jsonl"


def test_the_log_directory_is_created_on_demand(tmp_path: Path):
    target = tmp_path / "nested" / "logs"
    logger = McpLogger(log_dir=target)
    logger.record("netops", "send", rpc.build_request(1, "ping"))
    assert logger.path.is_file()


def test_each_line_is_flushed_as_it_is_written(logger: McpLogger):
    """A crash must not take the evidence with it, so nothing sits in a buffer."""
    logger.record("netops", "send", rpc.build_request(1, "ping"))
    assert len(read_lines(logger)) == 1  # read while the logger is still open


def test_close_is_idempotent(logger: McpLogger):
    logger.close()
    logger.close()


def test_recording_after_close_does_not_raise(logger: McpLogger):
    """Shutdown races the reader threads; a late message must not crash the exit."""
    logger.close()
    event = logger.record("netops", "recv", rpc.build_response(1, {}))
    assert event.type == TYPE_RESPONSE


# --------------------------------------------------------------------------
# Concurrency: the trace callback is invoked from several threads
# --------------------------------------------------------------------------


def test_concurrent_records_do_not_interleave_lines(tmp_path: Path):
    """Each MCPClient reads on its own thread while the caller writes on another."""
    logger = McpLogger(log_dir=tmp_path)
    workers = 4
    # Every thread starts writing at the same instant, so the lock is really
    # contended rather than the threads running one after another.
    barrier = threading.Barrier(workers)

    def worker(server: str) -> None:
        barrier.wait()
        for index in range(1, 51):
            logger.record(server, "send", rpc.build_request(index, "ping"))

    threads = [threading.Thread(target=worker, args=(f"s{n}",)) for n in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads), "a writer deadlocked"

    lines = read_lines(logger)  # json.loads on every line proves none was torn
    assert len(lines) == 200


# --------------------------------------------------------------------------
# The console view
# --------------------------------------------------------------------------


def test_an_event_renders_a_one_line_summary(logger: McpLogger):
    logger.record("netops", "send", rpc.build_request(1, "tools/call"))
    event = logger.record("netops", "recv", rpc.build_response(1, {}))

    summary = event.summary()
    assert "netops" in summary
    assert "1000" in summary  # the duration, in ms


def test_the_summary_names_the_method_of_the_request_it_answers(logger: McpLogger):
    """A bare response line reading 'response id=1' is unreadable in a trace."""
    logger.record("netops", "send", rpc.build_request(1, "tools/call"))
    event = logger.record("netops", "recv", rpc.build_response(1, {}))

    assert "tools/call" in event.summary()


def test_an_event_knows_its_own_arrow(logger: McpLogger):
    """Direction is carried by shape, not only by colour: the log stays
    readable for a colour-blind reader and in a black-and-white printout."""
    out = logger.record("netops", "send", rpc.build_request(1, "ping"))
    incoming = logger.record("netops", "recv", rpc.build_response(1, {}))

    assert out.arrow == "->"
    assert incoming.arrow == "<-"


def test_log_event_is_immutable():
    event = LogEvent(
        ts=FIXED_TS,
        direction=DIRECTION_OUT,
        server="netops",
        transport="stdio",
        type=TYPE_REQUEST,
        method="ping",
        id=1,
        payload={},
    )
    with pytest.raises(Exception):
        event.server = "other"  # type: ignore[misc]
