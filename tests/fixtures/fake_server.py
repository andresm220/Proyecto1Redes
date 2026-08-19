"""A minimal MCP server used to test the transport and the client in isolation.

It deliberately uses plain `json` rather than host.mcp.jsonrpc: if both sides
of the test shared an encoder, a bug in that encoder would cancel itself out
and the test would still pass.

Behaviour is switched with the FAKE_MODE environment variable:

    normal        the happy path (default)
    bad_version   answer initialize with an unsupported protocolVersion
    garbage       emit an unparseable line before every valid response
    silent        accept initialize but never answer it
    crash         exit abruptly once initialize arrives
"""

from __future__ import annotations

import json
import os
import sys

PROTOCOL_VERSION = "2025-11-25"

TOOLS = [
    {
        "name": "echo",
        "title": "Echo",
        "description": "Return the text it was given.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "explode",
        "title": "Explode",
        "description": "Always raises, to exercise the -32603 path.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

MODE = os.environ.get("FAKE_MODE", "normal")


def log(text: str) -> None:
    print(f"fake_server: {text}", file=sys.stderr, flush=True)


def write(message: dict) -> None:
    if MODE == "garbage":
        sys.stdout.write("this line is not JSON\n")
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def ok(message_id, result) -> None:
    write({"jsonrpc": "2.0", "id": message_id, "result": result})


def fail(message_id, code: int, message: str) -> None:
    write({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}})


def handle(request: dict) -> None:
    method = request.get("method")
    message_id = request.get("id")

    if method == "initialize":
        if MODE == "crash":
            log("exiting abruptly on purpose")
            raise SystemExit(1)
        if MODE == "silent":
            log("swallowing initialize on purpose")
            return
        version = "1999-01-01" if MODE == "bad_version" else PROTOCOL_VERSION
        ok(
            message_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fake", "version": "1.0.0"},
                "instructions": "A fixture server.",
            },
        )
    elif method == "notifications/initialized":
        log("client finished the handshake")  # a notification is never answered
    elif method == "ping":
        ok(message_id, {})
    elif method == "tools/list":
        ok(message_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            if "text" not in arguments:
                fail(message_id, -32602, "missing required argument: text")
            else:
                ok(
                    message_id,
                    {
                        "content": [{"type": "text", "text": arguments["text"]}],
                        "isError": False,
                    },
                )
        elif name == "explode":
            fail(message_id, -32603, "handler blew up")
        else:
            # Unknown *tool* is a domain failure, not a protocol failure.
            ok(
                message_id,
                {
                    "content": [{"type": "text", "text": f"no such tool: {name}"}],
                    "isError": True,
                },
            )
    elif message_id is not None:
        fail(message_id, -32601, f"Method not found: {method}")


def main() -> int:
    log(f"started in mode {MODE!r}")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            fail(None, -32700, "Parse error")
            continue
        handle(request)
    log("stdin closed, exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
