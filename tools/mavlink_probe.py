#!/usr/bin/env python3
"""Probe a QGroundControl MAVLink forwarding endpoint.

Answers the P1-00 question: what does QGC's forwarding actually give us, and is
the channel usable in both directions?

Listen mode needs nothing but the standard library, so it runs on a bare Windows
Python outside the venv. Round-trip mode needs pymavlink.

    python tools/mavlink_probe.py listen
    python tools/mavlink_probe.py listen --seconds 60 --json report.json
    python tools/mavlink_probe.py roundtrip --param SYSID_THISMAV

Configure QGC first:
    Application Settings -> General -> MAVLink -> Enable MAVLink forwarding
    Host: 127.0.0.1:14445
"""

from __future__ import annotations

import argparse
import functools
import json
import socket
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Literal

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 14445

MAVLINK_V1_MAGIC = 0xFE
MAVLINK_V2_MAGIC = 0xFD

# Fallback names, used only when pymavlink is not importable — listen mode has
# to run on a bare ground-station Python. When pymavlink IS available its
# dialect is authoritative and this table is not consulted, so an entry here
# going stale cannot mislead anyone on a development machine.
MESSAGE_NAMES = {
    0: "HEARTBEAT",
    1: "SYS_STATUS",
    2: "SYSTEM_TIME",
    22: "PARAM_VALUE",
    24: "GPS_RAW_INT",
    26: "SCALED_IMU",
    27: "RAW_IMU",
    29: "SCALED_PRESSURE",
    30: "ATTITUDE",
    32: "LOCAL_POSITION_NED",
    33: "GLOBAL_POSITION_INT",
    34: "RC_CHANNELS_SCALED",
    35: "RC_CHANNELS_RAW",
    36: "SERVO_OUTPUT_RAW",
    42: "MISSION_CURRENT",
    46: "MISSION_ITEM_REACHED",
    62: "NAV_CONTROLLER_OUTPUT",
    65: "RC_CHANNELS",
    74: "VFR_HUD",
    116: "SCALED_IMU2",
    125: "POWER_STATUS",
    136: "TERRAIN_REPORT",
    141: "ALTITUDE",
    147: "BATTERY_STATUS",
    165: "HIGH_LATENCY",
    193: "EKF_STATUS_REPORT",
    241: "VIBRATION",
    242: "HOME_POSITION",
    253: "STATUSTEXT",
}

# Messages the telemetry pipeline depends on that the autopilot streams at a
# configured rate. If one of these is absent, the relevant SR*_ parameter is
# too low and that is a finding.
REQUIRED_STREAMS = {0, 1, 24, 33, 74, 147}

# Messages the pipeline also depends on, but which the autopilot emits only
# when something happens: STATUSTEXT when the FC has something to say,
# MISSION_ITEM_REACHED when a waypoint is passed. A quiet, parked aircraft
# produces neither, so their absence says nothing about stream configuration.
# Reporting them as MISSING sends someone chasing SR*_ parameters that were
# never the problem.
EVENT_DRIVEN = {253, 46}

# A rate needs enough samples across a long enough window to mean anything.
# Below these thresholds the probe prints "-" rather than a number, because a
# number here would be read as a measurement.
MIN_SAMPLES_FOR_RATE = 3
MIN_SPAN_FOR_RATE_S = 0.5

PARAM_VALUE_MSGID = 22
HEARTBEAT_MSGID = 0

MAVLINK_V1_HEADER_LEN = 6
MAVLINK_V2_HEADER_LEN = 10

# HEARTBEAT payload layout, by the same descending-type-size rule as
# PARAM_VALUE. ordered_fieldnames is
# ['custom_mode', 'type', 'autopilot', 'base_mode', 'system_status',
#  'mavlink_version'], giving:
#
#     uint32  custom_mode      offset 0, 4 bytes
#     uint8   type             offset 4
#     uint8   autopilot        offset 5
#     uint8   base_mode        offset 6
#     uint8   system_status    offset 7
#     uint8   mavlink_version  offset 8
#
# Pinned against pymavlink in tools/tests/test_mavlink_probe.py.
HEARTBEAT_PAYLOAD_LEN = 9
_HEARTBEAT_TYPE_OFFSET = 4
_HEARTBEAT_AUTOPILOT_OFFSET = 5

