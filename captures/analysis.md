# Packet capture analysis

Two captures, taken with `tshark` while `tools/conformance_check.py` drove a
complete MCP session — handshake, `tools/list`, four `tools/call`, three
deliberate errors, and an explicit session teardown.

| File | Packets | What it is for |
|---|---|---|
| `mcp_local_plaintext.pcapng` | 161 | The JSON-RPC messages, in the clear |
| `mcp_cloudrun_tls.pcapng` | 248 | The link, network and transport layers, for real |

Both files were filtered before being committed: the first to `tcp.port == 8080`,
the second to the Cloud Run flow only. Nothing else that was on the wire at the
time is in them.

## Why two captures and not one

This is an engineering decision with a reason, not a shortcut, and it is worth
stating plainly.

**Cloud Run serves HTTPS only.** Google terminates TLS, and `*.run.app` cannot
be addressed over plain HTTP. A capture against the real deployment therefore
shows the TLS handshake and then `Application Data` — you can see that a
conversation happened, and how many bytes it took, but not which message was
`tools/call`. That is precisely what requirement 7 asks to be identified.

**Loopback gives plaintext but has no link layer.** Running the same server
over plain HTTP locally and capturing on Npcap's loopback adapter recovers
every JSON-RPC message, but the frames carry no Ethernet header at all:

```
Encapsulation type: NULL/Loopback (15)
[Protocols in frame: null:ip:tcp]
```

There are no MAC addresses to report, because no frame was ever built.

**The third option was rejected.** Plaintext over a real Ethernet segment would
have needed the server reachable from another host — a second machine, or the
WSL guest reaching back into Windows. The latter requires opening an inbound
port in the Windows firewall, and weakening the host's firewall for the
convenience of a class exercise is not a trade worth making.

So each capture answers the question it can answer honestly. The first carries
the application layer; the second carries everything beneath it. Taken
together they cover all four layers, and the split itself is informative: it is
a concrete demonstration that TLS is what stands between an observer and the
protocol.

---

# Capture 1 — the JSON-RPC messages

`mcp_local_plaintext.pcapng`, loopback, `tcp.port == 8080`, 161 packets over 11
TCP connections.

## Classification

Nineteen JSON-RPC messages were recovered. Every one of them falls into one of
the three categories the assignment names.

| Frame | Category | Kind | Method / detail | id |
|---:|---|---|---|---:|
| 6 | synchronisation | request | `initialize` | 1 |
| 12 | response | response | | 1 |
| 21 | synchronisation | **notification** | `notifications/initialized` | — |
| 36 | synchronisation | request | `ping` | 2 |
| 42 | response | response | | 2 |
| 53 | request | request | `tools/list` | 3 |
| 59 | response | response | | 3 |
| 70 | request | request | `tools/call` (`lookup_account`) | 4 |
| 77 | response | response | | 4 |
| 88 | request | request | `tools/call` (`lookup_account`) | 5 |
| 94 | response | response | | 5 |
| 103 | request | request | `tools/call` (`check_service_status`) | 6 |
| 109 | response | **error** | `-32602` account_id is required | 6 |
| 120 | request | request | `resources/list` | 7 |
| 126 | response | **error** | `-32601` Method not found | 7 |
| 137 | request | request | `tools/call`, `params` is a string | 99 |
| 143 | response | **error** | `-32600` params must be an object | 99 |
| 166 | synchronisation | request | `ping` after `DELETE` | 8 |
| 170 | response | **error** | `-32600` unknown or expired session | 8 |

**Totals:** 4 synchronisation, 6 request, 9 response (5 successful, 4 errors).

Three of the five reserved JSON-RPC error codes appear in this single session:
`-32600`, `-32601` and `-32602`.

## The notification is visible as a notification

Frame 21 carries `notifications/initialized`, and the answer in the capture is:

```
HTTP/1.1 202 Accepted
content-length: 0
```

A notification has no `id` and is never answered, so the server owes no JSON-RPC
message — and says so with a status code and an empty body rather than
inventing an empty result. Every request in the same capture is answered `200`
with a body. The distinction between the two message types is therefore
observable on the wire without reading a single byte of JSON.

## One exchange in full

