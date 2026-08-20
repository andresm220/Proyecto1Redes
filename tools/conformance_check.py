"""An independent conformance check for the netops MCP server.

This is deliberately a *foreign* client: it uses only the standard library and
raw `json`, and imports nothing from `host/`. If it shared our encoder, a bug in
that encoder would cancel itself out and the check would pass anyway.

It plays the part a third-party host such as Claude Desktop plays: launch the
server, complete the handshake, list the tools, call one, and shut down.

Run from the repository root:

    python tools/conformance_check.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROTOCOL_VERSION = "2025-11-25"
REPO_ROOT = Path(__file__).resolve().parent.parent

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))


class ForeignClient:
    """A minimal stdio JSON-RPC client written from the spec, not from our code."""

    def __init__(self, data_dir: Path) -> None:
        env = os.environ.copy()
        env["NETOPS_DATA_DIR"] = str(data_dir)
        env["PYTHONIOENCODING"] = "utf-8"
        self.process = subprocess.Popen(
            [sys.executable, "-m", "servers.netops.stdio_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(REPO_ROOT),
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._next_id = 0

    def send(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False)
        assert "\n" not in line, "NDJSON framing forbids embedded newlines"
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return json.loads(self.process.stdout.readline())

    def notify(self, method: str) -> None:
        self.send({"jsonrpc": "2.0", "method": method})

    def close(self) -> int:
        self.process.stdin.close()
        return self.process.wait(timeout=10)


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="netops-conformance-"))
    (workdir / "seed").mkdir()
    seed = REPO_ROOT / "servers" / "netops" / "data" / "seed"
    for name in ("accounts.json", "outages.json"):
        (workdir / "seed" / name).write_bytes((seed / name).read_bytes())

    print(f"Conformance check against {REPO_ROOT}")
    print(f"State directory: {workdir}\n")

    client = ForeignClient(workdir)

    print("Lifecycle")
    response = client.request(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "conformance-check", "version": "1.0.0"},
        },
    )
    result = response.get("result", {})
    check("initialize returns a result", "result" in response, json.dumps(response)[:200])
    check(
        f"protocolVersion is {PROTOCOL_VERSION}",
        result.get("protocolVersion") == PROTOCOL_VERSION,
        f"got {result.get('protocolVersion')!r}",
    )
    check("serverInfo carries a name and version", bool(result.get("serverInfo", {}).get("name")))
    check("capabilities advertise tools", "tools" in result.get("capabilities", {}))
    check("instructions are provided", bool(result.get("instructions")))
    check("the response echoes the request id", response.get("id") == 1)

    client.notify("notifications/initialized")

    ping = client.request("ping")
    check("ping answers after the handshake", "result" in ping)

    print("\nTools")
    listed = client.request("tools/list")
    tools = listed.get("result", {}).get("tools", [])
    check("tools/list returns tools", bool(tools), json.dumps(listed)[:200])
    check("every tool has a name and a description",
          all(tool.get("name") and tool.get("description") for tool in tools))
    check("every tool has an object inputSchema",
          all(tool.get("inputSchema", {}).get("type") == "object" for tool in tools))
    print(f"        found {len(tools)}: {', '.join(tool['name'] for tool in tools)}")

    called = client.request(
        "tools/call", {"name": "lookup_account", "arguments": {"account_id": "GT-10231"}}
    )
    call_result = called.get("result", {})
    check("tools/call returns content blocks", isinstance(call_result.get("content"), list))
    check("isError is false on success", call_result.get("isError") is False)
    text = (call_result.get("content") or [{}])[0].get("text", "")
    check("the payload is JSON", _is_json(text), text[:120])
    check("non-ASCII survives the pipe", "í" in text or "á" in text, text[:120])

    print("\nErrors")
    domain = client.request(
        "tools/call", {"name": "lookup_account", "arguments": {"account_id": "GT-99999"}}
    )
    check(
        "a missing account is a successful result with isError true",
        "result" in domain and domain["result"].get("isError") is True,
        json.dumps(domain)[:200],
    )

    invalid = client.request("tools/call", {"name": "check_service_status", "arguments": {}})
    check(
        "a missing required argument is -32602",
        invalid.get("error", {}).get("code") == -32602,
        json.dumps(invalid)[:200],
    )

    unknown = client.request("resources/list")
    check(
        "an unknown method is -32601",
        unknown.get("error", {}).get("code") == -32601,
        json.dumps(unknown)[:200],
    )

    client.send({"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": "not an object"})
    malformed = json.loads(client.process.stdout.readline())
    check(
        "an ill-typed params field is rejected",
        "error" in malformed,
        json.dumps(malformed)[:200],
    )

    print("\nShutdown")
    code = client.close()
    check("the server exits cleanly when stdin closes", code == 0, f"exit code {code}")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except json.JSONDecodeError:
        return False


if __name__ == "__main__":
    sys.exit(main())