# From pymavlink's enums; pinned by test rather than trusted here.
MAV_TYPE_GCS = 6
MAV_AUTOPILOT_INVALID = 8

# MAV_TYPE values that denote ground equipment or a peripheral rather than an
# aircraft. A gimbal, a companion computer or an antenna tracker emits its own
# HEARTBEAT, often under the vehicle's SYSID with a different component ID.
NON_VEHICLE_MAV_TYPES = frozenset(
    {
        5,  # ANTENNA_TRACKER
        6,  # GCS
        18,  # ONBOARD_CONTROLLER
        26,  # GIMBAL
        27,  # ADSB
        30,  # CAMERA
        31,  # CHARGING_STATION
        32,  # FLARM
        33,  # SERVO
        34,  # ODID
        36,  # BATTERY
        37,  # PARACHUTE
        38,  # LOG
        39,  # OSD
        40,  # IMU
        41,  # GPS
        42,  # WINCH
    }
)

SourceKind = Literal["vehicle", "gcs", "component", "unclassified"]

# Roundtrip timing. The injection interval is deliberately longer than the
# correlation window so the channel is quiet between attempts: if every moment
# fell inside some window, correlating a reply with an injection would prove
# nothing at all.
PEER_DISCOVERY_S = 15.0
BASELINE_S = 15.0
INJECT_INTERVAL_S = 5.0
CORRELATION_WINDOW_S = 2.0
REQUIRED_CORRELATIONS = 3

# PARAM_VALUE payload layout.
#
# MAVLink orders fields on the wire by descending type size, not by their order
# in the XML definition, so param_id does not start where the message
# documentation reads as though it should:
#
#     float    param_value   offset  0, 4 bytes
#     uint16   param_count   offset  4, 2 bytes
#     uint16   param_index   offset  6, 2 bytes
#     char     param_id[16]  offset  8, 16 bytes
#     uint8    param_type    offset 24, 1 byte
#
# These offsets are asserted against frames built by pymavlink in
# tools/tests/test_mavlink_probe.py rather than trusted from memory.
_PARAM_ID_START = 8
_PARAM_ID_END = 24


@functools.cache
def _dialect_message_names() -> dict[int, str]:
    """Message names straight from pymavlink, or {} if it is not installed.

    pymavlink's dialect is the reference for MAVLink names (CLAUDE.md rule 4),
    so resolving against it at runtime beats a hand-maintained table that
    drifts. The import is optional and local because listen mode must keep
    working on a machine that has nothing but the standard library.
    """
    try:
        from pymavlink.dialects.v20 import ardupilotmega as dialect
    except ImportError:
        return {}

    # msgname, not name: name is deprecated and warns on every access, which
    # under our warnings-as-errors test config would be a failure.
    names: dict[int, str] = {}
    for msgid, message_class in dialect.mavlink_map.items():
        names[int(msgid)] = str(message_class.msgname)
    return names


def resolve_message_name(msgid: int) -> str:
    """Name a message ID, falling back to '#id' when nothing knows it."""
    dynamic = _dialect_message_names().get(msgid)
    if dynamic is not None:
        return dynamic
    static = MESSAGE_NAMES.get(msgid)
    if static is not None:
        return static
    return f"#{msgid}"


def compute_rate_hz(count: int, span_s: float) -> float | None:
    """Return an observed message rate, or None when it cannot be supported.

    Two samples a millisecond apart are not a 54 kHz stream; they are one event
    observed twice. Dividing by a near-zero span turns a burst of event-driven
    messages into an absurd rate that looks like a measurement, so a rate is
    only reported when there are enough samples across a long enough window.
    """
    if count < MIN_SAMPLES_FOR_RATE or span_s < MIN_SPAN_FOR_RATE_S:
        return None
    return (count - 1) / span_s


