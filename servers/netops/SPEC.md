# netops — MCP Server Specification

**Server name:** `netops`
**Version:** `1.0.0`
**Protocol version:** `2025-11-25`
**Transport:** stdio, NDJSON framing

Every JSON-RPC exchange shown in this document was captured from a running
server, not written by hand.

---

## 1. Purpose and industry use case

`netops` is the back office of an internet service provider's technical support
desk. It exposes the operations a first-line support agent performs while a
subscriber is on the phone: identify the account, read the current state of
their link, check whether a mass outage already explains the complaint, run a
diagnostic, and open or follow up a support ticket.

The use case was chosen because it is a real industry workflow whose value comes
from *sequencing* rather than from any single lookup. A complaint of "I have no
internet" is not answered by one query: the agent has to identify the subscriber,
read the link, and check regional outages before deciding whether a ticket is
even warranted. That makes it a good fit for an LLM-driven host, which can chain
the calls and reason about the results, and it keeps the server honest — each
tool does one thing and returns facts.

The data is simulated but deterministic. Link metrics (latency, packet loss,
SNR) are derived from a hash of the account id rather than from a random number
generator, so the same call always returns the same reading. Tickets and visits
are real persistent state, stored on disk and surviving a server restart.

### Data model

| Entity | Where it lives | Mutable |
|---|---|---|
| Accounts | `data/seed/accounts.json` | No — versioned, read-only |
| Outages | `data/seed/outages.json` | No — versioned, read-only |
| Tickets, visits | `data/state.json` | Yes — written atomically |

Five subscriber accounts (`GT-10231` … `GT-10235`) span four regions
(`guatemala`, `quetzaltenango`, `peten`, `escuintla`) and three account states
(`active`, `suspended`, `pending_installation`). Two outages are active and one
is resolved. The seed is arranged so the entities relate to each other: account
`GT-10233` sits in `peten`, where outage `OUT-2026-013` is active, so
`check_service_status` on that account reports the outage rather than a fault.

---

## 2. Transport and protocol version

The server speaks JSON-RPC 2.0 over stdio. It is launched as a subprocess by the
host and communicates through the standard streams.

| Aspect | Value |
|---|---|
| Protocol version | `2025-11-25` |
| Framing | NDJSON — one JSON message per line, terminated by `\n` |
| Encoding | UTF-8 |
| `stdout` | Protocol messages only |
| `stderr` | Server logs only, never parsed as protocol |
| `stdin` closed | Server exits cleanly |

**Framing is NDJSON, not the `Content-Length` header framing that LSP uses.**
A message must not contain an embedded newline; `json.dumps` escapes newlines
inside strings, so a payload containing line breaks stays on one line.

**No MCP SDK is used.** The envelopes, the handshake and the dispatch loop are
implemented by hand in `host/mcp/jsonrpc.py` and
`servers/netops/stdio_server.py`.

**Encoding note.** The server forces UTF-8 on all three standard streams at
startup. On Windows these default to `cp1252`, which corrupts any payload
containing accented characters — and the seed data contains several
(`María Fernanda Ochoa`, `Petén`).

---

## 3. Lifecycle

The handshake is mandatory. `tools/list` and `tools/call` are rejected with
`-32600` until it has completed.

```
Host                                                  Server
  |                                                     |
  |-- initialize (request, id=0) ---------------------->|
  |<------------------------- initialize result (id=0) -|
  |                                                     |
  |-- notifications/initialized (notification) -------->|
  |                                          (no reply) |
  |                                                     |
  |-- tools/list, tools/call, ping ...  --------------->|
```

### 3.1 initialize

The client announces the protocol version it supports and identifies itself.

**Request**

```json
{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "uvg-mcp-host", "version": "0.1.0"}}}
```

**Response**

```json
{"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {"listChanged": false}}, "serverInfo": {"name": "netops", "version": "1.0.0"}, "instructions": "Technical support back office for a Guatemalan ISP. Look up subscriber accounts, read live link metrics, check whether a regional outage already explains a complaint, run diagnostics, and open or follow up support tickets. Always check list_outages before opening a ticket for a connectivity complaint: if a mass outage already covers the subscriber's region, the ETA is the answer and a new ticket only adds noise."}}
```

`instructions` is guidance written for the model, and a host is expected to fold
it into its system prompt.

**Version mismatch.** If the client's `protocolVersion` differs from the
server's, the client must disconnect and report rather than proceed. This server
always answers with `2025-11-25`.

