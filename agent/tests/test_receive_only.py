"""The relay can never transmit on its UDP socket.

This is the Stage 0 safety guarantee at its first link: the server cannot
affect flight because the component nearest the aircraft is physically unable
to speak to it (`relay-v1.md` §1, `ARCHITECTURE.md` §4).

Two tests, deliberately. One checks the object, one checks the source. The
object test would pass if someone reached into the private socket; the source
test would pass if someone added a `send` method nobody calls. Together they
are hard to defeat by accident, which is the only way this would ever be
defeated.
"""

from __future__ import annotations

import ast
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent import udp
from agent.udp import PortInUseError, ReceiveOnlyUDPSocket

AGENT_ROOT = Path(udp.__file__).resolve().parent

# Every socket method that can put bytes on the wire.
TRANSMIT_METHODS = frozenset(
    {"send", "sendall", "sendto", "sendmsg", "sendfile", "sendmsg_afalg"}
)


@pytest.fixture
def bound_socket() -> Iterator[ReceiveOnlyUDPSocket]:
    # Port 0: let the OS choose, so the test never collides with a real relay
    # or with QGC forwarding on this machine.
    sock = ReceiveOnlyUDPSocket("127.0.0.1", 0, timeout_s=0.01)
    yield sock
    sock.close()


def test_the_socket_wrapper_exposes_no_transmit_method(
    bound_socket: ReceiveOnlyUDPSocket,
) -> None:
    exposed = {name for name in dir(bound_socket) if not name.startswith("_")}

    assert not exposed & TRANSMIT_METHODS, (
        f"ReceiveOnlyUDPSocket exposes {sorted(exposed & TRANSMIT_METHODS)}"
    )


def test_the_wrapper_is_not_a_socket_subclass(
    bound_socket: ReceiveOnlyUDPSocket,
) -> None:
    """Subclassing would inherit sendto and defeat the whole point."""
    assert not isinstance(bound_socket, socket.socket)


@pytest.mark.parametrize("method", sorted(TRANSMIT_METHODS))
def test_transmit_methods_raise_attribute_error(
    bound_socket: ReceiveOnlyUDPSocket, method: str
) -> None:
    with pytest.raises(AttributeError):
        getattr(bound_socket, method)


def _source_files() -> list[Path]:
    return [path for path in AGENT_ROOT.rglob("*.py") if "tests" not in path.parts]


def test_there_are_source_files_to_check() -> None:
    """Guard against this suite passing because it scanned nothing."""
    assert _source_files()


@pytest.mark.parametrize("module", _source_files(), ids=lambda path: str(path.name))
def test_no_module_calls_a_transmit_method(module: Path) -> None:
    """No `.send*(` call anywhere in the relay, on any object.

    Deliberately broader than "not on the UDP socket": a call this test cannot
    prove is safe is a call that should not be in this package. The WebSocket
    uplink sends through the `websockets` library, whose connection objects use
    `.send(...)` — so those are the calls this test would flag, and they live
    in relay.py, which is why the exemption is named there explicitly rather
    than left as a blanket allowance.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in TRANSMIT_METHODS:
            continue
        # The uplink's own sends are on a websockets connection, never a socket.
        if _is_websocket_send(func):
            continue
        offenders.append(f"{func.attr} at line {node.lineno}")

    assert not offenders, (
        f"{module.name} transmits: {', '.join(offenders)}. The relay is "
        f"receive-only toward the aircraft (relay-v1 §1)."
    )


def _is_websocket_send(func: ast.Attribute) -> bool:
    """True for `connection.send(...)` on the uplink WebSocket."""
    return (
        func.attr == "send"
        and isinstance(func.value, ast.Name)
        and (func.value.id == "connection")
    )


def test_the_detector_detects() -> None:
    """A broken invariant check is worse than none."""
    tree = ast.parse("sock.sendto(b'x', ('127.0.0.1', 14445))\n")
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in TRANSMIT_METHODS
    ]

    assert calls, "the AST scan would not notice a sendto call"


def test_receive_returns_none_when_nothing_arrives(
    bound_socket: ReceiveOnlyUDPSocket,
) -> None:
    assert bound_socket.receive() is None


def test_receive_returns_the_datagram_verbatim() -> None:
    """Checked end to end through a real socket, with an external sender."""
    receiver = ReceiveOnlyUDPSocket("127.0.0.1", 0, timeout_s=2.0)
    try:
        port = receiver.bound_endpoint[1]
        payload = bytes(range(256))

        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(payload, ("127.0.0.1", port))
        finally:
            sender.close()

        assert receiver.receive() == payload
    finally:
        receiver.close()


# --- exclusive bind ---------------------------------------------------------
#
# Two relays were found splitting the forwarded stream during the 2026-09-22
# Procedure B run: the socket set SO_REUSEADDR, which on Windows lets a second
# process bind the same UDP port. The OS then hands each an arbitrary share of
# the datagrams and neither can tell it is only seeing part of the stream -
# indistinguishable from packet loss.
#
# These run on Linux in CI, where the absence of SO_REUSEADDR is already enough
# for the second bind to fail, and on Windows, where SO_EXCLUSIVEADDRUSE is.


def test_a_second_bind_on_the_same_port_is_refused() -> None:
    """The runbook's warning, made a guarantee."""
    first = ReceiveOnlyUDPSocket("127.0.0.1", 0, timeout_s=0.01)
    try:
        port = first.bound_endpoint[1]

        with pytest.raises(PortInUseError) as raised:
            ReceiveOnlyUDPSocket("127.0.0.1", port, timeout_s=0.01)
    finally:
        first.close()

    message = str(raised.value)
    assert str(port) in message, "the error must name the port"
    assert "another process" in message.lower()
    assert "mavlink_probe" in message, "it must name the other likely holder"


def test_a_raw_socket_cannot_steal_the_port_either() -> None:
    """Not just two relays: the probe, or anything else, is refused too."""
    relay_socket = ReceiveOnlyUDPSocket("127.0.0.1", 0, timeout_s=0.01)
    try:
        port = relay_socket.bound_endpoint[1]
        intruder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                intruder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            with pytest.raises(OSError):
                intruder.bind(("127.0.0.1", port))
        finally:
            intruder.close()
    finally:
        relay_socket.close()


def test_the_port_is_released_on_close() -> None:
    """The presence half: refusing forever would be its own bug."""
    first = ReceiveOnlyUDPSocket("127.0.0.1", 0, timeout_s=0.01)
    port = first.bound_endpoint[1]
    first.close()

    second = ReceiveOnlyUDPSocket("127.0.0.1", port, timeout_s=0.01)
    try:
        assert second.bound_endpoint[1] == port
    finally:
        second.close()


def test_a_failed_bind_does_not_leak_the_socket() -> None:
    """The half-built socket is closed before the error is raised."""
    first = ReceiveOnlyUDPSocket("127.0.0.1", 0, timeout_s=0.01)
    try:
        port = first.bound_endpoint[1]
        for _ in range(20):
            with pytest.raises(PortInUseError):
                ReceiveOnlyUDPSocket("127.0.0.1", port, timeout_s=0.01)
    finally:
        first.close()


def test_the_relay_does_not_ask_for_reuseaddr() -> None:
    """Pin the absence: re-adding it would silently restore the split-stream bug.

    The constant may still be named in a comment explaining why it is absent;
    what must not come back is the setsockopt call.
    """
    source = Path(udp.__file__).read_text(encoding="utf-8")

    assert "SO_REUSEADDR, 1)" not in source
    assert "SO_EXCLUSIVEADDRUSE" in source
