"""netops: ISP technical support. Business logic and tool schemas.

This module is transport-agnostic on purpose: it knows nothing about JSON-RPC,
stdio or HTTP. It exposes TOOLS (the descriptors that tools/list returns) and
dispatch() (what tools/call runs). The *_server.py modules adapt a transport
onto this surface, which is what lets the remote phase add HTTP without moving
any logic.

Two failure kinds, deliberately kept apart:

  * A call that does not satisfy the advertised schema raises
    InvalidParamsError, which the transport turns into a JSON-RPC -32602. The
    exchange itself was malformed.
  * A call that is well formed but cannot succeed - an account that does not
    exist, a ticket that was never opened - raises DomainError, which becomes a
    *successful* result carrying `isError: true`. The model is expected to read
    it and react, so it is data, not a protocol failure.
"""

from __future__ import annotations

import json
from datetime import date as date_type
from typing import Any, Callable

from host.mcp.jsonrpc import InvalidParamsError
from servers.netops.store import NetopsStore, stable_int
from servers.netops.validation import require_one_of, validate_arguments

SERVER_NAME = "netops"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2025-11-25"

INSTRUCTIONS = (
    "Technical support back office for a Guatemalan ISP. Look up subscriber "
    "accounts, read live link metrics, check whether a regional outage already "
    "explains a complaint, run diagnostics, and open or follow up support "
    "tickets. Always check list_outages before opening a ticket for a "
    "connectivity complaint: if a mass outage already covers the subscriber's "
    "region, the ETA is the answer and a new ticket only adds noise."
)

REGIONS = ["guatemala", "quetzaltenango", "peten", "escuintla"]
CATEGORIES = ["connectivity", "speed", "billing", "equipment", "installation"]
PRIORITIES = ["low", "normal", "high", "critical"]
TIME_WINDOWS = ["08:00-12:00", "12:00-16:00", "16:00-20:00"]
TEST_TYPES = ["ping", "speed", "line"]

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


class DomainError(Exception):
    """A well-formed call that cannot succeed. Becomes isError: true."""

    def __init__(self, message: str, **details: Any) -> None:
        self.message = message
        self.details = details
        super().__init__(message)

    def to_payload(self) -> dict[str, Any]:
        return {"error": self.message, **self.details}


# --------------------------------------------------------------------------
# Tool descriptors
# --------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "lookup_account",
        "title": "Look up a subscriber account",
        "description": (
            "Find a subscriber by account id or by phone number. Returns the plan, "
            "the account status and the service address. Exactly one of account_id "
            "or phone must be supplied."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "string",
                    "description": "Account id, for example GT-10231.",
                    "minLength": 3,
                },
                "phone": {
                    "type": "string",
                    "description": "Phone number in any format; only digits are compared.",
                    "minLength": 8,
                },
            },
        },
    },
    {
        "name": "check_service_status",
        "title": "Check service status",
        "description": (
            "Read the current link state for one account: latency, packet loss and "
            "SNR. If a mass outage covers the account's region, it is reported here."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Account id.", "minLength": 3}
            },
            "required": ["account_id"],
        },
    },
    {
        "name": "list_outages",
        "title": "List mass outages",
        "description": (
            "List mass outages with their cause and estimated time of repair. "
            "Filter by region, and by default only outages that are still active."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "Restrict to one region. Omit for every region.",
                    "enum": REGIONS,
                },
                "active_only": {
                    "type": "boolean",
                    "description": "Only outages still in progress. Defaults to true.",
                },
            },
        },
    },
    {
        "name": "run_diagnostic",
        "title": "Run a line diagnostic",
        "description": (
            "Run a diagnostic against a subscriber line and return the measurements "
            "together with a probable cause. ping measures reachability, speed "
            "measures throughput against the contracted plan, line reads the "
            "physical layer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Account id.", "minLength": 3},
                "test_type": {
                    "type": "string",
                    "description": "Which diagnostic to run.",
                    "enum": TEST_TYPES,
                },
            },
            "required": ["account_id", "test_type"],
        },
    },
    {
        "name": "open_ticket",
        "title": "Open a support ticket",
        "description": (
            "Open a support ticket against an account and return its id. Check "
            "list_outages first: if a mass outage already covers the region, the "
            "ETA answers the complaint and a ticket is not needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Account id.", "minLength": 3},
                "category": {
                    "type": "string",
                    "description": "What the ticket is about.",
                    "enum": CATEGORIES,
                },
                "description": {
                    "type": "string",
                    "description": "What the subscriber reported, in their own terms.",
                    "minLength": 5,
                    "maxLength": 2000,
                },
                "priority": {
                    "type": "string",
                    "description": "Ticket priority. Defaults to normal.",
                    "enum": PRIORITIES,
                },
            },
            "required": ["account_id", "category", "description"],
        },
    },
    {
        "name": "get_ticket",
        "title": "Read a ticket",
        "description": "Read a ticket's current status, its history, and any scheduled visit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "description": "Ticket id, for example TCK-00001.",
                    "minLength": 5,
                }
            },
            "required": ["ticket_id"],
        },
    },
    {
        "name": "schedule_visit",
        "title": "Schedule a technician visit",
        "description": (
            "Schedule a technician visit against an existing ticket. The date must "
            "not be in the past. Scheduling again reschedules the existing visit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "Ticket id.", "minLength": 5},
                "date": {
                    "type": "string",
                    "description": "Visit date as YYYY-MM-DD.",
                    "pattern": DATE_PATTERN,
                },
                "time_window": {
                    "type": "string",
                    "description": "Arrival window.",
                    "enum": TIME_WINDOWS,
                },
            },
            "required": ["ticket_id", "date", "time_window"],
        },
    },
]