Stream 3, reassembled (`tshark -z follow,tcp,ascii,3`):

```http
POST /mcp HTTP/1.1
Accept-Encoding: identity
Content-Length: 51
Host: 127.0.0.1:8080
User-Agent: Python-urllib/3.12
Content-Type: application/json
Accept: application/json, text/event-stream
Mcp-Session-Id: d4e3271d8a84425aaee04e287afb091c
Mcp-Protocol-Version: 2025-11-25
Connection: close

{"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
```

```http
HTTP/1.1 200 OK
date: Thu, 17 Sep 2026 04:55:11 GMT
server: uvicorn
content-type: application/json
content-length: 3680

{"jsonrpc":"2.0","id":3,"result":{"tools":[{"name":"lookup_account", ... }]}}
```

Both MCP headers are present and correct: `Mcp-Session-Id` carries the id the
server issued at `initialize`, and `Mcp-Protocol-Version` states the revision
the session negotiated. The `Accept` header offers both response shapes the
specification allows; this server always chooses the single JSON object.

The `tools/list` result is 3,680 bytes — larger than one TCP segment, which is
where the transport layer becomes visible. See segmentation below.

---

# Capture 2 — the layers beneath

`mcp_cloudrun_tls.pcapng`, Wi-Fi, `192.168.0.29 ↔ 34.143.73.2`, 248 packets
over 11 TCP connections, 14.1 seconds.

## Link layer — Ethernet II

| Frame | Source MAC | Destination MAC | Direction |
|---:|---|---|---|
| 280 | `3c:21:9c:ab:a2:61` | `08:95:2a:e2:fb:cb` | out |
| 281 | `08:95:2a:e2:fb:cb` | `3c:21:9c:ab:a2:61` | in |

**Neither MAC belongs to the Cloud Run server, and that is the point.** A MAC
address has meaning only inside one broadcast domain. `3c:21:9c:ab:a2:61` is
this machine's Wi-Fi adapter and `08:95:2a:e2:fb:cb` is the default gateway —
the first hop. The server is nine hops away and its MAC address was never
transmitted here and could not be. Every router along the path rewrites both
MAC addresses while leaving the IP addresses untouched; that difference is the
whole reason the two layers are separate.

The largest frames in the capture are 1,466 bytes, and they account for
themselves exactly:

```
  14  Ethernet II header
  20  IPv4 header
  20  TCP header
1412  TCP payload
────
1466  bytes on the wire
```

Comfortably inside the interface's 1,500-byte MTU, which is why nothing had to
be fragmented.

## Network layer — IPv4

| Field | Outbound | Inbound |
|---|---|---|
| Source | `192.168.0.29` | `34.143.73.2` |
| Destination | `34.143.73.2` | `192.168.0.29` |
| TTL | 128 | 119 |
| Don't Fragment | set | set |

The TTL hints at the path length. Our packets leave with 128, the Windows
default. The replies arrive with 119, and since only 128 of the usual initial
values (64, 128, 255) can decrement to 119, **the replies crossed nine
routers** — each one decrementing the field by one. This is an inference from a
conventional initial value, not something the packet states outright; the
capture proves the replies were forwarded nine times *if* they started at 128.

Nothing was fragmented: `DF` is set in both directions, and TCP avoids
fragmentation by segmenting to the negotiated MSS instead — which is what the
next layer shows.

The source address is a private one, `192.168.0.29`; the packets that reach
Google carry the router's public address instead. NAT rewrites it on the way
out, which is another thing the capture can only show from this side of it.

## Transport layer — TCP

**The three-way handshake**, frames 280–282:

```
280  192.168.0.29 → 34.143.73.2   [SYN]      seq=0  win=64240  MSS=1460
281  34.143.73.2 → 192.168.0.29   [SYN,ACK]  seq=0  ack=1  win=65535  MSS=1412
282  192.168.0.29 → 34.143.73.2   [ACK]      seq=1  ack=1  win=259
```

**Segmentation.** Each side advertises the largest segment it is willing to
receive. We offer 1,460 — the standard 1,500-byte Ethernet MTU minus 20 bytes
of IP header and 20 of TCP header. Google answers 1,412, which is smaller
because their network adds encapsulation of its own. The lower of the two wins,
so the largest segments in the capture carry exactly 1,412 bytes of payload.

