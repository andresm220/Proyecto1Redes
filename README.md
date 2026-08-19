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
| F5 | `SPEC.md`, README, Claude Desktop validation | Pending |