def parse_heartbeat(frame: bytes) -> tuple[int, int] | None:
    """Return (type, autopilot) from a HEARTBEAT frame, or None."""
    payload = payload_of(frame)
    if payload is None:
        return None
    padded = payload.ljust(HEARTBEAT_PAYLOAD_LEN, b"\x00")
    return padded[_HEARTBEAT_TYPE_OFFSET], padded[_HEARTBEAT_AUTOPILOT_OFFSET]


def classify_source(mav_type: int, autopilot: int) -> SourceKind:
    """Decide what a heartbeating source is, from what it says it is.

    Not from its SYSID: 255 for a ground station is a convention and a user
    setting, not a guarantee. Not from how much it transmits either — a vehicle
    that has just booted sends a HEARTBEAT and nothing else yet, and that is
    exactly the moment it must not be mistaken for a peripheral.

    Ambiguity resolves to "vehicle". Registering a GCS as an aircraft is a
    nuisance; failing to register an aircraft is the dangerous direction.
    """
    if mav_type == MAV_TYPE_GCS:
        return "gcs"
    # A component that is not an autopilot declares MAV_AUTOPILOT_INVALID.
    if mav_type in NON_VEHICLE_MAV_TYPES or autopilot == MAV_AUTOPILOT_INVALID:
        return "component"
    return "vehicle"


def classify_absences(seen: set[int]) -> tuple[set[int], set[int]]:
    """Split what did not arrive into findings and non-findings.

    Returns (missing_streams, unobserved_events). The first is actionable: a
    stream the pipeline needs is not configured to arrive. The second is not,
    and must never be presented as though it were — an event that did not
    happen is not a misconfiguration.
    """
    return REQUIRED_STREAMS - seen, EVENT_DRIVEN - seen


def bind_exclusive(sock: socket.socket, host: str, port: int) -> None:
    """Bind so that a second process cannot take the same port.

    Deliberately not SO_REUSEADDR. On Windows that option lets two
    processes bind the same UDP port, after which the OS splits the
    datagrams between them arbitrarily - each reader silently sees part of
    the stream and reports it as if it were the whole. That is
    indistinguishable from packet loss, and it is exactly what this tool
    exists to measure.

    SO_EXCLUSIVEADDRUSE is the Windows opt-out. On POSIX, simply not asking
    for SO_REUSEADDR already makes the second bind fail.

    The relay applies the same rule in agent/udp.py. The two are kept
    separate because listen mode must run on a ground-station Python with
    nothing but the standard library.
    """
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.bind((host, port))


def payload_of(frame: bytes) -> bytes | None:
    """Return a frame's payload, or None if the frame is unusable.

    Slices forward by the declared payload length rather than backwards from
    the end of the frame. A signed MAVLink v2 frame carries a 13-byte signature
    after the checksum, so counting back from the end silently yields the wrong
    bytes for exactly the frames that are hardest to notice going wrong.
    """
    if len(frame) < 2:
        return None

    magic = frame[0]
    if magic == MAVLINK_V2_MAGIC:
        start = MAVLINK_V2_HEADER_LEN
    elif magic == MAVLINK_V1_MAGIC:
        start = MAVLINK_V1_HEADER_LEN
    else:
        return None

    payload_len = frame[1]
    payload = frame[start : start + payload_len]
    if len(payload) < payload_len:
        return None
    return payload


def extract_param_id(frame: bytes) -> str | None:
    """Return the param_id carried by a PARAM_VALUE frame, or None.

    None means "could not read a name", which the caller must treat as a
    failure. It must never be treated as a wildcard match: doing so is how a
    one-way link gets reported as bidirectional.
    """
    payload = payload_of(frame)
    if payload is None:
        return None

    # MAVLink v2 truncates trailing zero bytes from the payload. The bytes it
    # removed were zeros by definition, so restoring them is exact, not a guess.
    padded = payload.ljust(_PARAM_ID_END, b"\x00")
    try:
        name = padded[_PARAM_ID_START:_PARAM_ID_END].split(b"\x00")[0].decode("ascii")
    except (UnicodeDecodeError, IndexError):
        return None

    return name or None