### 3.2 notifications/initialized

The client confirms the handshake. This is a **notification**: it carries no
`id` and is never answered.

```json
{"jsonrpc": "2.0", "method": "notifications/initialized"}
```

### 3.3 ping

Available at any time, including before the handshake.

```json
{"jsonrpc": "2.0", "id": 1, "method": "ping"}
{"jsonrpc": "2.0", "id": 1, "result": {}}
```

### 3.4 Shutdown

The host closes the server's `stdin`. The server's read loop reaches EOF, it
logs and exits with code 0. A host that needs to force the issue escalates:
close `stdin`, wait, `terminate()`, then `kill()`.

### 3.5 Supported methods

| Method | Kind | Requires handshake |
|---|---|---|
| `initialize` | request | — |
| `notifications/initialized` | notification | — |
| `ping` | request | No |
| `tools/list` | request | Yes |
| `tools/call` | request | Yes |

Any other method returns `-32601`.

---

## 4. Tools reference

Seven tools. Every schema below is the exact `inputSchema` the server returns
from `tools/list`.

Every successful call returns the same envelope:

```json
{"content": [{"type": "text", "text": "<JSON payload>"}], "isError": false}
```

The `text` block holds a JSON object, indented for readability. A domain failure
uses the same shape with `"isError": true` and a payload of
`{"error": "<message>", ...context}`.

---

### 4.1 `lookup_account`

Find a subscriber by account id or by phone number. Returns the plan, the
account status and the service address. Exactly one of `account_id` or `phone`
must be supplied.

```json
{
  "type": "object",
  "properties": {
    "account_id": {"type": "string", "description": "Account id, for example GT-10231.", "minLength": 3},
    "phone": {"type": "string", "description": "Phone number in any format; only digits are compared.", "minLength": 8}
  }
}
```

Phone numbers are compared by their digits only, so `+502 5555-0101`,
`50255550101` and `(502) 5555 0101` all resolve to the same account.

**Output**

| Field | Type | Notes |
|---|---|---|
| `account_id`, `holder`, `phone` | string | |
| `plan`, `plan_speed_mbps` | string, integer | Contracted plan |
| `status` | string | `active`, `suspended`, `pending_installation` |
| `technology` | string | `fiber`, `coaxial` |
| `region` | string | One of the four regions |
| `service_address` | string | |
| `installed_on` | string \| null | `null` when installation is pending |
| `suspension_reason` | string | Present only when `status` is `suspended` |

**Errors**

| Condition | Result |
|---|---|
| Neither `account_id` nor `phone` given | `-32602` |
| Both given | `-32602` |
| `account_id` shorter than 3 characters | `-32602` |
| No such account | `isError: true` |

---

### 4.2 `check_service_status`

Read the current link state for one account: latency, packet loss and SNR. If a
mass outage covers the account's region, it is reported here.

```json
{
  "type": "object",
  "properties": {
    "account_id": {"type": "string", "description": "Account id.", "minLength": 3}
  },
  "required": ["account_id"]
}
```

**Output**

| Field | Type | Notes |
|---|---|---|
| `link_state` | string | `up`, `degraded`, `down`, `not_provisioned` |
| `reason` | string | `healthy`, `packet_loss`, `mass_outage`, `administrative_suspension`, `pending_installation` |
| `detail` | string | Human-readable explanation |
| `outage_id`, `eta` | string | Present only when `reason` is `mass_outage` |
| `metrics` | object | `latency_ms`, `packet_loss_pct`, `snr_db`, `downstream_mbps`, `upstream_mbps` |

`reason` is evaluated in priority order: an administrative suspension outranks an
outage, which outranks a measured fault. Metrics are always returned, even when
the link is down, so the caller can distinguish "no signal" from "billing hold".

**Errors**

| Condition | Result |
|---|---|
| `account_id` missing | `-32602` |
| No such account | `isError: true` |

---

### 4.3 `list_outages`

List mass outages with their cause and estimated time of repair. Filter by
region, and by default only outages that are still active.

```json
{
  "type": "object",
  "properties": {
    "region": {"type": "string", "description": "Restrict to one region. Omit for every region.", "enum": ["guatemala", "quetzaltenango", "peten", "escuintla"]},
    "active_only": {"type": "boolean", "description": "Only outages still in progress. Defaults to true."}
  }
}
```

**Output**

