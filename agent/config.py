"""Ground relay configuration.

TOML, not environment variables, and deliberately so: this file is edited by a
pilot on a laptop, not by an operator with a deployment pipeline. A text file
they can open, read and comment is the right interface; `set COURIER_...` is
not. The rest of the system keeps the environment-based configuration in
`common.config` — this is the one component whose operator is not a developer.

The bearer token is never in this file. It lives in its own file, referenced by
path, so that a configuration can be shared, pasted into a support thread or
committed as an example without leaking a credential.
"""

from __future__ import annotations

import ipaddress
import tomllib
from pathlib import Path
from typing import Any, Self

from pydantic import (
    AnyWebsocketUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from agent.queue import DEFAULT_QUEUE_MAX_BYTES
from common.config import ConfigurationError

__all__ = ["RelayConfig", "load_config", "read_token"]

# The in-memory hand-off between the UDP thread and the writer thread. Sized so
# a slow disk write cannot stall intake: at the ~12 datagrams/s per aircraft
# measured in ADR-001, this is minutes of buffer, and it is bounded so a
# pathological stall drops datagrams rather than exhausting memory.
DEFAULT_INTAKE_QUEUE_SIZE = 10000

# P7-01 alerts the operator when a link has been lost for 30 s. The relay must
# have finished deciding its uplink is dead before then, or the operator is
# told about a link the relay still believes is healthy - a third state nobody
# has designed for. This bounds the sum of the keepalive settings below.
LINK_LOSS_ALERT_S = 30.0

# Fields holding a filesystem path. A relative one is resolved against the
# configuration file's own directory, not the process's working directory.
_PATH_FIELDS = ("token_path", "queue_path", "ca_path")

_BACKSLASH = chr(92)

# Shown when TOML parsing fails on what looks like a pasted Windows path. By
# far the most likely cause of a decode error on a ground station.
_WINDOWS_PATH_HINT = """
  A backslash in a double-quoted TOML string starts an escape sequence, so a
  Windows path written like this is not valid TOML:

      token_path = "C:\\Users\\pilot\\relay.token"

  Use forward slashes, which Windows accepts everywhere:

      token_path = "C:/Users/pilot/relay.token"

  or a single-quoted literal string, where backslashes are taken as written:

      token_path = 'C:\\Users\\pilot\\relay.token'
"""


class RelayConfig(BaseModel):
    """Validated relay configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    station_id: str = Field(min_length=1)
    gateway_url: AnyWebsocketUrl
    token_path: Path
    queue_path: Path

    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=14445, ge=1, le=65535)
    queue_max_bytes: int = Field(default=DEFAULT_QUEUE_MAX_BYTES, gt=0)
    intake_queue_size: int = Field(default=DEFAULT_INTAKE_QUEUE_SIZE, gt=0)
    # How long the disk writer may go without completing a loop before the
    # relay reports `storage_ok: false`. A write that hangs in fsync neither
    # fails nor finishes, so only its duration can reveal it.
    writer_stall_timeout_s: float = Field(default=5.0, gt=0.0)

    # A PEM bundle for a development CA, when the Gateway or the verification
    # sink serves a self-signed certificate. Absent means the system trust
    # store, which is what a production deployment uses.
    ca_path: Path | None = None

    # Keepalive on the uplink. A half-open connection - bytes stop, the socket
    # stays up, nothing is refused - is only detectable from missing pongs, and
    # how long that takes is the sum of these three:
    #
    #     detection = (time to the next ping) + ping_timeout + close_timeout
    #     worst case = ping_interval + ping_timeout + close_timeout
    #
    # close_timeout counts because the close handshake waits for a close frame
    # that a dead link can never deliver. Measured on 2026-09-22; see
    # docs/runbooks/p1-01-test-records.md.
    uplink_ping_interval_s: float = Field(default=10.0, gt=0.0)
    uplink_ping_timeout_s: float = Field(default=10.0, gt=0.0)
    uplink_close_timeout_s: float = Field(default=5.0, gt=0.0)

    @property
    def worst_case_detection_s(self) -> float:
        """Longest the relay can believe a dead uplink is alive."""
        return (
            self.uplink_ping_interval_s
            + self.uplink_ping_timeout_s
            + self.uplink_close_timeout_s
        )

    @property
    def uses_tls(self) -> bool:
        """relay-v1 §2 requires wss."""
        return self.gateway_url.scheme == "wss"

    @model_validator(mode="after")
    def _detection_completes_before_the_operator_is_alerted(self) -> Self:
        """Bound how long the relay can disagree with the Gateway.

        The Gateway declares a station unreachable after three missed status
        messages, about 3 s (relay-v1 §8). The relay takes longer, because a
        half-open socket looks alive until a ping goes unanswered. In between,
        the two components hold different beliefs about the same link.

        That disagreement is harmless to the data - no acknowledgement can
        arrive through a dead link, so nothing is deleted and the queue simply
        grows - but it must be bounded, and it must close before P7-01 alerts
        the operator. Otherwise the alert fires while the relay still thinks
        the uplink is healthy.
        """
        if self.worst_case_detection_s >= LINK_LOSS_ALERT_S:
            raise ValueError(
                f"uplink keepalive sums to {self.worst_case_detection_s:.0f}s "
                f"(ping_interval + ping_timeout + close_timeout), which is not "
                f"below the {LINK_LOSS_ALERT_S:.0f}s link-loss alert in P7-01. "
                f"The relay would still believe its uplink was healthy when the "
                f"operator is told it is down."
            )
        return self

    @model_validator(mode="after")
    def _plaintext_is_loopback_only(self) -> Self:
        """Refuse ws:// to anything but this machine.

        The bearer token is sent as a request header. On loopback that never
        reaches a wire; to any other host it crosses a network in the clear,
        and on the shared LAN of a flying site that is a credential anyone can
        read. Loopback stays permitted because the relay and a sink on the same
        laptop are a legitimate development setup — and because a test there is
        not testing the network anyway.
        """
        if self.uses_tls:
            return self
        host = self.gateway_url.host or ""
        if _is_loopback(host):
            return self
        raise ValueError(
            f"gateway_url uses ws:// to {host!r}, which would send the bearer "
            f"token across the network in plaintext. Use wss://, with ca_path "
            f"pointing at your development CA if the certificate is "
            f"self-signed. Plain ws:// is permitted only to localhost."
        )


def _is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _describe(error: ValidationError, path: Path) -> str:
    lines = [f"invalid relay configuration in {path}:"]
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or "(file)"
        lines.append(f"  {location}: {detail['msg']}")
    return "\n".join(lines)


def _resolve_paths(raw: dict[str, Any], base: Path) -> dict[str, Any]:
    """Resolve relative paths against the configuration file's own directory.

    A pilot writing `token_path = "relay.token"` means the file sitting next to
    relay.toml. Resolving against the working directory instead makes the relay
    start from one directory and fail from another, for a reason nothing in the
    error message explains.
    """
    resolved = dict(raw)
    for field in _PATH_FIELDS:
        value = resolved.get(field)
        if not isinstance(value, str):
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            resolved[field] = str((base / candidate).resolve())
    return resolved


def _toml_error_message(path: Path, text: str, error: Exception) -> str:
    """Explain a TOML parse failure, and guess at the usual cause."""
    message = f"{path} is not valid TOML: {error}"
    looks_like_a_windows_path = "escape" in str(error).lower() or _BACKSLASH in text
    if not looks_like_a_windows_path:
        return message
    return message + "\n" + _WINDOWS_PATH_HINT.rstrip()


def load_config(path: Path) -> RelayConfig:
    """Read and validate a relay TOML file.

    Raises ConfigurationError with a message naming the offending key. A pilot
    reading this on a laptop gets a sentence, not a stack trace.
    """
    try:
        # utf-8-sig, not utf-8: Notepad writes a byte-order mark, and a BOM
        # makes tomllib fail with "Invalid statement (at line 1, column 1)" -
        # unintelligible to the pilot who just saved the file. Identical to
        # utf-8 when no BOM is present.
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"no relay configuration at {path}. "
            f"Copy agent/relay.example.toml and edit it."
        ) from error
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"{path} is not valid UTF-8 text") from error

    try:
        raw: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(_toml_error_message(path, text, error)) from error

    # Relative paths belong to the file that names them, not to whichever
    # directory the relay happened to be started from.
    raw = _resolve_paths(raw, path.parent)

    try:
        return RelayConfig(**raw)
    except ValidationError as error:
        raise ConfigurationError(_describe(error, path)) from error


def read_token(path: Path) -> str:
    """Read the bearer token, or explain what is wrong with it."""
    try:
        # utf-8-sig for the same reason as the configuration: a BOM would
        # otherwise become an invisible prefix on the bearer token, and the
        # failure would surface as a 401 from the Gateway.
        token = path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"no token file at {path}. The Gateway operator issues this; it is "
            f"not stored in the relay configuration."
        ) from error
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"token file {path} is not valid UTF-8") from error

    if not token:
        raise ConfigurationError(f"token file {path} is empty")
    return token