def decode_header(pkt: bytes) -> tuple[int, int, int] | None:
    """Return (sysid, compid, msgid) or None if the frame is unparseable."""
    if len(pkt) < 8:
        return None
    magic = pkt[0]
    if magic == MAVLINK_V2_MAGIC:
        if len(pkt) < 10:
            return None
        sysid, compid = pkt[5], pkt[6]
        msgid = pkt[7] | (pkt[8] << 8) | (pkt[9] << 16)
        return sysid, compid, msgid
    if magic == MAVLINK_V1_MAGIC:
        return pkt[3], pkt[4], pkt[5]
    return None


def split_frames(buf: bytes) -> list[bytes]:
    """A UDP datagram may carry several MAVLink frames back to back."""
    frames: list[bytes] = []
    i = 0
    n = len(buf)
    while i < n:
        magic = buf[i]
        if magic == MAVLINK_V2_MAGIC:
            if i + 10 > n:
                break
            payload_len = buf[i + 1]
            incompat = buf[i + 2]
            total = 12 + payload_len + (13 if incompat & 0x01 else 0)
        elif magic == MAVLINK_V1_MAGIC:
            if i + 6 > n:
                break
            total = 8 + buf[i + 1]
        else:
            i += 1  # resynchronise
            continue
        if i + total > n:
            break
        frames.append(buf[i : i + total])
        i += total
    return frames


