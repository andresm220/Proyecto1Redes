"""The transport interface an MCP connection is built on.

A transport moves whole JSON-RPC messages in both directions and knows nothing
about MCP semantics: no handshake, no tools, no request/response correlation.
That split is what lets the remote phase add an HTTP transport without touching
MCPClient - it only has to implement the four methods below.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class TransportError(Exception):
    """The channel itself failed: the peer died, the pipe broke, no connection."""


class Transport(ABC):
    """A bidirectional channel carrying one JSON-RPC message at a time."""

    #: How this channel moves bytes, as the structured log names it. Every
    #: logged message is tagged with it, so one session log stays readable
    #: when it mixes local subprocesses with a remote server over HTTP.
    kind: str = "unknown"

    @abstractmethod
    def start(self) -> None:
        """Open the channel. Must be called before send() or receive()."""

    @abstractmethod
    def send(self, message: dict[str, Any]) -> None:
        """Write one message to the peer.

        Raises TransportError if the channel is not open or the write fails.
        """

    @abstractmethod
    def receive(self) -> dict[str, Any] | None:
        """Block until the next message arrives.

        Returns None once the peer has closed the channel and no messages
        remain, which is the signal for a reader loop to stop.
        """

    @abstractmethod
    def close(self) -> None:
        """Shut the channel down. Must be safe to call more than once."""

    def __enter__(self) -> "Transport":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
