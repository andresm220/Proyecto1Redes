"""JSON-RPC 2.0 envelopes, implemented by hand.

This module is pure: it builds, encodes, decodes and classifies messages and
performs no I/O. That is what makes it testable before any subprocess exists.

Specification: https://www.jsonrpc.org/specification

The three message types are:

    request       {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    response      {"jsonrpc": "2.0", "id": 1, "result": {...}}
                  {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "..."}}
    notification  {"jsonrpc": "2.0", "method": "notifications/initialized"}

A request carries a unique id and expects exactly one response. A notification
carries no id and must never be answered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Union

JSONRPC_VERSION = "2.0"

# The five error codes reserved by the specification.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

ERROR_MESSAGES = {
    PARSE_ERROR: "Parse error",
    INVALID_REQUEST: "Invalid Request",
    METHOD_NOT_FOUND: "Method not found",
    INVALID_PARAMS: "Invalid params",
    INTERNAL_ERROR: "Internal error",
}

# A request id is an int or a string, never null. Null appears only in an error
# response to a message whose id could not be determined (a parse error).
MessageId = Union[int, str]
Params = Union[dict[str, Any], list[Any]]


class JsonRpcError(Exception):
    """A protocol-level error, carrying the code that goes on the wire.

    This is for broken exchanges only. A failed operation whose exchange was
    well formed - an account that does not exist, say - is a successful
    response carrying `isError: true`, not a JsonRpcError.
    """

    code = INTERNAL_ERROR

    def __init__(self, message: str | None = None, data: Any = None) -> None:
        self.message = message or ERROR_MESSAGES.get(self.code, "Error")
        self.data = data
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


class ParseError(JsonRpcError):
    """The received text was not valid JSON."""

    code = PARSE_ERROR


class InvalidRequestError(JsonRpcError):
    """Valid JSON, but not a valid JSON-RPC 2.0 envelope."""

    code = INVALID_REQUEST


class MethodNotFoundError(JsonRpcError):
    """The method does not exist on this peer."""

    code = METHOD_NOT_FOUND


class InvalidParamsError(JsonRpcError):
    """The params are missing, ill-typed, or fail schema validation."""

    code = INVALID_PARAMS


class InternalError(JsonRpcError):
    """An unhandled failure while executing an otherwise valid call."""

    code = INTERNAL_ERROR


@dataclass(frozen=True)
class Request:
    id: MessageId
    method: str
    params: Params = field(default_factory=dict)


@dataclass(frozen=True)
class Notification:
    method: str
    params: Params = field(default_factory=dict)


@dataclass(frozen=True)
class SuccessResponse:
    id: MessageId
    result: Any


@dataclass(frozen=True)
class ErrorResponse:
    id: MessageId | None
    code: int
    message: str
    data: Any = None


Message = Union[Request, Notification, SuccessResponse, ErrorResponse]


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def build_request(id: MessageId, method: str, params: Params | None = None) -> dict[str, Any]:
    if not isinstance(id, (int, str)) or isinstance(id, bool):
        raise ValueError("request id must be an int or a string, never null")
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def build_notification(method: str, params: Params | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        message["params"] = params
    return message


def build_response(id: MessageId, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "result": result}


def build_error(
    id: MessageId | None,
    code: int,
    message: str | None = None,
    data: Any = None,
) -> dict[str, Any]:
    """Build an error response.

    `id` is null only when the offending message's id could not be recovered,
    which in practice means a parse error.
    """
    payload: dict[str, Any] = {
        "code": code,
        "message": message or ERROR_MESSAGES.get(code, "Error"),
    }
    if data is not None:
        payload["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "error": payload}


def build_error_from(id: MessageId | None, error: JsonRpcError) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": id, "error": error.to_payload()}


# --------------------------------------------------------------------------
# Encoding and decoding (NDJSON framing)
# --------------------------------------------------------------------------


def encode(message: dict[str, Any]) -> str:
    """Serialise one message to a single NDJSON line, without the terminator.

    json.dumps escapes newlines inside strings, so the result is always a
    single line - which is exactly what NDJSON framing requires.
    """
    line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    if "\n" in line or "\r" in line:  # pragma: no cover - defensive
        raise ValueError("encoded message contains an embedded newline")
    return line


def decode(line: str) -> dict[str, Any]:
    """Parse one NDJSON line into a raw message object.

    Raises ParseError for anything that is not a JSON object.

    A leading byte order mark is stripped rather than rejected. JSON is defined
    as UTF-8 and a BOM is redundant, but several tools prepend one when writing
    a stream - PowerShell does it on every pipe - and RFC 8259 lets a parser
    ignore it. Failing on that would turn a cosmetic quirk into a dead session.
    """
    text = line.strip().lstrip("\ufeff").strip()
    if not text:
        raise ParseError("empty line")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ParseError("a JSON-RPC message must be a JSON object")
    return parsed


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def _validate_envelope(raw: dict[str, Any]) -> None:
    if raw.get("jsonrpc") != JSONRPC_VERSION:
        raise InvalidRequestError(f"jsonrpc must be exactly {JSONRPC_VERSION!r}")


def _validate_id(raw: dict[str, Any]) -> MessageId:
    id_value = raw.get("id")
    if isinstance(id_value, bool) or not isinstance(id_value, (int, str)):
        raise InvalidRequestError("id must be an int or a string, never null")
    return id_value


def _validate_params(raw: dict[str, Any]) -> Params:
    if "params" not in raw:
        return {}
    params = raw["params"]
    if not isinstance(params, (dict, list)):
        raise InvalidRequestError("params must be an object or an array")
    return params


def parse_message(raw: dict[str, Any]) -> Message:
    """Classify a decoded message into one of the four concrete shapes.

    Raises InvalidRequestError when the envelope is well-formed JSON but does
    not describe a valid JSON-RPC 2.0 message.
    """
    if not isinstance(raw, dict):
        raise InvalidRequestError("a JSON-RPC message must be a JSON object")
    _validate_envelope(raw)

    if "method" in raw:
        method = raw["method"]
        if not isinstance(method, str) or not method:
            raise InvalidRequestError("method must be a non-empty string")
        params = _validate_params(raw)
        if "id" in raw:
            return Request(id=_validate_id(raw), method=method, params=params)
        return Notification(method=method, params=params)

    has_result = "result" in raw
    has_error = "error" in raw
    if has_result == has_error:
        raise InvalidRequestError("a response must carry exactly one of result or error")

    if has_result:
        return SuccessResponse(id=_validate_id(raw), result=raw["result"])

    error = raw["error"]
    if not isinstance(error, dict):
        raise InvalidRequestError("error must be an object")
    code = error.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        raise InvalidRequestError("error.code must be an integer")
    message = error.get("message")
    if not isinstance(message, str):
        raise InvalidRequestError("error.message must be a string")
    # An error response may carry a null id when the request's id could not be
    # recovered, so it is the one place a null id is legal.
    id_value = raw.get("id")
    if id_value is not None and (isinstance(id_value, bool) or not isinstance(id_value, (int, str))):
        raise InvalidRequestError("id must be an int, a string, or null on an error response")
    return ErrorResponse(id=id_value, code=code, message=message, data=error.get("data"))


def parse_line(line: str) -> Message:
    """decode() then parse_message(), the pairing every transport needs."""
    return parse_message(decode(line))
