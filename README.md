# uvg-mcp-host

A Model Context Protocol host and server built from scratch for **CC3067 Redes**
(Universidad del Valle de Guatemala).

**No MCP SDK is used anywhere in this repository.** JSON-RPC 2.0 framing, the
handshake, requests, notifications, responses and errors are all implemented by
hand. The only third-party runtime dependencies are the Anthropic SDK (the LLM
API client, not an MCP library), `rich`, `python-dotenv` and `pytest`.

- **Protocol version:** `2025-11-25`
- **Transport:** stdio, NDJSON framing (one JSON message per line, UTF-8)

## Layout

```
config/servers.json     Declares which MCP servers the host launches
host/                   The MCP host (client side)
  mcp/                  Hand-written JSON-RPC + transport + session lifecycle
  llm/                  Anthropic Messages API wrapper
servers/netops/         Our own MCP server: ISP technical support
  core.py               Business logic and tool schemas (transport-agnostic)
  stdio_server.py       stdio transport adapter over core.py
tests/                  pytest suite
```

The business logic lives in `core.py` and the `*_server.py` modules are thin
adapters, so adding a remote HTTP transport later means adding one file rather
than copying the server.

## Requirements

- Python 3.11 or newer (developed on 3.12)
- An Anthropic API key, for the agentic loop only

## Setup

The project uses a standard virtual environment. From the repository root:

```powershell
# Windows / PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```bash
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If you prefer conda, `conda create -n mcp-proj python=3.11` followed by the same
`pip install -r requirements.txt` works identically.

Then copy the environment template and add your key:

```
cp .env.example .env       # copy .env.example .env   on Windows
```

`.env` is git-ignored and is never committed. The key is only needed for the
agentic loop; tool calls made with `/call` work without it.

## Running

```
python -m host.main
```

Commands:

| Command | Description |
|---|---|
| `/servers` | List the configured MCP servers and their connection state |
| `/tools` | List every tool exposed by the connected servers |
| `/call <tool> <json>` | Invoke one tool directly, bypassing the LLM |
| `/verbose` | Toggle the JSON-RPC message trace (on by default) |
| `/reset` | Forget the conversation so far |
| `/help` | Show the command table |
| `/quit` | Close every server and exit |

Anything that does not start with `/` is sent to the model, which may call the
servers' tools to answer. Tools are namespaced as `<server>__<tool>`, so
`/call netops__lookup_account {"account_id": "GT-10231"}` invokes `lookup_account`
on the `netops` server.

The assistant needs `ANTHROPIC_API_KEY` and a funded account; `/call` does not.

## Tests

```
python -m pytest -q
```

The suite launches the real server as a subprocess and speaks to it over real
pipes; nothing about the transport is mocked.

### Conformance check

`tools/conformance_check.py` is an independent client: standard library only,
raw `json`, and no import from `host/`. It plays the part a third-party host
plays — launch, handshake, list, call, shut down — and it needs no dependencies,
so it runs on a bare Python:

```
python tools/conformance_check.py
```

Because it shares no code with the host, a bug in our own encoder cannot cancel
itself out and let the check pass anyway.

## Connecting Claude Desktop

The server is a normal MCP server, so any MCP host can drive it. For Claude
Desktop, add this to `claude_desktop_config.json`
(`%APPDATA%\Claude\claude_desktop_config.json` on Windows,
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS) and
restart the app:

```json
{
  "mcpServers": {
    "netops": {
      "command": "python",
      "args": ["-m", "servers.netops.stdio_server"],
      "cwd": "C:\\Users\\aame2\\OneDrive\\Documentos\\Proyecto1Redes"
    }
  }
}
```

`cwd` must be the repository root so the module resolves. The server needs no
third-party packages, so a system Python works; point `command` at
`.venv\Scripts\python.exe` if you would rather use the virtual environment.

## Documentation

- `servers/netops/SPEC.md` — the netops server specification: tools, schemas,
  raw request/response examples, and error codes
- `docs/DESIGN.md` — design decisions and their rationale

## Build status

| Phase | Scope | Status |
|---|---|---|
| F0 | Scaffolding, dependencies, configuration | Done |
| F1 | `jsonrpc.py` and its tests | Done |
| F2 | `stdio_transport.py` and `MCPClient` | Done |
| F3 | The `netops` server over stdio | Done |
| F4 | Minimal host and agentic loop | Done (not yet run against a funded account) |
| F5 | `SPEC.md`, README, conformance check | Done |

### Third-party validation

The server has been connected successfully from **Claude Code**, an MCP host
written independently of this repository:

```
$ claude mcp get netops
netops:
  Scope: Local config (private to you in this project)
  Status: ✔ Connected
  Type: stdio
  Command: C:\Python312\python.exe
  Args: -m servers.netops.stdio_server
```

A production host completing the handshake against a protocol implementation
written by hand is the strongest conformance evidence available here.

One item still stands open: the agentic loop in `host/agent.py` has not been
exercised against a funded Anthropic account. The loop is covered by tests that
drive it with a scripted model against the real servers, but no live model has
chosen a tool on its own.