`{"region": ..., "active_only": ..., "count": N, "outages": [...]}`, where each
outage carries `outage_id`, `region`, `status`, `cause`, `started_at`, `eta`,
`affected_accounts`, `affected_services`, and `resolved_at` once resolved.

**Errors**

| Condition | Result |
|---|---|
| `region` outside the enum | `-32602` |
| `active_only` not a boolean | `-32602` |

This tool has no domain failure mode: an empty region is a `count` of 0, not an
error.

---

### 4.4 `run_diagnostic`

Run a diagnostic against a subscriber line and return the measurements together
with a probable cause.

```json
{
  "type": "object",
  "properties": {
    "account_id": {"type": "string", "description": "Account id.", "minLength": 3},
    "test_type": {"type": "string", "description": "Which diagnostic to run.", "enum": ["ping", "speed", "line"]}
  },
  "required": ["account_id", "test_type"]
}
```

**Output** — `readings` depends on `test_type`:

| `test_type` | `readings` fields |
|---|---|
| `ping` | `latency_ms`, `jitter_ms`, `packet_loss_pct`, `packets_sent` |
| `speed` | `downstream_mbps`, `upstream_mbps`, `contracted_mbps`, `pct_of_plan` |
| `line` | `snr_db`, `attenuation_db`, `technology`, `sync_errors_last_hour` |

`probable_cause` is always present and is derived from account state, active
outages and the measured values, in that order.

**Errors**

| Condition | Result |
|---|---|
| `test_type` outside the enum | `-32602` |
| Either argument missing | `-32602` |
| No such account | `isError: true` |

---

### 4.5 `open_ticket`

Open a support ticket against an account and return its id.

```json
{
  "type": "object",
  "properties": {
    "account_id": {"type": "string", "description": "Account id.", "minLength": 3},
    "category": {"type": "string", "description": "What the ticket is about.", "enum": ["connectivity", "speed", "billing", "equipment", "installation"]},
    "description": {"type": "string", "description": "What the subscriber reported, in their own terms.", "minLength": 5, "maxLength": 2000},
    "priority": {"type": "string", "description": "Ticket priority. Defaults to normal.", "enum": ["low", "normal", "high", "critical"]}
  },
  "required": ["account_id", "category", "description"]
}
```

Ticket ids are sequential and zero-padded: `TCK-00001`, `TCK-00002`. `priority`
defaults to `normal`. The ticket is written to disk before the response is sent,
so a subsequent `get_ticket` — in this session or a later one — sees it.

**Output** — `ticket_id`, `status` (always `open`), `account_id`, `category`,
`priority`, `created_at`.

**Errors**

| Condition | Result |
|---|---|
| Any required argument missing | `-32602` |
| `category` or `priority` outside its enum | `-32602` |
| `description` shorter than 5 characters | `-32602` |
| No such account | `isError: true` |

---

### 4.6 `get_ticket`

Read a ticket's current status, its history, and any scheduled visit.

```json
{
  "type": "object",
  "properties": {
    "ticket_id": {"type": "string", "description": "Ticket id, for example TCK-00001.", "minLength": 5}
  },
  "required": ["ticket_id"]
}
```

**Output** — the full ticket: `ticket_id`, `account_id`, `category`,
`description`, `priority`, `status`, `created_at`, `updated_at`, `history`, and
`visit` when one has been scheduled. `history` is an append-only list of
`{at, status, note}` entries.

**Errors**

| Condition | Result |
|---|---|
| `ticket_id` missing or shorter than 5 characters | `-32602` |
| No such ticket | `isError: true` |

---

### 4.7 `schedule_visit`

Schedule a technician visit against an existing ticket.

```json
{
  "type": "object",
  "properties": {
    "ticket_id": {"type": "string", "description": "Ticket id.", "minLength": 5},
    "date": {"type": "string", "description": "Visit date as YYYY-MM-DD.", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
    "time_window": {"type": "string", "description": "Arrival window.", "enum": ["08:00-12:00", "12:00-16:00", "16:00-20:00"]}
  },
  "required": ["ticket_id", "date", "time_window"]
}
```

Scheduling against a ticket that already has a visit reschedules it, and the
response carries `"rescheduled": true`. The ticket's status moves to `scheduled`
and a history entry is appended.

**Output** — `ticket_id`, `account_id`, `date`, `time_window`, `status`
(`scheduled`), `scheduled_at`, `rescheduled`, `message`.

**Errors** — this tool shows the protocol/domain boundary most clearly:

