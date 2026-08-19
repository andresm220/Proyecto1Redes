"""F1 tests: the three JSON-RPC message types and the five reserved error codes."""

from __future__ import annotations

import json

import pytest

from host.mcp import jsonrpc as rpc

# --------------------------------------------------------------------------
# The three message types
# --------------------------------------------------------------------------


def test_request_round_trip():
    message = rpc.build_request(1, "tools/list", {})
    assert message == {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    parsed = rpc.parse_line(rpc.encode(message))
    assert isinstance(parsed, rpc.Request)
    assert (parsed.id, parsed.method, parsed.params) == (1, "tools/list", {})


def test_request_accepts_a_string_id():
    parsed = rpc.parse_message(rpc.build_request("req-7", "ping"))
    assert isinstance(parsed, rpc.Request)
    assert parsed.id == "req-7"


def test_request_omits_params_when_not_given():
    assert "params" not in rpc.build_request(1, "ping")
    assert rpc.parse_message(rpc.build_request(1, "ping")).params == {}


@pytest.mark.parametrize("bad_id", [None, True, 1.5, [], {}])
def test_request_id_is_never_null_or_ill_typed(bad_id):
    with pytest.raises(ValueError, match="int or a string"):
        rpc.build_request(bad_id, "ping")


def test_notification_round_trip():
    message = rpc.build_notification("notifications/initialized")
    assert message == {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert "id" not in message, "a notification must never carry an id"

    parsed = rpc.parse_line(rpc.encode(message))
    assert isinstance(parsed, rpc.Notification)
    assert parsed.method == "notifications/initialized"


def test_a_message_with_an_id_is_a_request_not_a_notification():
    """The id is the only thing that separates the two. Guard the boundary."""
    assert isinstance(rpc.parse_message(rpc.build_request(1, "ping")), rpc.Request)
    assert isinstance(rpc.parse_message(rpc.build_notification("ping")), rpc.Notification)


def test_success_response_round_trip():
    message = rpc.build_response(1, {"tools": []})
    assert message == {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}

    parsed = rpc.parse_line(rpc.encode(message))
    assert isinstance(parsed, rpc.SuccessResponse)
    assert (parsed.id, parsed.result) == (1, {"tools": []})


def test_error_response_round_trip():
    message = rpc.build_error(1, rpc.METHOD_NOT_FOUND)
    assert message == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32601, "message": "Method not found"},
    }

    parsed = rpc.parse_line(rpc.encode(message))
    assert isinstance(parsed, rpc.ErrorResponse)
    assert (parsed.id, parsed.code, parsed.message) == (1, -32601, "Method not found")


def test_result_and_error_are_mutually_exclusive():
    both = {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {"code": -1, "message": "x"}}
    neither = {"jsonrpc": "2.0", "id": 1}
    for message in (both, neither):
        with pytest.raises(rpc.InvalidRequestError, match="exactly one"):
            rpc.parse_message(message)


# --------------------------------------------------------------------------
# The five reserved error codes
# --------------------------------------------------------------------------

CODES = [
    (rpc.ParseError, rpc.PARSE_ERROR, -32700, "Parse error"),
    (rpc.InvalidRequestError, rpc.INVALID_REQUEST, -32600, "Invalid Request"),
    (rpc.MethodNotFoundError, rpc.METHOD_NOT_FOUND, -32601, "Method not found"),
    (rpc.InvalidParamsError, rpc.INVALID_PARAMS, -32602, "Invalid params"),
    (rpc.InternalError, rpc.INTERNAL_ERROR, -32603, "Internal error"),
]


@pytest.mark.parametrize("exc_class,constant,code,message", CODES)
def test_each_error_code_has_its_exception_and_wire_form(exc_class, constant, code, message):
    assert constant == code
    assert exc_class.code == code

    error = exc_class()
    assert error.to_payload() == {"code": code, "message": message}

    response = rpc.build_error_from(1, error)
    parsed = rpc.parse_message(response)
    assert isinstance(parsed, rpc.ErrorResponse)
    assert (parsed.code, parsed.message) == (code, message)


@pytest.mark.parametrize("exc_class,constant,code,message", CODES)
def test_each_error_carries_optional_data(exc_class, constant, code, message):
    error = exc_class("custom text", data={"field": "account_id"})
    assert error.to_payload() == {
        "code": code,
        "message": "custom text",
        "data": {"field": "account_id"},
    }
    assert rpc.parse_message(rpc.build_error_from(7, error)).data == {"field": "account_id"}


def test_parse_error_is_raised_for_malformed_json():
    with pytest.raises(rpc.ParseError, match="invalid JSON"):
        rpc.decode('{"jsonrpc": "2.0", ')


def test_parse_error_is_raised_for_a_non_object():
    with pytest.raises(rpc.ParseError, match="must be a JSON object"):
        rpc.decode("[1, 2, 3]")


def test_parse_error_is_raised_for_a_blank_line():
    with pytest.raises(rpc.ParseError, match="empty line"):
        rpc.decode("   ")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"id": 1, "method": "ping"}, "jsonrpc must be"),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, "jsonrpc must be"),
        ({"jsonrpc": "2.0", "id": 1, "method": ""}, "non-empty string"),
        ({"jsonrpc": "2.0", "id": 1, "method": 42}, "non-empty string"),
        ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": "no"}, "object or an array"),
        ({"jsonrpc": "2.0", "id": None, "method": "ping"}, "never null"),
        ({"jsonrpc": "2.0", "id": True, "method": "ping"}, "never null"),
        ({"jsonrpc": "2.0", "id": 1, "error": "not an object"}, "error must be an object"),
        ({"jsonrpc": "2.0", "id": 1, "error": {"message": "x"}}, "code must be an integer"),
        ({"jsonrpc": "2.0", "id": 1, "error": {"code": -1}}, "message must be a string"),
    ],
)
def test_invalid_request_is_raised_for_a_broken_envelope(raw, expected):
    with pytest.raises(rpc.InvalidRequestError, match=expected):
        rpc.parse_message(raw)


def test_error_response_may_carry_a_null_id():
    """The one legal null id: a parse error, whose request id is unrecoverable."""
    parsed = rpc.parse_message(rpc.build_error(None, rpc.PARSE_ERROR))
    assert isinstance(parsed, rpc.ErrorResponse)
    assert parsed.id is None


# --------------------------------------------------------------------------
# NDJSON framing
# --------------------------------------------------------------------------


def test_encode_never_emits_an_embedded_newline():
    """NDJSON framing breaks if a payload newline reaches the wire unescaped."""
    line = rpc.encode(rpc.build_response(1, {"text": "line one\nline two\r\nthree"}))
    assert "\n" not in line and "\r" not in line
    assert rpc.parse_message(json.loads(line)).result["text"] == "line one\nline two\r\nthree"


def test_encode_preserves_non_ascii_verbatim():
    """Service addresses in the seed data carry accents; they must survive.

    ensure_ascii=False keeps them as real UTF-8 rather than \\uXXXX escapes,
    which is what makes the cp1252 default on Windows a hazard worth testing.
    """
    address = "Zona 10, Ciudad de Guatemala, Petén — 3º nivel"
    line = rpc.encode(rpc.build_response(1, {"address": address}))
    assert address in line, "non-ASCII must not be escaped away"
    assert line.encode("utf-8").decode("utf-8")  # survives a UTF-8 round trip
    assert rpc.parse_line(line).result["address"] == address


def test_decode_tolerates_surrounding_whitespace():
    assert rpc.decode('  {"jsonrpc":"2.0","id":1,"method":"ping"}  \n')["id"] == 1
