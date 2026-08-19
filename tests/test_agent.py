"""F4 tests: the agentic loop and the MCP -> Anthropic schema translation.

The model is scripted rather than called: the loop's job is to route tool calls
and shape messages, and that is deterministic. The servers are real, so a tool
call in these tests goes over an actual stdio pipe to the actual netops server.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from host.agent import MAX_ITERATIONS, Agent
from host.config import ServerConfig
from host.llm.anthropic_client import mcp_tool_to_anthropic, mcp_tools_to_anthropic
from host.mcp.registry import ServerRegistry
from host.session import Session, build_system_prompt


# --------------------------------------------------------------------------
# A scripted stand-in for the Messages API
# --------------------------------------------------------------------------


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def tool_use_block(block_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": arguments}


def response(*blocks: dict[str, Any], stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


class ScriptedLLM:
    """Returns pre-written responses and records what it was asked."""

    def __init__(self, *responses: SimpleNamespace) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, messages, tools, system):
        self.calls.append(
            {"messages": [dict(m) for m in messages], "tools": tools, "system": system}
        )
        if not self.responses:
            return response(text_block("(script exhausted)"))
        return self.responses.pop(0)


@pytest.fixture
def registry(data_dir):
    registry = ServerRegistry(
        {
            "netops": ServerConfig(
                name="netops",
                command="python",
                args=["-m", "servers.netops.stdio_server"],
                env={"NETOPS_DATA_DIR": str(data_dir)},
            )
        }
    )
    registry.connect_all()
    assert registry.clients, f"netops failed to start: {registry.failures}"
    yield registry
    registry.close_all()


def make_agent(registry, llm, **kwargs) -> Agent:
    return Agent(llm=llm, registry=registry, session=Session(), **kwargs)


# --------------------------------------------------------------------------
# Schema translation
# --------------------------------------------------------------------------


def test_inputSchema_is_renamed_to_input_schema(registry):
    """The one difference between the formats, and it fails silently."""
    tool = registry.get("netops__check_service_status")
    translated = mcp_tool_to_anthropic(tool)

    assert translated["name"] == "netops__check_service_status"
    assert "input_schema" in translated
    assert "inputSchema" not in translated
    assert translated["input_schema"] == tool.definition["inputSchema"]
    assert translated["description"] == tool.definition["description"]


def test_every_translated_name_is_a_legal_anthropic_tool_name(registry):
    import re

    for translated in mcp_tools_to_anthropic(registry.tools):
        assert re.match(r"^[a-zA-Z0-9_-]{1,128}$", translated["name"])


def test_translation_covers_every_tool(registry):
    assert len(mcp_tools_to_anthropic(registry.tools)) == 7


def test_server_instructions_reach_the_system_prompt():
    prompt = build_system_prompt({"netops": "Always check list_outages first."})
    assert "Always check list_outages first." in prompt
    assert "netops" in prompt


def test_blank_instructions_are_skipped():
    assert "Guidance from" not in build_system_prompt({"netops": "   "})


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_a_turn_with_no_tool_use_returns_the_text(registry):
    llm = ScriptedLLM(response(text_block("Hola, ¿en qué le ayudo?")))
    agent = make_agent(registry, llm)

    assert agent.run_turn("hola") == "Hola, ¿en qué le ayudo?"
    assert len(llm.calls) == 1


def test_a_tool_call_is_executed_and_fed_back(registry):
    llm = ScriptedLLM(
        response(
            tool_use_block("toolu_1", "netops__lookup_account", {"account_id": "GT-10231"}),
            stop_reason="tool_use",
        ),
        response(text_block("La cuenta GT-10231 tiene plan Fibra 300.")),
    )
    agent = make_agent(registry, llm)
    answer = agent.run_turn("¿qué plan tiene GT-10231?")

    assert answer == "La cuenta GT-10231 tiene plan Fibra 300."
    assert len(llm.calls) == 2

    # The second request must carry the real tool output back to the model.
    tool_result = llm.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_1"
    assert tool_result["is_error"] is False
    assert "Fibra 300" in tool_result["content"]


def test_a_multi_step_chain_runs_to_completion(registry):
    """Status, then outages, then the answer: three model turns, two tool rounds."""
    llm = ScriptedLLM(
        response(
            tool_use_block(
                "toolu_1", "netops__check_service_status", {"account_id": "GT-10233"}
            ),
            stop_reason="tool_use",
        ),
        response(
            tool_use_block("toolu_2", "netops__list_outages", {"region": "peten"}),
            stop_reason="tool_use",
        ),
        response(text_block("Hay una incidencia masiva en Petén con ETA a las 18:00.")),
    )
    agent = make_agent(registry, llm)
    answer = agent.run_turn("GT-10233 no tiene internet")

    assert "incidencia masiva" in answer
    assert len(llm.calls) == 3
    assert "OUT-2026-013" in llm.calls[2]["messages"][-1]["content"][0]["content"]


def test_parallel_tool_calls_come_back_in_one_user_turn(registry):
    """Splitting them across messages trains the model out of parallel calls."""
    llm = ScriptedLLM(
        response(
            tool_use_block("toolu_1", "netops__lookup_account", {"account_id": "GT-10231"}),
            tool_use_block("toolu_2", "netops__lookup_account", {"account_id": "GT-10234"}),
            stop_reason="tool_use",
        ),
        response(text_block("Ambas cuentas están activas.")),
    )
    agent = make_agent(registry, llm)
    agent.run_turn("compara GT-10231 y GT-10234")

    last_message = llm.calls[1]["messages"][-1]
    assert last_message["role"] == "user"
    assert len(last_message["content"]) == 2
    assert [block["tool_use_id"] for block in last_message["content"]] == ["toolu_1", "toolu_2"]


def test_the_session_keeps_the_whole_exchange(registry):
    llm = ScriptedLLM(
        response(
            tool_use_block("toolu_1", "netops__list_outages", {}),
            stop_reason="tool_use",
        ),
        response(text_block("Dos incidencias activas.")),
    )
    session = Session()
    agent = Agent(llm=llm, registry=registry, session=session)
    agent.run_turn("¿hay incidencias?")

    assert [message["role"] for message in session.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


# --------------------------------------------------------------------------
# Failures the model has to be told about
# --------------------------------------------------------------------------


def test_a_domain_error_comes_back_as_is_error(registry):
    """isError from MCP becomes is_error for Anthropic: same meaning."""
    llm = ScriptedLLM(
        response(
            tool_use_block("toolu_1", "netops__lookup_account", {"account_id": "GT-99999"}),
            stop_reason="tool_use",
        ),
        response(text_block("No encontré esa cuenta.")),
    )
    agent = make_agent(registry, llm)
    agent.run_turn("busca GT-99999")

    tool_result = llm.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "No se encontró" in tool_result["content"]


def test_a_protocol_error_is_reported_to_the_model_not_raised(registry):
    """A -32602 must reach the model so it can fix its own arguments."""
    llm = ScriptedLLM(
        response(
            tool_use_block("toolu_1", "netops__check_service_status", {}),
            stop_reason="tool_use",
        ),
        response(text_block("Necesito el número de cuenta.")),
    )
    agent = make_agent(registry, llm)
    answer = agent.run_turn("revisa el estado")

    tool_result = llm.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "-32602" in tool_result["content"]
    assert answer == "Necesito el número de cuenta."


def test_a_hallucinated_tool_name_is_reported_to_the_model(registry):
    llm = ScriptedLLM(
        response(
            tool_use_block("toolu_1", "netops__delete_everything", {}),
            stop_reason="tool_use",
        ),
        response(text_block("Esa herramienta no existe.")),
    )
    agent = make_agent(registry, llm)
    agent.run_turn("borra todo")

    tool_result = llm.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "unknown tool" in tool_result["content"]


def test_the_iteration_cap_stops_a_runaway_turn(registry):
    """Without the cap a model that keeps asking for tools never stops."""
    forever = [
        response(
            tool_use_block(f"toolu_{index}", "netops__list_outages", {}),
            stop_reason="tool_use",
        )
        for index in range(MAX_ITERATIONS + 5)
    ]
    llm = ScriptedLLM(*forever)
    agent = make_agent(registry, llm)
    answer = agent.run_turn("sigue llamando tools")

    assert len(llm.calls) == MAX_ITERATIONS
    assert "Stopped after 10 tool rounds" in answer


def test_the_cap_is_configurable(registry):
    llm = ScriptedLLM(
        *[
            response(
                tool_use_block(f"toolu_{index}", "netops__list_outages", {}),
                stop_reason="tool_use",
            )
            for index in range(10)
        ]
    )
    agent = make_agent(registry, llm, max_iterations=3)
    agent.run_turn("sigue")
    assert len(llm.calls) == 3


def test_stop_reason_tool_use_with_no_tool_block_does_not_loop(registry):
    """A malformed response must end the turn rather than spin."""
    llm = ScriptedLLM(response(text_block("raro"), stop_reason="tool_use"))
    agent = make_agent(registry, llm)

    assert agent.run_turn("hola") == "raro"
    assert len(llm.calls) == 1


def test_tool_arguments_are_passed_through_verbatim(registry):
    """Whatever the model asked for is what the server must receive."""
    llm = ScriptedLLM(
        response(
            tool_use_block(
                "toolu_1",
                "netops__open_ticket",
                {
                    "account_id": "GT-10231",
                    "category": "connectivity",
                    "description": "Se cae el servicio cada noche.",
                    "priority": "high",
                },
            ),
            stop_reason="tool_use",
        ),
        response(text_block("Listo.")),
    )
    agent = make_agent(registry, llm)
    agent.run_turn("abre un ticket")

    payload = json.loads(llm.calls[1]["messages"][-1]["content"][0]["content"])
    assert payload["priority"] == "high"
    assert payload["ticket_id"].startswith("TCK-")
