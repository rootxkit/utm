"""The console entry point, end to end through main().

`python -m agent` is the only thing a pilot ever types, and until now nothing
executed it. The refusal paths matter most: someone reading these messages is
on a laptop at a flying site, not at a debugger.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent.__main__ import main
from agent.queue import DurableQueue
from agent.udp import ReceiveOnlyUDPSocket
from tests.ports import free_tcp_port, free_udp_port

_VALID_TEMPLATE = """
station_id = "cli-test"
gateway_url = "wss://gateway.example.org/relay/v1"
token_path = "relay.token"
queue_path = "relay-queue.sqlite3"
bind_port = {port}
"""


def valid_config(port: int | None = None) -> str:
    """A config that binds a port nothing else is using.

    The port is reserved rather than left to default. The default is 14445,
    which is the port a *real* relay binds - exclusively - so these tests
    failed with `WinError 10048` whenever one was running during the local
    end-to-end check. A test that cannot run while the system runs is a test
    people learn to ignore.
    """
    return _VALID_TEMPLATE.format(port=port if port is not None else free_udp_port())


def capture(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    """Return the JSON log lines main() emitted."""
    out = capsys.readouterr()
    return [
        json.loads(line)
        for line in (out.out + out.err).splitlines()
        if line.startswith("{")
    ]


def test_missing_config_exits_two_with_a_readable_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--config", str(tmp_path / "absent.toml")])

    assert code == 2
    records = capture(capsys)
    assert records, "nothing was logged"
    assert records[-1]["message"] == "cannot start"
    assert "relay.example.toml" in str(records[-1]["reason"])


def test_malformed_config_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "relay.toml"
    config.write_text("station_id = = =", encoding="utf-8")

    assert main(["--config", str(config)]) == 2
    assert "not valid TOML" in str(capture(capsys)[-1]["reason"])


def test_missing_token_file_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Configuration is fine; the credential it points at is not."""
    config = tmp_path / "relay.toml"
    config.write_text(valid_config(), encoding="utf-8")

    assert main(["--config", str(config)]) == 2
    assert "Gateway operator" in str(capture(capsys)[-1]["reason"])


def test_plaintext_to_a_remote_host_is_refused_at_startup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The relay will not send a bearer token across a LAN in the clear.

    This is the rule the sink mirrors. Checked here through main() rather than
    against the validator alone, because a rule that never reaches the entry
    point protects nothing.
    """
    config = tmp_path / "relay.toml"
    config.write_text(
        valid_config().replace("wss://gateway.example.org", "ws://192.168.1.50:8443"),
        encoding="utf-8",
    )

    assert main(["--config", str(config)]) == 2

    reason = str(capture(capsys)[-1]["reason"])
    assert "plaintext" in reason
    assert "wss://" in reason


def test_log_level_is_honoured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--log-level ERROR must still show the reason the relay would not start."""
    code = main(["--config", str(tmp_path / "absent.toml"), "--log-level", "ERROR"])

    assert code == 2
    assert capture(capsys)[-1]["level"] == "ERROR"


def test_the_default_config_path_is_relay_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`python -m agent` with no arguments looks for relay.toml here."""
    monkeypatch.chdir(tmp_path)

    assert main([]) == 2
    assert "relay.toml" in str(capture(capsys)[-1]["reason"])


def test_a_config_elsewhere_finds_its_token_beside_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure from the first local run, at the level it was hit.

    token_path = "relay.token" used to resolve against the working directory,
    so running the relay from anywhere but the config's own folder failed with
    a message that pointed at the wrong place entirely.
    """
    station = tmp_path / "station"
    station.mkdir()
    (station / "relay.toml").write_text(valid_config(), encoding="utf-8")
    (station / "relay.token").write_text("a-real-token", encoding="utf-8")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    # Stop before the uplink: this test is about configuration, not networking.
    started: dict[str, object] = {}

    def fake_run(coro: object) -> None:
        started["ran"] = True
        getattr(coro, "close", lambda: None)()

    monkeypatch.setattr("agent.__main__.asyncio.run", fake_run)

    code = main(["--config", str(station / "relay.toml")])

    assert code == 0, f"startup failed: {capture(capsys)}"
    assert started.get("ran") is True
    # The queue belongs beside the config, not in the shell's directory.
    assert (station / "relay-queue.sqlite3").exists()
    assert not (elsewhere / "relay-queue.sqlite3").exists()


