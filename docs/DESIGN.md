# Design decisions

This document records the decisions that the assignment brief left open, and why
each one was made. It is background material for the final technical report; the
graded server specification is `servers/netops/SPEC.md`.

## Layering

Three layers that never reach across each other:

```
host/main.py ──> Registry ──> MCPClient ──> StdioTransport ──┐
     │                                                       │ NDJSON over stdin/stdout
     └──> Agent ──> AnthropicClient (Messages API)           │
                                                             v
                                     servers/netops/stdio_server.py
                                             └──> core.py ──> data/
```

- `host/mcp/` knows nothing about Anthropic. It speaks JSON-RPC and MCP only.
- `servers/netops/core.py` knows nothing about JSON-RPC. It exposes tool
  descriptors and a dispatch function.
- The `*_server.py` modules are transport adapters over `core.py`.

The payoff is the remote phase: adding HTTP means adding `http_server.py` beside
`stdio_server.py` and `http_transport.py` beside `stdio_transport.py`. No
business logic moves.

`jsonrpc.py` is pure — it builds and parses envelopes and performs no I/O. That
is what makes it fully testable before any subprocess exists, which is why F1
precedes F2.

## Decisions

### Virtual environment: `venv` rather than conda

The brief suggests `conda create -n mcp-proj python=3.11`. Conda is not
installed on the development machine, and the project has no non-Python or
binary dependencies that would justify it. `python -m venv` with the already
installed Python 3.12 satisfies the stated 3.11+ requirement with no extra
tooling. The README documents both paths so a grader can use either.

### Persistence: read-only JSON seed plus a mutable state file

The brief allows JSON or SQLite. The data splits cleanly in two:

- `data/seed/` — accounts, outages and service metrics. Versioned in git, never
  written to at runtime. This is what makes results deterministic: metrics such
  as latency and SNR are derived from a hash of the `account_id` rather than
  from `random`, so the same account always reports the same values.
- `data/state.json` — tickets and scheduled visits, the only mutable state.
  Git-ignored, written atomically (temporary file plus `os.replace`) so an
  interrupted write cannot leave a truncated file behind.

SQLite would also have worked with no added dependency, but it makes the state
an opaque binary: nobody can inspect or hand-edit the fixture data without a SQL
client. Plain JSON keeps the demo and the grading inspectable. The split also
means a test run never dirties a versioned file, so there is no `git checkout`
needed to return to a clean fixture state.

### Schema validation: hand-written rather than `jsonschema`

Arguments are validated against each tool's `inputSchema` before the handler
runs; an invalid argument returns `-32602`. The `jsonschema` package is not in
the set of permitted dependencies, and the schemas in use need only `type`,
`required`, `enum` and `minLength` — roughly forty lines of code. Adding a
dependency to avoid writing them would not be a good trade.

### Protocol errors versus tool errors

The distinction the brief calls out is enforced in one place and documented in
`SPEC.md`:

| Situation | Response |
|---|---|
| Unknown method | `error`, `-32601` |
| Missing or ill-typed argument | `error`, `-32602` |
| Malformed JSON on a line | `error`, `-32700` |
| Malformed envelope (bad `jsonrpc`/`id`) | `error`, `-32600` |
| **Account does not exist** | **successful `result` with `isError: true`** |
| Unhandled exception in a handler | `error`, `-32603`, traceback to stderr |

A domain outcome the model should reason about is a successful protocol
exchange. Only a broken exchange is a protocol error.

### Windows specifics

The server subprocess is launched with `encoding="utf-8"` and
`PYTHONIOENCODING=utf-8` in its environment. The Windows default of `cp1252`
would corrupt any JSON payload containing accented characters, which the Spanish
service addresses in the seed data do contain.

`stderr` is drained on its own thread and printed with a prefix; it is never
parsed as protocol. A server that writes a traceback cannot corrupt the message
stream.