def cmd_listen(args: argparse.Namespace) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        bind_exclusive(sock, args.host, args.port)
    except OSError as exc:
        print(f"Cannot bind {args.host}:{args.port} -- {exc}", file=sys.stderr)
        print(
            "Another process already holds that port. Only one reader of the "
            "forwarded stream may run at a time: stop the ground relay "
            "(python -m agent) or any other probe first.",
            file=sys.stderr,
        )
        return 2
    sock.settimeout(1.0)

    print(f"Listening on {args.host}:{args.port} for {args.seconds}s.")
    print("Enable MAVLink forwarding in QGC if nothing arrives.\n")

    # Keyed by (sysid, compid), not sysid: an aircraft's gimbal or companion
    # computer heartbeats under the vehicle's SYSID with its own component ID,
    # and merging them would audit a peripheral against a vehicle's stream
    # requirements.
    counts: Counter[tuple[tuple[int, int], int]] = Counter()
    first_seen: dict[tuple[tuple[int, int], int], float] = {}
    last_seen: dict[tuple[tuple[int, int], int], float] = {}
    kinds: dict[tuple[int, int], SourceKind] = {}
    sources: set[str] = set()
    datagrams = 0
    total_bytes = 0
    unparseable = 0

    start = time.monotonic()
    deadline = start + args.seconds
    try:
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            now = time.monotonic()
            datagrams += 1
            total_bytes += len(data)
            sources.add(f"{addr[0]}:{addr[1]}")
            for frame in split_frames(data):
                header = decode_header(frame)
                if header is None:
                    unparseable += 1
                    continue
                sysid, compid, msgid = header
                source = (sysid, compid)
                key = (source, msgid)
                counts[key] += 1
                first_seen.setdefault(key, now)
                last_seen[key] = now

                if msgid == HEARTBEAT_MSGID:
                    identity = parse_heartbeat(frame)
                    if identity is not None:
                        kinds[source] = classify_source(*identity)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        sock.close()

    elapsed = time.monotonic() - start
    if not counts:
        print("NOTHING RECEIVED.")
        print("  - Is forwarding enabled in QGC and pointed at this host:port?")
        print("  - Is QGC actually connected to a vehicle?")
        print("  - On Windows, check that the firewall is not blocking loopback.")
        return 1

    per_source: dict[tuple[int, int], list[tuple[int, int, float | None]]] = (
        defaultdict(list)
    )
    for (source, msgid), count in counts.items():
        span_s = last_seen[(source, msgid)] - first_seen[(source, msgid)]
        per_source[source].append((msgid, count, compute_rate_hz(count, span_s)))

    def kind_of(source: tuple[int, int]) -> SourceKind:
        return kinds.get(source, "unclassified")

    vehicles = [s for s in sorted(per_source) if kind_of(s) == "vehicle"]

    print(f"Duration        {elapsed:.1f}s")
    print(f"Datagrams       {datagrams}  ({total_bytes / elapsed / 1024:.1f} KiB/s)")
    print(f"Source(s)       {', '.join(sorted(sources))}")
    print(f"Vehicles        {[sysid for sysid, _ in vehicles] or 'none'}")
    if unparseable:
        print(f"Unparseable     {unparseable} frames")
    print()

    for source in sorted(per_source):
        sysid, compid = source
        kind = kind_of(source)
        label = {
            "vehicle": "vehicle",
            "gcs": "ground station",
            "component": "component",
            "unclassified": "unclassified, no HEARTBEAT observed",
        }[kind]
        # ASCII only: this output is pasted into a decision record, and a
        # Windows console in cp1252 renders an em dash as a replacement char.
        print(f"SYSID {sysid} / COMP {compid} - {label}")

        rows = per_source[source]
        # Sized to the block's content: resolved MAVLink names run past 24
        # characters (GIMBAL_DEVICE_ATTITUDE_STATUS is 29), and a misaligned
        # column is harder to read than a wide one.
        width = max(24, *(len(resolve_message_name(m)) for m, _, _ in rows))
        print(f"  {'message':<{width}} {'count':>7} {'Hz':>7}")
        for msgid, count, rate in sorted(rows, key=lambda r: -r[1]):
            rate_text = "-" if rate is None else f"{rate:.2f}"
            name = resolve_message_name(msgid)
            print(f"  {name:<{width}} {count:>7} {rate_text:>7}")

        # Only aircraft are held to the pipeline's stream requirements. A
        # ground station or a gimbal was never going to send GLOBAL_POSITION_INT
        # and reporting it as missing is noise that trains people to ignore the
        # line. "unclassified" is checked too: if we could not confirm what it
        # is, the safe assumption is that it might be an aircraft.
        if kind in ("vehicle", "unclassified"):
            missing_streams, unobserved_events = classify_absences(
                {m for m, _, _ in rows}
            )
            if missing_streams:
                names = ", ".join(
                    sorted(resolve_message_name(m) for m in missing_streams)
                )
                print(f"  MISSING (required by the pipeline): {names}")
                print("  Raise the relevant SR*_ stream rate parameters.")
            if unobserved_events:
                names = ", ".join(
                    sorted(resolve_message_name(m) for m in unobserved_events)
                )
                print(
                    f"  not observed (event-driven, absence is not a finding): {names}"
                )
        print()

    if args.json:
        report = {
            "host": args.host,
            "port": args.port,
            "duration_s": round(elapsed, 2),
            "datagrams": datagrams,
            "bytes": total_bytes,
            "sources": sorted(sources),
            "unparseable_frames": unparseable,
            "endpoints": [
                {
                    "sysid": sysid,
                    "compid": compid,
                    "kind": kind_of((sysid, compid)),
                }
                for sysid, compid in sorted(per_source)
            ],
            "vehicles": [
                {"sysid": sysid, "compid": compid} for sysid, compid in vehicles
            ],
            "messages": [
                {
                    "sysid": sysid,
                    "compid": compid,
                    "kind": kind_of((sysid, compid)),
                    "msgid": msgid,
                    "name": resolve_message_name(msgid),
                    "count": count,
                    # null, not 0.0: too few samples to say, which is not the
                    # same claim as "it arrived at zero hertz".
                    "rate_hz": None if rate is None else round(rate, 3),
                }
                for sysid, compid in sorted(per_source)
                for msgid, count, rate in sorted(per_source[(sysid, compid)])
            ],
        }
        with Path(args.json).open("w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"Report written to {args.json}")

    return 0


def _discover_peer(
    sock: socket.socket, timeout_s: float
) -> tuple[tuple[str, int] | None, int | None]:
    """Wait for any frame, to learn who to answer and which vehicle it is."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
        except TimeoutError:
            continue
        for frame in split_frames(data):
            header = decode_header(frame)
            if header is not None:
                return addr, header[0]
    return None, None


def _count_unsolicited_param_values(
    sock: socket.socket, seconds: float, wanted: str
) -> tuple[int, int]:
    """Listen without injecting anything. Returns (any PARAM_VALUE, matching)."""
    total = 0
    matching = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            data, _ = sock.recvfrom(4096)
        except TimeoutError:
            continue
        for frame in split_frames(data):
            header = decode_header(frame)
            if header is None or header[2] != PARAM_VALUE_MSGID:
                continue
            total += 1
            name = extract_param_id(frame)
            if name is not None and name.upper() == wanted.upper():
                matching += 1
    return total, matching


def _inject_and_correlate(
    sock: socket.socket,
    peer: tuple[str, int],
    packed: bytes,
    wanted: str,
    timeout_s: float,
) -> tuple[int, int, int]:
    """Inject on a fixed interval, counting only responses that follow one.

    Returns (correlated, uncorrelated, attempts). The injection interval is
    deliberately longer than the correlation window, so the channel is quiet
    between attempts. Without that gap every arrival would fall inside some
    window and the correlation would prove nothing.
    """
    correlated = 0
    uncorrelated = 0
    attempts = 0
    last_injection: float | None = None

    next_send_at = time.monotonic()
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline and correlated < REQUIRED_CORRELATIONS:
        now = time.monotonic()
        if now >= next_send_at:
            sock.sendto(packed, peer)
            attempts += 1
            last_injection = now
            next_send_at = now + INJECT_INTERVAL_S

        try:
            data, _ = sock.recvfrom(4096)
        except TimeoutError:
            continue

        arrived = time.monotonic()
        for frame in split_frames(data):
            header = decode_header(frame)
            if header is None or header[2] != PARAM_VALUE_MSGID:
                continue
            name = extract_param_id(frame)
            # An unreadable name is a failure, never a match. Treating it as one
            # is how a one-way link gets reported as bidirectional.
            if name is None or name.upper() != wanted.upper():
                continue
            if (
                last_injection is not None
                and arrived - last_injection <= CORRELATION_WINDOW_S
            ):
                correlated += 1
            else:
                uncorrelated += 1

    return correlated, uncorrelated, attempts


def cmd_roundtrip(args: argparse.Namespace) -> int:
    """Test whether traffic injected on the forwarding socket reaches the vehicle.

    The naive version of this test — send a PARAM_REQUEST_READ, call any
    PARAM_VALUE a success — cannot distinguish a reply from ordinary traffic.
    QGC requests parameters on its own, so PARAM_VALUE is already flowing
    through the forwarded stream whether or not our injection goes anywhere.

    A false negative here costs nothing: it leaves us on Stage 0, which is the
    plan. A false positive is the expensive one, because it would have us
    believe the server can reach the aircraft when it cannot. So the test
    refuses to guess:

      1. Measure a quiet baseline with no injection at all.
      2. If PARAM_VALUE arrives unsolicited, this method cannot answer the
         question on this setup. Say so and stop.
      3. Otherwise inject on a fixed interval, and count only the requested
         param_id arriving inside the window after an injection, repeatedly.
    """
    try:
        from pymavlink.dialects.v20 import ardupilotmega as mav_dialect
    except ImportError:
        print("roundtrip mode needs pymavlink:", file=sys.stderr)
        print("  .\\.venv\\Scripts\\python -m pip install pymavlink", file=sys.stderr)
        return 2

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        bind_exclusive(sock, args.host, args.port)
    except OSError as exc:
        print(f"Cannot bind {args.host}:{args.port} -- {exc}", file=sys.stderr)
        print(
            "Another process already holds that port. Stop the ground relay "
            "(python -m agent) or any other probe first.",
            file=sys.stderr,
        )
        return 2
    sock.settimeout(1.0)

    try:
        print(f"Waiting for a frame on {args.host}:{args.port} to learn the peer...")
        peer, target_sys = _discover_peer(sock, PEER_DISCOVERY_S)
        if peer is None or target_sys is None:
            print(
                "No traffic arrived. Run 'listen' first and fix that.", file=sys.stderr
            )
            return 1

        print(f"Peer {peer[0]}:{peer[1]}, vehicle SYSID {target_sys}")

        # Step 1 — baseline. Nothing is injected during this window.
        print(f"\nBaseline: listening {BASELINE_S:.0f}s without injecting anything.")
        unsolicited, unsolicited_match = _count_unsolicited_param_values(
            sock, BASELINE_S, args.param
        )
        print(
            f"  unsolicited PARAM_VALUE: {unsolicited} "
            f"({unsolicited_match} matching {args.param})"
        )

        if unsolicited:
            print()
            print("RESULT: INCONCLUSIVE.")
            print()
            print(
                f"PARAM_VALUE arrived {unsolicited} time(s) in "
                f"{BASELINE_S:.0f}s without us asking for it."
            )
            print("A reply to an injected request cannot be told apart from")
            print("traffic that was going to arrive anyway, so this method")
            print("cannot answer the question on this setup.")
            print()
            print("Either quiet the ground station first - close other GCS")
            print("instances, let QGC finish its initial parameter download, then")
            print("re-run - or answer the question a different way.")
            print()
            print("Do NOT record this as bidirectional. Record it as untested.")
            return 3

        # Step 2 — inject, and require repeated correlation.
        mav = mav_dialect.MAVLink(None, srcSystem=255, srcComponent=190)
        request = mav_dialect.MAVLink_param_request_read_message(
            target_system=target_sys,
            target_component=1,
            param_id=args.param.encode("ascii"),
            param_index=-1,
        )
        packed = request.pack(mav)

        print(
            f"\nInjecting PARAM_REQUEST_READ for {args.param!r} "
            f"every {INJECT_INTERVAL_S:.0f}s for up to {args.timeout}s."
        )
        print(
            f"Counting a reply only within {CORRELATION_WINDOW_S:.0f}s of an "
            f"injection; {REQUIRED_CORRELATIONS} needed."
        )
        correlated, uncorrelated, attempts = _inject_and_correlate(
            sock, peer, packed, args.param, float(args.timeout)
        )
        print(
            f"  attempts {attempts}, correlated {correlated}, "
            f"uncorrelated {uncorrelated}"
        )
    finally:
        sock.close()

    print()
    if correlated >= REQUIRED_CORRELATIONS:
        print("RESULT: BIDIRECTIONAL on this QGC build.")
        print()
        print(f"{correlated} replies for {args.param} each followed an injection")
        print(f"within {CORRELATION_WINDOW_S:.0f}s, against a silent baseline.")
        print()
        print("Do not rely on it regardless. It is undocumented, it varies by")
        print("QGC build, and it would let the server affect flight through a")
        print("path nobody designed for that. Record the exact QGC version.")
        return 0

    print("RESULT: TELEMETRY-ONLY.")
    print()
    if uncorrelated:
        print(f"{uncorrelated} matching PARAM_VALUE arrived outside any injection")
        print("window, which is not evidence of a reply. Re-run if this is high.")
        print()
    print("This is the expected outcome and the one the plan assumes.")
    print("The system only observes; missions and commands stay in QGC.")
    return 0


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default=DEFAULT_HOST)
    common.add_argument("--port", type=int, default=DEFAULT_PORT)

    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0], parents=[common]
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p_listen = sub.add_parser(
        "listen", help="inventory message types and rates", parents=[common]
    )
    p_listen.add_argument("--seconds", type=int, default=30)
    p_listen.add_argument("--json", help="write a JSON report to this path")
    p_listen.set_defaults(func=cmd_listen)

    p_rt = sub.add_parser(
        "roundtrip",
        help="test whether the channel is bidirectional",
        parents=[common],
    )
    p_rt.add_argument("--param", default="SYSID_THISMAV")
    # Budget for the injection phase only; the baseline window runs first, so
    # the whole command takes roughly BASELINE_S longer than this. The default
    # allows well over the REQUIRED_CORRELATIONS injections needed at
    # INJECT_INTERVAL_S apart, so a slow radio link is not mistaken for silence.
    p_rt.add_argument("--timeout", type=int, default=45)
    p_rt.set_defaults(func=cmd_roundtrip)

    args = parser.parse_args()
    # argparse hands back Any; name the contract the subparsers were built to.
    handler: Callable[[argparse.Namespace], int] = args.func
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
