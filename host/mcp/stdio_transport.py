"""stdio transport: launch an MCP server as a subprocess and speak NDJSON to it.

Framing is one JSON message per line, terminated by \\n, encoded UTF-8, with no
embedded newlines. This is *not* the Content-Length framing that LSP uses.

stderr belongs to the server's own logging. It is drained on a separate thread
and surfaced with a prefix, but it is never parsed as protocol - a server that
writes a traceback must not be able to corrupt the message stream.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from host.mcp import jsonrpc
from host.mcp.transport import Transport, TransportError

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Pushed onto the inbox when the server's stdout reaches EOF.
_EOF = object()

DEFAULT_CLOSE_TIMEOUT = 5.0

# Node's launchers ship on Windows as batch scripts - npx is npx.cmd - and
# CreateProcess does not consult PATHEXT, so launching "npx" directly raises
# FileNotFoundError there. They have to go through the command interpreter.
#
# uvx is deliberately absent: it is a real .exe, and wrapping it in cmd only
# adds a layer that can mangle argument quoting. The rule keys on the command
# name rather than living in config/servers.json, so the same declaration file
# works unchanged on Windows, macOS and Linux.
WINDOWS_SHELL_COMMANDS = frozenset({"npx", "npm", "yarn", "pnpm", "bunx"})


def build_argv(command: str, args: list[str], is_windows: bool | None = None) -> list[str]:
    """Resolve a server declaration into the argv that actually launches it.

    Two rewrites happen here, both of them platform detail that has no place
    in a configuration file:

    "python" becomes the interpreter running the host. config/servers.json
    says "python" so it stays portable, but a bare "python" resolves against
    PATH and would miss the virtual environment, starting the server without
    the dependencies it needs.

    A Node launcher on Windows is prefixed with `cmd /c`, for the reason
    above.
    """
    if is_windows is None:
        is_windows = os.name == "nt"
    if command in ("python", "python3"):
        return [sys.executable, *args]
    if is_windows and command.lower() in WINDOWS_SHELL_COMMANDS:
        return ["cmd", "/c", command, *args]
    return [command, *args]


class StdioTransport(Transport):
    """Runs one MCP server as a child process and exchanges NDJSON with it."""

    kind = "stdio"

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        name: str = "server",
        on_stderr: Callable[[str, str], None] | None = None,
        close_timeout: float = DEFAULT_CLOSE_TIMEOUT,
    ) -> None:
        self.command = command
        self.args = list(args or [])
        self.extra_env = dict(env or {})
        self.cwd = cwd or PROJECT_ROOT
        self.name = name
        self.close_timeout = close_timeout
        self._on_stderr = on_stderr

        self._process: subprocess.Popen[str] | None = None
        self._inbox: queue.Queue[Any] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._closed = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.extra_env)
        # Windows defaults the child to cp1252, which mangles any JSON payload
        # containing accented characters. Force UTF-8 both ways.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def start(self) -> None:
        if self._process is not None:
            raise TransportError(f"{self.name}: transport already started")

        argv = build_argv(self.command, self.args)
        try:
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.cwd),
                env=self._build_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,  # line buffered, so a written line leaves immediately
            )
        except OSError as exc:
            raise TransportError(
                f"{self.name}: could not launch {self.command!r}: {exc}"
            ) from exc

        self._reader = threading.Thread(
            target=self._read_loop, name=f"{self.name}-stdout", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._stderr_loop, name=f"{self.name}-stderr", daemon=True
        )
        self._stderr_reader.start()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # -- reading -----------------------------------------------------------

    def _read_loop(self) -> None:
        """Decode NDJSON lines off stdout onto the inbox until EOF."""
        assert self._process is not None and self._process.stdout is not None
        try:
            for line in self._process.stdout:
                if not line.strip():
                    continue
                try:
                    self._inbox.put(jsonrpc.decode(line))
                except jsonrpc.ParseError as exc:
                    # An unreadable line is the server's bug, not a reason to
                    # tear down the connection. Report it and keep reading.
                    self._report_stderr(f"[unparseable line] {exc}: {line.strip()[:200]}")
        except (ValueError, OSError):
            # The pipe was closed underneath us during shutdown.
            pass
        finally:
            self._inbox.put(_EOF)

    def _stderr_loop(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            for line in self._process.stderr:
                text = line.rstrip("\r\n")
                if text:
                    self._report_stderr(text)
        except (ValueError, OSError):
            pass

    def _report_stderr(self, text: str) -> None:
        if self._on_stderr is not None:
            self._on_stderr(self.name, text)

    def receive(self) -> dict[str, Any] | None:
        item = self._inbox.get()
        if item is _EOF:
            # Put it back so every other waiter also sees the close.
            self._inbox.put(_EOF)
            return None
        return item

    # -- writing -----------------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise TransportError(f"{self.name}: transport is not started")
        if process.poll() is not None:
            raise TransportError(
                f"{self.name}: server exited with code {process.returncode} before the write"
            )

        line = jsonrpc.encode(message)
        with self._send_lock:
            try:
                process.stdin.write(line + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise TransportError(f"{self.name}: write failed, server is gone: {exc}") from exc

    # -- shutdown ----------------------------------------------------------

    def close(self) -> None:
        """Close stdin, then escalate: wait, terminate, kill."""
        if self._closed.is_set():
            return
        self._closed.set()

        process = self._process
        if process is None:
            return

        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass

        try:
            process.wait(timeout=self.close_timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=self.close_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread.is_alive():
                thread.join(timeout=self.close_timeout)

        # Unblock anyone still sitting in receive().
        self._inbox.put(_EOF)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "running" if self.is_running else "stopped"
        return f"<StdioTransport {self.name} {state}>"
