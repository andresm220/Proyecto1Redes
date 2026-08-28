"""stdio transport adapter for the netops server.

Run with:  python -m servers.netops.stdio_server

Reads one JSON-RPC message per line from stdin and writes one per line to
stdout. stdout carries protocol and nothing else; every log line goes to
stderr, so a stray print can never corrupt the message stream.

This file is only the wire. The handshake, the method table and the error
mapping live in protocol.py, and the business logic in core.py - which is what
lets http_server.py sit beside this file without either one duplicating the
other.
"""

from __future__ import annotations

import sys

from host.mcp import jsonrpc
from servers.netops import core
from servers.netops.protocol import NetopsSession
from servers.netops.store import NetopsStore


def log(text: str) -> None:
    print(f"[netops] {text}", file=sys.stderr, flush=True)


class NetopsStdioServer:
    """Pumps NDJSON between stdin/stdout and one NetopsSession."""

    def __init__(self, store: NetopsStore | None = None) -> None:
        self.session = NetopsSession(store=store, log=log)

    @property
    def store(self) -> NetopsStore:
        return self.session.store

    def handle_line(self, line: str) -> dict | None:
        """Turn one input line into one response, or None if none is owed."""
        return self.session.handle_line(line)

    def serve(self, stdin=None, stdout=None) -> int:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        log(f"listening on stdio, protocol {core.PROTOCOL_VERSION}")

        for line in stdin:
            if not line.strip():
                continue
            response = self.handle_line(line)
            if response is not None:
                stdout.write(jsonrpc.encode(response) + "\n")
                stdout.flush()

        log("stdin closed, shutting down")
        return 0


def main() -> int:
    # Windows defaults these streams to cp1252, which would corrupt any accented
    # payload. The host also sets PYTHONIOENCODING, but a server launched by
    # another client (Claude Desktop, say) may not get that, so force it here.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    return NetopsStdioServer().serve()


if __name__ == "__main__":
    sys.exit(main())