| Condition | Result | Why |
|---|---|---|
| `date` is `25/08/2026` | `-32602` | Violates the schema's `pattern` |
| `date` is `2026-02-30` | `-32602` | Right shape, not a real calendar date |
| `date` is in the past | `isError: true` | Valid argument, rejected by a business rule |
| `time_window` outside the enum | `-32602` | |
| No such ticket | `isError: true` | Well-formed call, missing entity |

---

## 5. Raw JSON-RPC examples

Captured from a live server.

### 5.1 `lookup_account` — success

```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "lookup_account", "arguments": {"account_id": "GT-10231"}}}
```

```json
{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "{\n  \"account_id\": \"GT-10231\",\n  \"holder\": \"María Fernanda Ochoa\",\n  \"phone\": \"+502 5555-0101\",\n  \"plan\": \"Fibra 300\",\n  \"plan_speed_mbps\": 300,\n  \"status\": \"active\",\n  \"technology\": \"fiber\",\n  \"region\": \"guatemala\",\n  \"service_address\": \"12 Avenida 5-43, Zona 10, Ciudad de Guatemala\",\n  \"installed_on\": \"2024-03-11\"\n}"}], "isError": false}}
```

### 5.2 `check_service_status` — an outage explains the complaint

```json
{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "check_service_status", "arguments": {"account_id": "GT-10233"}}}
```

```json
{"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "{\n  \"account_id\": \"GT-10233\",\n  \"account_status\": \"active\",\n  \"region\": \"peten\",\n  \"link_state\": \"down\",\n  \"reason\": \"mass_outage\",\n  \"detail\": \"Corte de fibra troncal por trabajos viales sobre la ruta a Flores\",\n  \"outage_id\": \"OUT-2026-013\",\n  \"eta\": \"2026-08-19T18:00:00-06:00\",\n  \"metrics\": {\n    \"latency_ms\": 31,\n    \"packet_loss_pct\": 0.6,\n    \"snr_db\": 34.2,\n    \"downstream_mbps\": 42.0,\n    \"upstream_mbps\": 23.0\n  }\n}"}], "isError": false}}
```

### 5.3 `open_ticket` — creating persistent state

```json
{"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "open_ticket", "arguments": {"account_id": "GT-10233", "category": "connectivity", "description": "Sigue sin servicio despues del ETA publicado.", "priority": "high"}}}
```

```json
{"jsonrpc": "2.0", "id": 4, "result": {"content": [{"type": "text", "text": "{\n  \"ticket_id\": \"TCK-00001\",\n  \"status\": \"open\",\n  \"account_id\": \"GT-10233\",\n  \"category\": \"connectivity\",\n  \"priority\": \"high\",\n  \"created_at\": \"2026-08-19T22:44:37+00:00\"\n}"}], "isError": false}}
```

### 5.4 `lookup_account` — a domain error, not a protocol error

The account does not exist. The exchange itself succeeded, so this is a
**successful response** carrying `isError: true`.

```json
{"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "lookup_account", "arguments": {"account_id": "GT-99999"}}}
```

```json
{"jsonrpc": "2.0", "id": 5, "result": {"content": [{"type": "text", "text": "{\n  \"error\": \"No se encontró ninguna cuenta con account_id='GT-99999'.\",\n  \"account_id\": \"GT-99999\"\n}"}], "isError": true}}
```

### 5.5 `check_service_status` — a protocol error

The required argument is missing, so the call never matched the advertised
schema. This is an `error` response, not a result.

```json
{"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "check_service_status", "arguments": {}}}
```

```json
{"jsonrpc": "2.0", "id": 6, "error": {"code": -32602, "message": "account_id: is required", "data": {"field": "account_id"}}}
```

### 5.6 Unknown method

```json
{"jsonrpc": "2.0", "id": 7, "method": "resources/list"}
```

```json
{"jsonrpc": "2.0", "id": 7, "error": {"code": -32601, "message": "Method not found: resources/list"}}
```

---

## 6. Error codes

### 6.1 The distinction that governs everything

A **protocol error** means the exchange was broken: the message could not be
parsed, the envelope was invalid, the method does not exist, or the arguments do
not match the advertised schema. It is reported in the JSON-RPC `error` field
and there is no `result`.

A **tool error** means the exchange was fine and the operation could not
succeed: an account that does not exist, a ticket that was never opened, a date
in the past. It is reported as a **successful** `result` whose `isError` field is
`true`. The model is expected to read it and react — so it is data, not a
transport failure.