TOOLS_BY_NAME: dict[str, dict[str, Any]] = {tool["name"]: tool for tool in TOOLS}


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def _account_view(account: dict[str, Any]) -> dict[str, Any]:
    view = {
        "account_id": account["account_id"],
        "holder": account["holder"],
        "phone": account["phone"],
        "plan": account["plan"],
        "plan_speed_mbps": account["plan_speed_mbps"],
        "status": account["status"],
        "technology": account["technology"],
        "region": account["region"],
        "service_address": account["service_address"],
        "installed_on": account["installed_on"],
    }
    if "suspension_reason" in account:
        view["suspension_reason"] = account["suspension_reason"]
    return view


def _require_account(store: NetopsStore, account_id: str) -> dict[str, Any]:
    account = store.find_account(account_id=account_id)
    if account is None:
        raise DomainError(
            f"No existe la cuenta {account_id}.",
            account_id=account_id,
            hint="Verifique el id con lookup_account, o busque por teléfono.",
        )
    return account


def _link_metrics(account: dict[str, Any]) -> dict[str, Any]:
    """Deterministic per-account metrics, so a repeated call reads the same."""
    account_id = account["account_id"]
    return {
        "latency_ms": stable_int(account_id, "latency", low=6, high=45),
        "packet_loss_pct": round(stable_int(account_id, "loss", low=0, high=20) / 10, 1),
        "snr_db": round(stable_int(account_id, "snr", low=240, high=380) / 10, 1),
        "downstream_mbps": round(
            account["plan_speed_mbps"] * stable_int(account_id, "down", low=82, high=99) / 100, 1
        ),
        "upstream_mbps": round(
            account["plan_speed_mbps"] * stable_int(account_id, "up", low=40, high=52) / 100, 1
        ),
    }


def handle_lookup_account(store: NetopsStore, arguments: dict[str, Any]) -> dict[str, Any]:
    supplied = require_one_of(arguments, ["account_id", "phone"])
    account = store.find_account(**{supplied: arguments[supplied]})
    if account is None:
        raise DomainError(
            f"No se encontró ninguna cuenta con {supplied}={arguments[supplied]!r}.",
            **{supplied: arguments[supplied]},
        )
    return _account_view(account)


def handle_check_service_status(store: NetopsStore, arguments: dict[str, Any]) -> dict[str, Any]:
    account = _require_account(store, arguments["account_id"])
    metrics = _link_metrics(account)
    outage = store.active_outage_for(account)

    if account["status"] == "suspended":
        state = {
            "link_state": "down",
            "reason": "administrative_suspension",
            "detail": account.get("suspension_reason", "Cuenta suspendida"),
        }
    elif account["status"] == "pending_installation":
        state = {
            "link_state": "not_provisioned",
            "reason": "pending_installation",
            "detail": "La instalación todavía no se ha realizado.",
        }
    elif outage is not None:
        state = {
            "link_state": "down",
            "reason": "mass_outage",
            "detail": outage["cause"],
            "outage_id": outage["outage_id"],
            "eta": outage["eta"],
        }
    elif metrics["packet_loss_pct"] >= 1.5:
        state = {
            "link_state": "degraded",
            "reason": "packet_loss",
            "detail": f"Pérdida de paquetes de {metrics['packet_loss_pct']}%.",
        }
    else:
        state = {"link_state": "up", "reason": "healthy", "detail": "Enlace estable."}

    return {
        "account_id": account["account_id"],
        "account_status": account["status"],
        "region": account["region"],
        **state,
        "metrics": metrics,
    }


def handle_list_outages(store: NetopsStore, arguments: dict[str, Any]) -> dict[str, Any]:
    region = arguments.get("region")
    active_only = arguments.get("active_only", True)
    outages = store.find_outages(region=region, active_only=active_only)
    return {
        "region": region or "all",
        "active_only": active_only,
        "count": len(outages),
        "outages": outages,
    }


