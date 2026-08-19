"""A small JSON Schema validator, covering only what the tool schemas use.

The assignment permits four dependencies and `jsonschema` is not one of them,
so the subset the tools actually need is implemented here: `type`, `required`,
`enum`, `minLength`, `maxLength`, `pattern`, `minimum`, `maximum`, and
`additionalProperties: false`.

Every failure raises InvalidParamsError, which the server turns into -32602.
This is deliberately the protocol-error side of the line: an argument that does
not satisfy the advertised schema means the call was never well formed. A
well-formed call that fails for a domain reason - an account that does not
exist - is a successful result carrying `isError: true` instead.
"""

from __future__ import annotations

import re
from typing import Any

from host.mcp.jsonrpc import InvalidParamsError

# JSON Schema type name -> the Python types that satisfy it.
_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}


def _type_matches(value: Any, expected: str) -> bool:
    python_types = _TYPES.get(expected)
    if python_types is None:
        return True  # unknown type keyword: nothing to enforce
    if expected in ("integer", "number") and isinstance(value, bool):
        return False  # bool is an int in Python, but not a JSON number
    if expected == "boolean":
        return isinstance(value, bool)
    return isinstance(value, python_types)


def _fail(path: str, problem: str) -> None:
    raise InvalidParamsError(f"{path}: {problem}", data={"field": path})


def _validate_value(value: Any, schema: dict[str, Any], path: str) -> None:
    expected = schema.get("type")
    if isinstance(expected, str) and not _type_matches(value, expected):
        _fail(path, f"expected {expected}, got {type(value).__name__}")

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(option) for option in schema["enum"])
        _fail(path, f"must be one of: {allowed}")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            _fail(path, f"must be at least {minimum_length} character(s)")
        maximum_length = schema.get("maxLength")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            _fail(path, f"must be at most {maximum_length} character(s)")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and not re.match(pattern, value):
            _fail(path, f"must match {pattern}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            _fail(path, f"must be >= {minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            _fail(path, f"must be <= {maximum}")

    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_value(item, item_schema, f"{path}[{index}]")

    if isinstance(value, dict) and isinstance(schema.get("properties"), dict):
        _validate_object(value, schema, path)


def _validate_object(value: dict[str, Any], schema: dict[str, Any], path: str) -> None:
    properties: dict[str, Any] = schema.get("properties", {})

    for name in schema.get("required", []):
        if name not in value:
            _fail(f"{path}.{name}" if path else name, "is required")

    if schema.get("additionalProperties") is False:
        unknown = sorted(set(value) - set(properties))
        if unknown:
            _fail(path or "arguments", f"unknown argument(s): {', '.join(unknown)}")

    for name, item in value.items():
        item_schema = properties.get(name)
        if isinstance(item_schema, dict):
            _validate_value(item, item_schema, f"{path}.{name}" if path else name)


def validate_arguments(arguments: Any, schema: dict[str, Any]) -> dict[str, Any]:
    """Check `arguments` against a tool's inputSchema.

    Returns the arguments unchanged so call sites can use it inline.
    """
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise InvalidParamsError(
            f"arguments must be an object, got {type(arguments).__name__}"
        )
    _validate_object(arguments, schema, "")
    return arguments


def require_one_of(arguments: dict[str, Any], names: list[str]) -> str:
    """Enforce 'exactly one of these arguments', which plain JSON Schema needs
    anyOf/oneOf to express. Returns the name of the one that was supplied.
    """
    supplied = [name for name in names if arguments.get(name) not in (None, "")]
    if not supplied:
        raise InvalidParamsError(
            f"exactly one of {', '.join(names)} is required, none was given",
            data={"expected_one_of": names},
        )
    if len(supplied) > 1:
        raise InvalidParamsError(
            f"exactly one of {', '.join(names)} is required, got {', '.join(supplied)}",
            data={"expected_one_of": names},
        )
    return supplied[0]
