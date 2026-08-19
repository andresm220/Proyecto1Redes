"""F3 tests: the seven tools against core.py directly, with no protocol involved.

Splitting these from the integration tests means a failure here is a logic bug
and a failure there is a wiring bug.
"""

from __future__ import annotations

import json

import pytest

from host.mcp.jsonrpc import InvalidParamsError
from servers.netops import core
from servers.netops.store import NetopsStore
from tests.conftest import FIXED_NOW, read_payload


def call(store, name, **arguments):
    return core.dispatch(store, name, arguments)


def payload(store, name, **arguments):
    return read_payload(call(store, name, **arguments))


# --------------------------------------------------------------------------
# The tool catalogue
# --------------------------------------------------------------------------


def test_exactly_seven_tools_are_advertised():
    assert [tool["name"] for tool in core.TOOLS] == [
        "lookup_account",
        "check_service_status",
        "list_outages",
        "run_diagnostic",
        "open_ticket",
        "get_ticket",
        "schedule_visit",
    ]


@pytest.mark.parametrize("tool", core.TOOLS, ids=lambda tool: tool["name"])
def test_every_tool_descriptor_is_complete(tool):
    assert tool["title"] and tool["description"]
    schema = tool["inputSchema"]
    assert schema["type"] == "object"
    for name, definition in schema["properties"].items():
        assert definition.get("description"), f"{tool['name']}.{name} has no description"
    for required in schema.get("required", []):
        assert required in schema["properties"]


def test_unknown_tool_is_invalid_params():
    with pytest.raises(InvalidParamsError, match="unknown tool"):
        core.dispatch(None, "delete_everything", {})


# --------------------------------------------------------------------------
# lookup_account
# --------------------------------------------------------------------------


def test_lookup_by_account_id(store):
    result = payload(store, "lookup_account", account_id="GT-10231")
    assert result["plan"] == "Fibra 300"
    assert result["status"] == "active"
    assert result["service_address"].startswith("12 Avenida 5-43")


def test_lookup_by_phone_ignores_formatting(store):
    """Digits are what identify a number, not the punctuation around them."""
    for spelling in ("+502 5555-0101", "50255550101", "(502) 5555 0101"):
        assert payload(store, "lookup_account", phone=spelling)["account_id"] == "GT-10231"


def test_lookup_needs_exactly_one_identifier(store):
    with pytest.raises(InvalidParamsError, match="exactly one"):
        call(store, "lookup_account")
    with pytest.raises(InvalidParamsError, match="exactly one"):
        call(store, "lookup_account", account_id="GT-10231", phone="+502 5555-0101")


def test_lookup_of_a_missing_account_is_a_domain_error(store):
    """Not a protocol failure: the call was well formed, the account is not there."""
    result = call(store, "lookup_account", account_id="GT-99999")
    assert result["isError"] is True
    assert "No se encontró" in read_payload(result)["error"]


# --------------------------------------------------------------------------
# check_service_status
# --------------------------------------------------------------------------


def test_status_of_a_healthy_account(store):
    result = payload(store, "check_service_status", account_id="GT-10234")
    assert result["link_state"] in ("up", "degraded")
    assert set(result["metrics"]) == {
        "latency_ms",
        "packet_loss_pct",
        "snr_db",
        "downstream_mbps",
        "upstream_mbps",
    }


def test_metrics_are_deterministic(store):
    """Derived from a hash of the account id, not random, so demos repeat."""
    first = payload(store, "check_service_status", account_id="GT-10231")
    second = payload(store, "check_service_status", account_id="GT-10231")
    assert first["metrics"] == second["metrics"]

    other = payload(store, "check_service_status", account_id="GT-10234")
    assert other["metrics"] != first["metrics"], "different accounts must differ"


def test_suspended_account_reports_billing_not_a_fault(store):
    result = payload(store, "check_service_status", account_id="GT-10232")
    assert result["link_state"] == "down"
    assert result["reason"] == "administrative_suspension"


def test_account_in_an_outage_region_reports_the_outage(store):
    """GT-10233 is in Peten, where OUT-2026-013 is active."""
    result = payload(store, "check_service_status", account_id="GT-10233")
    assert result["reason"] == "mass_outage"
    assert result["outage_id"] == "OUT-2026-013"
    assert result["eta"]


def test_status_requires_an_account_id(store):
    with pytest.raises(InvalidParamsError, match="required"):
        call(store, "check_service_status")