Collapsing the two would break the host in both directions: a missing account
would look like a broken connection, and a malformed request would look like a
business outcome the model should reason about.

### 6.2 Codes

| Code | Name | Raised when |
|---|---|---|
| `-32700` | Parse error | A line on stdin is not valid JSON. The request id cannot be recovered, so the response carries `"id": null` — the only case where a null id is legal |
| `-32600` | Invalid Request | Valid JSON but not a valid JSON-RPC 2.0 envelope: wrong `jsonrpc` value, non-string method, ill-typed id. Also returned when `tools/list` or `tools/call` is called before the handshake |
| `-32601` | Method not found | Any method outside the five supported ones |
| `-32602` | Invalid params | `params.name` missing or not a string; an unknown tool name; any argument that fails schema validation |
| `-32603` | Internal error | An unhandled exception in a handler. The traceback goes to `stderr`; the client receives the exception type and message |

**Unknown tool names return `-32602`, not `isError`.** The tool name is a
parameter of `tools/call`, so asking for one that `tools/list` does not
advertise means the request did not match the server's published capabilities.

Validation runs **before** any handler executes, so an invalid argument can
never reach the business logic.

### 6.3 The `data` field

`-32602` responses carry a `data` object identifying the offending argument:

```json
{"code": -32602, "message": "account_id: is required", "data": {"field": "account_id"}}
```

For `require_one_of` violations, `data` is `{"expected_one_of": [...]}` instead.

---

## 7. How to run and how to connect a client

### 7.1 Requirements

Python 3.11 or newer. No third-party package is needed to run the server itself
— it uses only the standard library plus this repository's own
`host/mcp/jsonrpc.py`.

### 7.2 Running standalone

From the repository root:

```bash
python -m servers.netops.stdio_server
```

The server reads NDJSON from stdin. Paste a handshake to try it by hand:

```json
{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"manual","version":"1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":1,"method":"tools/list"}
```

Close stdin (Ctrl+Z then Enter on Windows, Ctrl+D on Unix) to shut it down.

### 7.3 Connecting from this repository's host

`config/servers.json` already declares it:

```json
{
  "mcpServers": {
    "netops": {
      "command": "python",
      "args": ["-m", "servers.netops.stdio_server"],
      "env": {}
    }
  }
}
```

Then `python -m host.main`, and:

```
/tools
/call netops__lookup_account {"account_id": "GT-10231"}
```

The host namespaces tools as `<server>__<tool>`.

### 7.4 Connecting from Claude Code

```
claude mcp add netops -- python -m servers.netops.stdio_server
claude mcp get netops
```

Run both from the repository root: Claude Code launches the server with the
working directory it was itself started in, so `servers.netops.stdio_server`
resolves without any extra configuration. `claude mcp get` performs a health
check — it starts the server and completes the handshake — and reports
`Status: ✔ Connected` on success. Remove it again with
`claude mcp remove netops`.

This server has been verified this way.

### 7.5 Connecting from Claude Desktop

Add the entry below to `claude_desktop_config.json`, which lives at
`%APPDATA%\Claude\claude_desktop_config.json` on Windows and
`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS.
Restart Claude Desktop afterwards.

```json
{
  "mcpServers": {
    "netops": {
      "command": "python",
      "args": ["-m", "servers.netops.stdio_server"],
      "cwd": "<absolute path to this repository>"
    }
  }
}
```

`cwd` must be the repository root so that `servers.netops.stdio_server` resolves
as a module. If the repository's virtual environment is in use, point `command`
at that interpreter (`<repo>\.venv\Scripts\python.exe`) rather than at a bare
`python`.

### 7.6 Writing your own client

1. Launch the server as a subprocess with pipes on stdin and stdout. Force
   UTF-8 on the child (`PYTHONIOENCODING=utf-8`), and set the working directory
   to the repository root.
2. Send `initialize`, wait for the response, and compare its `protocolVersion`
   against yours. If they differ, disconnect and report.
3. Send the `notifications/initialized` notification. Do not wait for a reply —
   notifications are never answered.
4. Call `tools/list`, then `tools/call` as needed.
5. Read `stderr` on a separate thread and never parse it as protocol.
6. To shut down, close the server's stdin and wait for it to exit.

### 7.7 Data directory

The server reads its seed from `servers/netops/data/seed/` and writes state to
`servers/netops/data/state.json`. Set `NETOPS_DATA_DIR` to relocate both — the
test suite uses this to give every test its own copy, and a second client can use
it to keep its state separate from the repository's.