def handle_run_diagnostic(store: NetopsStore, arguments: dict[str, Any]) -> dict[str, Any]:
    account = _require_account(store, arguments["account_id"])
    test_type = arguments["test_type"]
    metrics = _link_metrics(account)
    outage = store.active_outage_for(account)

    if account["status"] == "suspended":
        cause = "Servicio suspendido administrativamente; no es una falla técnica."
    elif account["status"] == "pending_installation":
        cause = "La instalación está pendiente; todavía no hay enlace que medir."
    elif outage is not None:
        cause = f"Incidencia masiva {outage['outage_id']} en la región: {outage['cause']}."
    elif metrics["packet_loss_pct"] >= 1.5:
        cause = "Pérdida de paquetes elevada; probable degradación en el último tramo."
    elif metrics["snr_db"] < 27:
        cause = "Relación señal/ruido baja; revisar acometida y conectores."
    else:
        cause = "Sin anomalías detectadas en la línea."

    if test_type == "ping":
        readings = {
            "latency_ms": metrics["latency_ms"],
            "jitter_ms": round(metrics["latency_ms"] / 6, 1),
            "packet_loss_pct": metrics["packet_loss_pct"],
            "packets_sent": 20,
        }
    elif test_type == "speed":
        readings = {
            "downstream_mbps": metrics["downstream_mbps"],
            "upstream_mbps": metrics["upstream_mbps"],
            "contracted_mbps": account["plan_speed_mbps"],
            "pct_of_plan": round(
                metrics["downstream_mbps"] / account["plan_speed_mbps"] * 100, 1
            ),
        }
    else:  # line
        readings = {
            "snr_db": metrics["snr_db"],
            "attenuation_db": round(stable_int(account["account_id"], "att", low=50, high=180) / 10, 1),
            "technology": account["technology"],
            "sync_errors_last_hour": stable_int(account["account_id"], "err", low=0, high=12),
        }

    return {
        "account_id": account["account_id"],
        "test_type": test_type,
        "readings": readings,
        "probable_cause": cause,
    }


def handle_open_ticket(store: NetopsStore, arguments: dict[str, Any]) -> dict[str, Any]:
    account = _require_account(store, arguments["account_id"])
    ticket = store.create_ticket(
        account_id=account["account_id"],
        category=arguments["category"],
        description=arguments["description"],
        priority=arguments.get("priority", "normal"),
    )
    return {
        "ticket_id": ticket["ticket_id"],
        "status": ticket["status"],
        "account_id": ticket["account_id"],
        "category": ticket["category"],
        "priority": ticket["priority"],
        "created_at": ticket["created_at"],
    }


def handle_get_ticket(store: NetopsStore, arguments: dict[str, Any]) -> dict[str, Any]:
    ticket_id = arguments["ticket_id"].strip().upper()
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        raise DomainError(f"No existe el ticket {ticket_id}.", ticket_id=ticket_id)
    payload = dict(ticket)
    visit = store.get_visit(ticket["ticket_id"])
    if visit is not None:
        payload["visit"] = visit
    return payload


def handle_schedule_visit(store: NetopsStore, arguments: dict[str, Any]) -> dict[str, Any]:
    ticket_id = arguments["ticket_id"].strip().upper()
    ticket = store.get_ticket(ticket_id)
    if ticket is None:
        raise DomainError(
            f"No existe el ticket {ticket_id}; abra uno con open_ticket antes de agendar.",
            ticket_id=ticket_id,
        )

    raw_date = arguments["date"]
    try:
        # The schema already enforced YYYY-MM-DD; this catches dates that match
        # the shape but are not real, such as 2026-02-30.
        parsed = date_type.fromisoformat(raw_date)
    except ValueError:
        raise InvalidParamsError(
            f"date: {raw_date!r} is not a real calendar date", data={"field": "date"}
        ) from None

    today = date_type.fromisoformat(store.now()[:10])
    if parsed < today:
        raise DomainError(
            f"La fecha {raw_date} ya pasó; agende a partir de {today.isoformat()}.",
            ticket_id=ticket_id,
            earliest=today.isoformat(),
        )

    already = store.get_visit(ticket_id) is not None
    visit = store.schedule_visit(ticket_id, raw_date, arguments["time_window"])
    return {
        **visit,
        "rescheduled": already,
        "message": (
            f"Visita {'reagendada' if already else 'agendada'} para el {raw_date} "
            f"en la ventana {arguments['time_window']}."
        ),
    }


Handler = Callable[[NetopsStore, dict[str, Any]], dict[str, Any]]

HANDLERS: dict[str, Handler] = {
    "lookup_account": handle_lookup_account,
    "check_service_status": handle_check_service_status,
    "list_outages": handle_list_outages,
    "run_diagnostic": handle_run_diagnostic,
    "open_ticket": handle_open_ticket,
    "get_ticket": handle_get_ticket,
    "schedule_visit": handle_schedule_visit,
}


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def _text_result(payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "isError": is_error,
    }


def list_tools() -> dict[str, Any]:
    return {"tools": TOOLS}


def dispatch(store: NetopsStore, name: str, arguments: Any) -> dict[str, Any]:
    """Validate and run one tool call.

    Raises InvalidParamsError for a call that does not match the schema; returns
    an isError result for a domain failure.
    """
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        # The tool name is a parameter of tools/call, so an unknown one means the
        # request did not match what tools/list advertises.
        raise InvalidParamsError(
            f"unknown tool: {name!r}",
            data={"available": sorted(TOOLS_BY_NAME)},
        )

    arguments = validate_arguments(arguments, tool["inputSchema"])
    try:
        payload = HANDLERS[name](store, arguments)
    except DomainError as exc:
        return _text_result(exc.to_payload(), is_error=True)
    return _text_result(payload)