# --------------------------------------------------------------------------
# list_outages
# --------------------------------------------------------------------------


def test_outages_default_to_active_only(store):
    result = payload(store, "list_outages")
    assert result["count"] == 2
    assert {item["status"] for item in result["outages"]} == {"active"}


def test_outages_can_include_resolved(store):
    result = payload(store, "list_outages", active_only=False)
    assert result["count"] == 3


def test_outages_filter_by_region(store):
    result = payload(store, "list_outages", region="peten")
    assert [item["outage_id"] for item in result["outages"]] == ["OUT-2026-013"]


def test_unknown_region_is_rejected_by_the_enum(store):
    with pytest.raises(InvalidParamsError, match="must be one of"):
        call(store, "list_outages", region="antarctica")


def test_active_only_must_be_a_boolean(store):
    with pytest.raises(InvalidParamsError, match="expected boolean"):
        call(store, "list_outages", active_only="yes")


# --------------------------------------------------------------------------
# run_diagnostic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "test_type,expected",
    [
        ("ping", {"latency_ms", "jitter_ms", "packet_loss_pct", "packets_sent"}),
        ("speed", {"downstream_mbps", "upstream_mbps", "contracted_mbps", "pct_of_plan"}),
        ("line", {"snr_db", "attenuation_db", "technology", "sync_errors_last_hour"}),
    ],
)
def test_each_diagnostic_returns_its_own_readings(store, test_type, expected):
    result = payload(store, "run_diagnostic", account_id="GT-10231", test_type=test_type)
    assert set(result["readings"]) == expected
    assert result["probable_cause"]


def test_diagnostic_blames_the_outage_when_there_is_one(store):
    result = payload(store, "run_diagnostic", account_id="GT-10233", test_type="ping")
    assert "OUT-2026-013" in result["probable_cause"]


def test_unknown_test_type_is_rejected(store):
    with pytest.raises(InvalidParamsError, match="must be one of"):
        call(store, "run_diagnostic", account_id="GT-10231", test_type="traceroute")


# --------------------------------------------------------------------------
# Tickets and visits: the persistent state
# --------------------------------------------------------------------------


def test_open_ticket_then_get_ticket_is_consistent(store):
    opened = payload(
        store,
        "open_ticket",
        account_id="GT-10231",
        category="connectivity",
        description="Se corta el internet cada 10 minutos desde ayer.",
        priority="high",
    )
    assert opened["ticket_id"] == "TCK-00001"
    assert opened["status"] == "open"

    fetched = payload(store, "get_ticket", ticket_id="TCK-00001")
    assert fetched["account_id"] == "GT-10231"
    assert fetched["priority"] == "high"
    assert fetched["description"].startswith("Se corta el internet")
    assert fetched["history"][0]["status"] == "open"


def test_ticket_ids_increment(store):
    for expected in ("TCK-00001", "TCK-00002", "TCK-00003"):
        result = payload(
            store,
            "open_ticket",
            account_id="GT-10231",
            category="speed",
            description="Velocidad por debajo de lo contratado.",
        )
        assert result["ticket_id"] == expected


def test_priority_defaults_to_normal(store):
    result = payload(
        store,
        "open_ticket",
        account_id="GT-10231",
        category="equipment",
        description="El router no enciende.",
    )
    assert result["priority"] == "normal"


def test_state_survives_a_restart(data_dir):
    """The acceptance criterion for persistence: a new process sees the ticket."""
    first = NetopsStore(data_dir=data_dir, now=lambda: FIXED_NOW)
    opened = read_payload(
        core.dispatch(
            first,
            "open_ticket",
            {
                "account_id": "GT-10231",
                "category": "connectivity",
                "description": "Sin servicio desde la madrugada.",
            },
        )
    )

    reopened = NetopsStore(data_dir=data_dir, now=lambda: FIXED_NOW)
    fetched = read_payload(
        core.dispatch(reopened, "get_ticket", {"ticket_id": opened["ticket_id"]})
    )
    assert fetched["ticket_id"] == opened["ticket_id"]
    assert fetched["description"] == "Sin servicio desde la madrugada."


def test_the_seed_is_never_written_to(store, data_dir):
    before = (data_dir / "seed" / "accounts.json").read_bytes()
    call(
        store,
        "open_ticket",
        account_id="GT-10231",
        category="billing",
        description="Cobro duplicado en el recibo de agosto.",
    )
    assert (data_dir / "seed" / "accounts.json").read_bytes() == before