That is why a 3,680-byte `tools/list` response cannot travel as one packet: at
1,412 bytes per segment it takes three, and TCP reassembles them before the
HTTP layer ever sees a message. JSON-RPC has no idea any of this happened.

**Connection reuse.** This session opened **11 separate TCP connections** for
19 messages, and every request carried `Connection: close`. That is a property
of the client, not of MCP: `tools/conformance_check.py` is built on `urllib`,
which does not pool connections. The project's own host uses `httpx`, which
keeps one connection alive across the whole session — so the same conversation
driven by `python -m host.main` costs one handshake instead of eleven. For a
protocol as chatty as MCP that is the difference between paying a round trip
per call and paying it once.

## Application layer — TLS, and what it hides

```
Frame 283  TLS handshake type 1 (Client Hello)
           server_name: netops-mcp-261683462697.us-central1.run.app
Frame 288  TLS handshake type 2 (Server Hello)
```

After the Server Hello, everything is `Application Data`. The SNI extension in
the Client Hello is sent before encryption begins, so an observer learns *which
host* is being talked to — but not one byte of what is said to it. No HTTP
method, no header, no JSON-RPC message.

This is exactly the boundary that makes the first capture necessary, and it is
worth stating as a result rather than as an inconvenience: **deploying the
server behind TLS made the protocol unobservable to everyone, including us.**

---

# How the layers nest

```
┌─────────────────────────────────────────────────────────┐
│ JSON-RPC 2.0   {"jsonrpc":"2.0","id":3,"method":...}    │  the message
├─────────────────────────────────────────────────────────┤
│ MCP            Mcp-Session-Id, Mcp-Protocol-Version     │  the semantics
├─────────────────────────────────────────────────────────┤
│ HTTP/1.1       POST /mcp, 200 / 202, Content-Type       │  the carrier
├─────────────────────────────────────────────────────────┤
│ TLS            (remote only) encrypts everything above  │
├─────────────────────────────────────────────────────────┤
│ TCP            ports, seq/ack, MSS 1412, segmentation   │
├─────────────────────────────────────────────────────────┤
│ IPv4           192.168.0.29 → 34.143.73.2, TTL, DF      │
├─────────────────────────────────────────────────────────┤
│ Ethernet II    host MAC → gateway MAC, MTU 1500         │
└─────────────────────────────────────────────────────────┘
```

**JSON-RPC does not define a transport.** The specification says what a
request, a response and a notification look like and nothing at all about how
they travel. MCP is the layer that chooses a carrier, and it offers two: stdio
with NDJSON framing, and HTTP. The messages are byte-for-byte the same either
way — the `tools/list` response in this capture is the same JSON the stdio
server writes to its pipe.

That is why the framing question belongs to the transport and not to the
message. Over stdio, where a pipe is just a byte stream, MCP needs a rule for
where one message ends, and it chooses one JSON object per line. Over HTTP the
question never arises: `Content-Length` already says how long the body is, so
there is nothing left for MCP to decide.

---

# Reproducing this

The server, in plain HTTP:

```bash
uvicorn servers.netops.http_server:app --host 0.0.0.0 --port 8080
```

The capture, and the session that fills it:

```bash
tshark -i 10 -a duration:22 -w mcp_local_plaintext.pcapng &   # 10 = loopback
sleep 6
python tools/conformance_check.py --http http://127.0.0.1:8080/mcp
```

Against the deployment instead:

```bash
tshark -i 6 -a duration:28 -w mcp_cloudrun_tls.pcapng &       # 6 = Wi-Fi
sleep 7
python tools/conformance_check.py \
    --http https://netops-mcp-261683462697.us-central1.run.app/mcp
```

`tshark -D` lists the interface numbers on any given machine. Useful display
filters once the file is open in Wireshark:

| Filter | Shows |
|---|---|
| `http` | every request and response |
| `http.response.code == 202` | the notification, answered without a body |
| `json` | the JSON-RPC messages, dissected |
| `tcp.flags.syn == 1` | every connection opened |
| `tls.handshake.type == 1` | the Client Hello, with its SNI |
