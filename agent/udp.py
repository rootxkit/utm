"""The UDP intake socket — receive only.

At Stage 0 the server cannot affect flight. `ARCHITECTURE.md` §4 rests on that,
and `relay-v1.md` §1 states it as an absolute rule. This module is where it is
enforced: the socket object is private, and this class exposes no method that
can transmit.

That is deliberately stronger than "we do not call send". A capability no code
can express cannot be reached by accident, by a refactor, or by someone who has
not read the architecture. No command path is planned, here or anywhere
else: the system never commands an aircraft.
"""

from __future__ import annotations

import socket

from agent.framing import UDP_RECEIVE_BUFFER_BYTES

__all__ = ["PortInUseError", "ReceiveOnlyUDPSocket"]


class PortInUseError(RuntimeError):
    """Another process already holds the intake port."""


class ReceiveOnlyUDPSocket:
    """A bound UDP socket that can receive and close, and nothing else."""

    def __init__(self, host: str, port: int, *, timeout_s: float = 0.5) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Deliberately NOT SO_REUSEADDR. On Windows that option lets a second
        # process bind the same UDP port, and the OS then splits the datagrams
        # arbitrarily between them - each relay silently receives part of the
        # stream and neither can tell. That happened during the 2026-09-22
        # Procedure B run, with two relays and two probes left running.
        #
        # SO_EXCLUSIVEADDRUSE is the Windows opt-out; on POSIX the absence of
        # SO_REUSEADDR is already enough for the second bind to fail.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)

        try:
            self._socket.bind((host, port))
        except OSError as error:
            self._socket.close()
            raise PortInUseError(
                f"cannot bind UDP {host}:{port} - another process already holds "
                f"it ({error}). Only one reader of the forwarded stream may run "
                f"at a time: stop any other relay, and stop "
                f"tools/mavlink_probe.py if it is listening."
            ) from error

        self._socket.settimeout(timeout_s)
        self._host = host
        self._port = port

    @property
    def endpoint(self) -> tuple[str, int]:  # pragma: no cover - trivial accessor
        """The configured endpoint, as asked for."""
        return self._host, self._port

    @property
    def bound_endpoint(self) -> tuple[str, int]:
        """The endpoint actually bound, which differs when port 0 was asked for."""
        host, port = self._socket.getsockname()
        return str(host), int(port)

    def receive(self) -> bytes | None:
        """Return the next datagram, or None if none arrived before the timeout.

        The sender's address is deliberately discarded. The relay does not
        reply, so it has no use for a return path.
        """
        try:
            datagram, _address = self._socket.recvfrom(UDP_RECEIVE_BUFFER_BYTES)
        except TimeoutError:
            return None
        except OSError:
            # The socket was closed underneath us during shutdown.
            return None
        return datagram

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> ReceiveOnlyUDPSocket:
        return self  # pragma: no cover - context manager sugar

    def __exit__(self, *exc_info: object) -> None:
        self.close()  # pragma: no cover - context manager sugar