def test_a_second_relay_refuses_to_start_on_a_held_port(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Starting a second relay must fail, not silently split the stream.

    Two relays were found sharing UDP 14445 during the 2026-09-22 Procedure B
    run, each receiving an arbitrary share of the forwarded datagrams. Neither
    could tell, and the symptom is indistinguishable from packet loss.
    """
    holder = ReceiveOnlyUDPSocket("127.0.0.1", 0, timeout_s=0.01)
    try:
        port = holder.bound_endpoint[1]
        station = tmp_path / "station"
        station.mkdir()
        (station / "relay.toml").write_text(valid_config(port), encoding="utf-8")
        (station / "relay.token").write_text("a-real-token", encoding="utf-8")

        code = main(["--config", str(station / "relay.toml")])
    finally:
        holder.close()

    assert code == 2
    reason = str(capture(capsys)[-1]["reason"])
    assert str(port) in reason
    assert "another process" in reason.lower()


def test_a_dead_writer_thread_ends_the_process_with_a_failure_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """S-02. Without a writer nothing reaches disk; the relay must not run on.

    Driven through the real asyncio.run and the real uplink, pointed at a
    loopback port nothing listens on, so the only thing that can end the run
    is the relay noticing its writer has gone.
    """

    def broken_writer(self: object) -> None:
        raise RuntimeError("a bug outside the storage error handling")

    monkeypatch.setattr("agent.relay.Relay._writer_loop", broken_writer)

    station = tmp_path / "station"
    station.mkdir()
    config = valid_config().replace(
        "wss://gateway.example.org/relay/v1",
        f"ws://127.0.0.1:{free_tcp_port()}/relay/v1",
    )
    (station / "relay.toml").write_text(config, encoding="utf-8")
    (station / "relay.token").write_text("a-real-token", encoding="utf-8")

    code = main(["--config", str(station / "relay.toml")])

    logs = capture(capsys)
    assert code == 1
    assert any(line.get("message") == "relay cannot continue" for line in logs), logs
    assert any(line.get("message") == "durable queue writer crashed" for line in logs)


class _PoisoningConnection:
    """Commits and rollbacks both fail, so the queue's first write poisons it."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def commit(self) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    def rollback(self) -> None:
        raise sqlite3.OperationalError("disk I/O error during rollback")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def test_a_poisoned_queue_ends_the_process_with_a_failure_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only a restart cures a poisoned queue, so the relay must not stay up.

    Driven through the real main(): real intake, a real writer and a real
    uplink pointed at a loopback port nothing listens on.
    """

    def poisoning_queue(path: Path, **kwargs: Any) -> DurableQueue:
        queue = DurableQueue(path, **kwargs)
        queue._connection = _PoisoningConnection(queue._connection)  # type: ignore[assignment]
        return queue

    monkeypatch.setattr("agent.__main__.DurableQueue", poisoning_queue)

    udp_port = free_udp_port()
    station = tmp_path / "station"
    station.mkdir()
    config = valid_config(udp_port).replace(
        "wss://gateway.example.org/relay/v1",
        f"ws://127.0.0.1:{free_tcp_port()}/relay/v1",
    )
    (station / "relay.toml").write_text(config, encoding="utf-8")
    (station / "relay.token").write_text("a-real-token", encoding="utf-8")

    # Traffic, so the writer has something to write and poisons the queue.
    sending = threading.Event()

    def send() -> None:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            while not sending.is_set():
                sender.sendto(b"x" * 32, ("127.0.0.1", udp_port))
                time.sleep(0.05)
        finally:
            sender.close()

    sender_thread = threading.Thread(target=send, daemon=True)
    sender_thread.start()
    try:
        code = main(["--config", str(station / "relay.toml")])
    finally:
        sending.set()
        sender_thread.join()

    logs = capture(capsys)
    assert code == 1, logs
    assert any(line.get("message") == "relay cannot continue" for line in logs)
    assert any(
        line.get("message") == "durable queue is poisoned; the relay must restart"
        for line in logs
    )