def test_state_writes_leave_no_temporary_files_behind(store, data_dir):
    call(
        store,
        "open_ticket",
        account_id="GT-10231",
        category="billing",
        description="Cobro duplicado en el recibo de agosto.",
    )
    leftovers = list(data_dir.glob("state-*.tmp"))
    assert leftovers == [], f"atomic write left temporary files: {leftovers}"
    assert json.loads((data_dir / "state.json").read_text(encoding="utf-8"))["tickets"]


def test_ticket_for_a_missing_account_is_a_domain_error(store):
    result = call(
        store,
        "open_ticket",
        account_id="GT-99999",
        category="connectivity",
        description="Reporta que no tiene servicio.",
    )
    assert result["isError"] is True
    assert "GT-99999" in read_payload(result)["error"]


def test_description_below_the_minimum_length_is_rejected(store):
    with pytest.raises(InvalidParamsError, match="at least 5"):
        call(store, "open_ticket", account_id="GT-10231", category="speed", description="no")


def test_unknown_category_is_rejected(store):
    with pytest.raises(InvalidParamsError, match="must be one of"):
        call(
            store,
            "open_ticket",
            account_id="GT-10231",
            category="teleportation",
            description="Solicita teletransporte.",
        )


def test_get_of_a_missing_ticket_is_a_domain_error(store):
    result = call(store, "get_ticket", ticket_id="TCK-99999")
    assert result["isError"] is True


# --------------------------------------------------------------------------
# schedule_visit
# --------------------------------------------------------------------------


@pytest.fixture
def ticket_id(store):
    return payload(
        store,
        "open_ticket",
        account_id="GT-10231",
        category="connectivity",
        description="Necesita revisión en sitio.",
    )["ticket_id"]


def test_schedule_a_visit(store, ticket_id):
    result = payload(
        store,
        "schedule_visit",
        ticket_id=ticket_id,
        date="2026-08-25",
        time_window="08:00-12:00",
    )
    assert result["status"] == "scheduled"
    assert result["rescheduled"] is False

    ticket = payload(store, "get_ticket", ticket_id=ticket_id)
    assert ticket["status"] == "scheduled"
    assert ticket["visit"]["date"] == "2026-08-25"
    assert ticket["history"][-1]["status"] == "scheduled"


def test_scheduling_twice_reschedules(store, ticket_id):
    call(store, "schedule_visit", ticket_id=ticket_id, date="2026-08-25", time_window="08:00-12:00")
    result = payload(
        store,
        "schedule_visit",
        ticket_id=ticket_id,
        date="2026-08-26",
        time_window="12:00-16:00",
    )
    assert result["rescheduled"] is True
    assert result["date"] == "2026-08-26"


def test_a_malformed_date_is_a_protocol_error(store, ticket_id):
    """The shape is a schema concern, so it is -32602."""
    with pytest.raises(InvalidParamsError, match="must match"):
        call(store, "schedule_visit", ticket_id=ticket_id, date="25/08/2026", time_window="08:00-12:00")


def test_a_date_that_is_not_real_is_a_protocol_error(store, ticket_id):
    """2026-02-30 has the right shape but does not exist."""
    with pytest.raises(InvalidParamsError, match="not a real calendar date"):
        call(store, "schedule_visit", ticket_id=ticket_id, date="2026-02-30", time_window="08:00-12:00")


def test_a_past_date_is_a_domain_error(store, ticket_id):
    """The argument is valid; the business rule is what rejects it."""
    result = call(
        store, "schedule_visit", ticket_id=ticket_id, date="2020-01-01", time_window="08:00-12:00"
    )
    assert result["isError"] is True
    assert read_payload(result)["earliest"] == "2026-08-19"


def test_unknown_time_window_is_rejected(store, ticket_id):
    with pytest.raises(InvalidParamsError, match="must be one of"):
        call(store, "schedule_visit", ticket_id=ticket_id, date="2026-08-25", time_window="madrugada")


def test_scheduling_against_a_missing_ticket_is_a_domain_error(store):
    result = call(
        store, "schedule_visit", ticket_id="TCK-99999", date="2026-08-25", time_window="08:00-12:00"
    )
    assert result["isError"] is True
    assert "open_ticket" in read_payload(result)["error"]
