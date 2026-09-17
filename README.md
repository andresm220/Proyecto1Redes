# uvg-mcp-host

A Model Context Protocol host and server built from scratch for **CC3067 Redes**
(Universidad del Valle de Guatemala).

**No MCP SDK is used anywhere in this repository.** JSON-RPC 2.0 framing, the
handshake, requests, notifications, responses and errors are all implemented by
hand. The only third-party runtime dependencies are the Anthropic SDK (an LLM
API client, not an MCP library), `httpx`, `rich`, `python-dotenv` and `pytest`.

The agentic loop is provider-agnostic. It runs against the Anthropic Messages
API or against any OpenAI-compatible endpoint — Groq, OpenRouter, or a local
runtime such as Ollama — selected with one line in `.env`. See
[Choosing a model backend](#choosing-a-model-backend).

- **Protocol version:** `2025-11-25`
- **Transports:** stdio with NDJSON framing, and Streamable HTTP — the same
  server reached either way

## Layout

```
config/servers.json     Declares which MCP servers the host connects to
host/                   The MCP host (client side)
  mcp/                  Hand-written JSON-RPC + transport + session lifecycle
  llm/                  Provider adapters for the agentic loop
  logging/              The JSONL session log
servers/netops/         Our own MCP server: ISP technical support
  core.py               Business logic and tool schemas
  protocol.py           Handshake, method table, error mapping - no transport
  stdio_server.py       stdio adapter
  http_server.py        Streamable HTTP adapter
Dockerfile              Builds the HTTP adapter for a remote deployment
workspace/              Scratch area the official Filesystem and Git servers use
tests/                  pytest suite
```

The business logic lives in `core.py`, the protocol state machine in
`protocol.py`, and the `*_server.py` modules are adapters that only move bytes.
Neither adapter holds a method table or a handshake flag of its own, which is
what makes "the same server, deployed remotely" true rather than aspirational.

The host mirrors that split: `Transport` is a four-method interface, and
`registry.py` is the only place that knows a server can be remote at all.
`MCPClient`, the tool table and the agentic loop see one interface and cannot
tell which transport they were handed.

## Requirements

- Python 3.11 or newer (developed on 3.12)
- An API key for one model provider, for the agentic loop only
- **Node.js**, for the official Filesystem server (`npx`)
- **uv**, for the official Git server (`uvx`) — `pip install uv`

Both official servers are downloaded on first use and run as external
processes. Neither is imported as a library: consuming them as binaries is what
requirement 4 asks for, and no MCP SDK enters this project either way.

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

`.env` is git-ignored and is never committed. A key is only needed for the
agentic loop; tool calls made with `/call` work without one.

## Choosing a model backend

`LLM_PROVIDER` selects the backend. Everything except `anthropic` speaks the
OpenAI `/chat/completions` dialect, so a single adapter serves them all and
switching is a one-line change.

| `LLM_PROVIDER` | Reads | Notes |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Requires a funded account |
| `groq` | `GROQ_API_KEY`, `GROQ_MODEL` | Free tier, hosts open-weight models |
| `openrouter` | `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | |
| `ollama`, `lmstudio` | `LLM_MODEL` | Local runtime, no key needed |
| `openai_compatible` | `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | Anything else |

Base URLs for the known providers are built in; only `openai_compatible` needs
`LLM_BASE_URL` spelled out.

Free tiers rate-limit on tokens per minute, and the seven `netops` tool schemas
are large enough to trip that on a multi-step turn. When a provider answers 429
with a stated wait, the client honours it and retries rather than failing the
turn — expect visible pauses rather than errors.

The tool descriptors are translated per provider: MCP calls the schema
`inputSchema`, the Anthropic Messages API calls it `input_schema`, and the
OpenAI dialect nests it as `function.parameters`. Each adapter owns its own
translation, so `host/agent.py` never sees a vendor's shape.

## Running

```
python -m host.main
```

| Option | Effect |
|---|---|
| `--config PATH` | Server declarations to load (default: `config/servers.json`) |
| `--log-dir PATH` | Where to write the JSONL session log (default: `logs/`) |
| `-v`, `--verbose` | Show the live JSON-RPC trace (the default) |
| `-q`, `--quiet` | Start with the live trace off; the file log is written either way |

Commands:

| Command | Description |
|---|---|
| `/servers` | List the configured MCP servers and their connection state |
| `/tools` | List every tool exposed by the connected servers |
| `/call <tool> <json>` | Invoke one tool directly, bypassing the LLM |
| `/log` | Where the session log is, and how much is in it |
| `/log tail <n>` | Replay the last n MCP messages (default 20) |
| `/verbose` | Toggle the live JSON-RPC message trace |
| `/history` | Show the conversation turn by turn |
| `/save <file>` | Write the conversation to a JSON file |
| `/reset` | Forget the conversation so far |
| `/help` | Show the command table |
| `/quit` | Close every server and exit |

Anything that does not start with `/` is sent to the model, which may call the
servers' tools to answer. Tools are namespaced as `<server>__<tool>`, so
`/call netops__lookup_account {"account_id": "GT-10231"}` invokes `lookup_account`
on the `netops` server.

The assistant needs a configured model backend; `/call` does not.

## The session log

Every MCP message, in both directions and from every server, is appended to
`logs/mcp-YYYYMMDD-HHMMSS.jsonl` as one JSON object per line:

```json
{"ts":"2026-08-28T18:40:29.747+00:00","direction":"out","server":"netops",
 "transport":"stdio","type":"request","method":"tools/call","id":3,
 "payload":{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{...}}}
```

The `type` is derived from the envelope rather than declared by the caller:
`method` with an `id` is a request, `method` alone a notification, `result` a
response, `error` an error. Those are the same four categories the packet
capture analysis uses.

A response also carries `duration_ms`, correlated back to the request with the
matching `id` on the same server. It is measured with `perf_counter`, not
`monotonic`, because `monotonic` advances in ~15 ms steps on Windows and would
round every local stdio round trip to 0.

The payload is stored whole and every line is flushed as it is written, so a
session that ends in a crash is still readable afterwards. The file log is
independent of `--quiet`: silencing the console view must not put holes in the
evidence.

Read it back without leaving the chat:

```
> /log tail 4
                        Last 4 MCP message(s)
+--------------+----+--------+----------+------------+----+----------+
| Time         |    | Server | Type     | Method     | Id | Duration |
+--------------+----+--------+----------+------------+----+----------+
| 18:40:29.747 | -> | netops | request  | tools/list |  2 |          |
| 18:40:29.750 | <- | netops | response | tools/list |  2 |   2.9 ms |
| 18:40:32.085 | -> | netops | request  | tools/call |  3 |          |
| 18:40:32.091 | <- | netops | response | tools/call |  3 |   1.2 ms |
+--------------+----+--------+----------+------------+----+----------+
```

A response has no method of its own, so the view labels it with the method of
the request it answers - a trace of lines reading `response id=3` is not
readable. Direction is carried by the arrow as well as by the colour, so the
trace survives a colour-blind reader and a black-and-white printout.

`logs/` is git-ignored; a curated session is committed under `docs/` as
evidence instead of every run.

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

## Connected servers

`config/servers.json` declares three, all over stdio:

| Alias | Transport | Origin | What it is |
|---|---|---|---|
| `netops` | stdio | ours | ISP technical support: 7 tools, see `servers/netops/SPEC.md` |
| `filesystem` | stdio | official | `@modelcontextprotocol/server-filesystem`, rooted at `./workspace` |
| `git` | stdio | official | `mcp-server-git`, on `./workspace/demo-repo` |

Together they expose 33 tools, namespaced `<server>__<tool>` so two servers can
use the same tool name without colliding.

A fourth entry connects the same `netops` server over HTTP. It is deployed to
Cloud Run and live:

| Alias | Transport | Origin | What it is |
|---|---|---|---|
| `netops-remote` | http | ours | the same server, on Cloud Run |

```
https://netops-mcp-261683462697.us-central1.run.app/mcp
```

```
Connected to 4/4 server(s), 40 tool(s) available.

│ netops        │ stdio │ python -m servers.netops.stdio_server │ connected │
│ filesystem    │ stdio │ npx -y @modelcontextprotocol/…        │ connected │
│ git           │ stdio │ uvx mcp-server-git --repository …     │ connected │
│ netops-remote │ http  │ https://netops-mcp-…run.app/mcp       │ connected │
```

To try the HTTP transport without touching the cloud, run the adapter locally
and point the URL at `http://127.0.0.1:8080/mcp`:

```bash
uvicorn servers.netops.http_server:app --port 8080
```

### What the timings show

`docs/cloud-run-session.jsonl` is one session across all four servers and both
transports, every message tagged with the one that carried it:

```
netops         stdio  initialize    353.3 ms
filesystem     stdio  initialize   9195.7 ms      npx resolving its package
git            stdio  initialize   6435.0 ms      uvx resolving its package
netops-remote  http   initialize   2727.9 ms      Cloud Run cold start
netops-remote  http   tools/list     97.6 ms      steady state, over the internet
netops-remote  http   tools/call     98.3 ms
```

The remote server's *cold start* is faster than launching the local `npx` one.
stdio pays to spawn a process and, for `npx` and `uvx`, to resolve a package;
HTTP pays one round trip to a container the platform brings up. Once warm, a
call across the internet costs about 98 ms against roughly 1 ms over a pipe —
which is the honest trade, and the reason the transport is a choice rather than
a detail.

Windows needs `npx` to run through the command interpreter, because it ships as
`npx.cmd` and `CreateProcess` does not consult `PATHEXT`. That wrapper lives in
`build_argv` in `host/mcp/stdio_transport.py`, keyed on the command name, so
`config/servers.json` stays free of platform detail and works unchanged on
Windows, macOS and Linux. `uvx` is a real executable and is deliberately not
wrapped.

Before the first run, create the workspace the two official servers operate on:

```bash
mkdir -p workspace/demo-repo
git -C workspace/demo-repo init
```

## Documentation

- `servers/netops/SPEC.md` — the netops server specification: tools, schemas,
  raw request/response examples, and error codes
- `docs/demo-filesystem-git.md` — the Filesystem + Git scenario: one turn that
  writes a file, stages it and commits it, with the session log alongside it
- `docs/mixed-transport-session.jsonl` — one session across four servers and
  both transports, with the HTTP server running locally
- `docs/cloud-run-session.jsonl` — the same, with the HTTP server on Cloud Run
- `Dockerfile` — builds the HTTP adapter; `servers/netops/SPEC.md` §7.9 covers
  the container and the Cloud Run deployment

## Verifying it

`tools/conformance_check.py` is a deliberately foreign client: standard library
only, importing nothing from `host/`, so a bug in our own encoder cannot cancel
itself out. The same 19 checks run against either transport:

```bash
python tools/conformance_check.py                       # stdio
python tools/conformance_check.py --http http://localhost:8080/mcp
python tools/conformance_check.py --http https://netops-mcp-261683462697.us-central1.run.app/mcp
```

All three pass 19/19 — local subprocess, local container, and Cloud Run. That
is what makes "the same server, deployed remotely" an observation rather than a
claim about the source tree.
- `docs/reporte-avance.pdf` — the partial-delivery report submitted for the
  course, in Spanish, with its evidence screenshots under `docs/img/`

## Build status

| Phase | Scope | Status |
|---|---|---|
| F0 | Scaffolding, dependencies, configuration | Done |
| F1 | `jsonrpc.py` and its tests | Done |
| F2 | `stdio_transport.py` and `MCPClient` | Done |
| F3 | The `netops` server over stdio | Done |
| F4 | Minimal host and agentic loop | Done |
| F5 | `SPEC.md`, README, conformance check | Done |

### Third-party validation

The server has been connected successfully from two MCP hosts written
independently of this repository.

**Claude Desktop** (1.32885.1.0) negotiates the protocol and discovers all seven
tools. From its own log:

```
[LocalMcpServerManager] netops negotiated protocol version: 2025-11-25
[LocalMcpServerManager] Connected to netops (7 tools)
[localMcpBridge] announcing netops: 7 tool(s)
```

And from the server's side of the same exchange:

```
[netops] listening on stdio, protocol 2025-11-25
[netops] initialize from claude-ai 0.1.0
[netops] handshake complete
```

**Claude Code** connects the same way:

```
$ claude mcp get netops
netops:
  Status: ✔ Connected
  Type: stdio
```

A production host completing the handshake and listing the tools against a
protocol implementation written by hand is the strongest conformance evidence
available here.

> **Windows note.** The Microsoft Store build of Claude Desktop virtualises
> `%APPDATA%`, so its configuration is **not** at `%APPDATA%\Claude\`. The file
> it actually reads is
> `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`.
> Closing the window does not reload it either — the app stays resident, and a
> second launch logs `Not main instance, returning early` and exits. Quit from
> the tray, or end every `Claude.exe` process, before expecting a config change
> to apply.

### The agentic loop, end to end

The loop has been run against a live model. Asked in Spanish why account
GT-10233 has no service, the model chose `lookup_account`, read the region off
the result, chained `list_outages` filtered to that region, and concluded that
no ticket was warranted because a mass outage already covered the subscriber —
which is the guidance the server itself supplies in its `initialize`
`instructions`.

That run used Groq with `openai/gpt-oss-120b`. The Anthropic path is
implemented and covered by tests but has not been run against a funded account.
