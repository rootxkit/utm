"""A fake ASTM F3411 Service Provider, for tests and local SITL. U-02.

    python -m tools.fake_rid_sp --port 8090 --client-id utm-local \\
        --client-secret-file local/u02/fake-sp.secret \\
        --sysid 2 --serial 1581F5FKD2290002 --operator-id GEOOPERATOR0001 \\
        --mavlink udpin:127.0.0.1:14561 --geoid local/geoid/egm2008-2_5.pgm

Georgia has no USSP yet, so network Remote ID (`gateway/network_rid.py`)
needs something to poll on a laptop. This plays a USSP's Service Provider
with the endpoints a Display Provider uses, in the F3411-22a shapes InterUSS
publishes (the module docstring of `gateway/network_rid.py` lists them):

- `POST /token`: OAuth 2 client credentials, HTTP Basic; one client.
- `GET /uss/flights?view=lat1,lng1,lat2,lng2`: the flights in the view,
  413 when its diagonal is over `--max-diagonal-km`, 401 without a token
  it issued.
- `GET /uss/flights/{id}/details`: serial and operator.

Flights come from SITL vehicles, as the U-16 bridge's do: it reads each
vehicle's MAVLink (receive only, never writing to the connection) with the
bridge's own `VehicleState`, and serves the latest position as the flight's
current state. Its altitude is HAE, the vehicle's AMSL plus the `--geoid`
undulation, which the ingest subtracts again. Without `--mavlink` (tests)
flights are put in the store directly.

Nothing here is a flight instruction: it only listens to SITL.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import math
import secrets
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common.geoid import GeoidGrid
from tools.sitl_remote_id import MavlinkSource, VehicleState

DEFAULT_MAX_DIAGONAL_KM = 7.0
TOKEN_LIFETIME_S = 3600
# A flight not updated for this long is no longer served (F3411's
# NetMaxNearRealTimeDataPeriod).
STALE_AFTER_S = 60.0
_KM_PER_DEG = 111.32


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def rid_time(moment: datetime) -> dict[str, str]:
    return {"value": iso(moment), "format": "RFC3339"}


@dataclass
class Flight:
    flight_id: str
    serial: str | None
    operator_id: str | None
    lat_deg: float
    lon_deg: float
    alt_hae_m: float | None
    timestamp: datetime
    airborne: bool = True
    track_deg: float | None = None
    speed_ms: float | None = None
    vertical_speed_ms: float | None = None
    height_over_takeoff_m: float | None = None
    operator_lat_deg: float | None = None
    operator_lon_deg: float | None = None
    updated_s: float = 0.0

    def state(self) -> dict[str, Any]:
        position: dict[str, Any] = {
            "lat": self.lat_deg,
            "lng": self.lon_deg,
            "alt": -1000 if self.alt_hae_m is None else round(self.alt_hae_m, 2),
            "accuracy_h": "HA3m",
            "accuracy_v": "VA10m",
            "extrapolated": False,
            "pressure_altitude": -1000,
        }
        if self.height_over_takeoff_m is not None:
            position["height"] = {
                "distance": round(self.height_over_takeoff_m, 2),
                "reference": "TakeoffLocation",
            }
        return {
            "timestamp": rid_time(self.timestamp),
            "timestamp_accuracy": 0.1,
            "operational_status": "Airborne" if self.airborne else "Ground",
            "position": position,
            "track": 361 if self.track_deg is None else round(self.track_deg, 2),
            "speed": 255 if self.speed_ms is None else round(self.speed_ms, 2),
            "speed_accuracy": "SA1mps",
            "vertical_speed": (
                63
                if self.vertical_speed_ms is None
                else round(self.vertical_speed_ms, 2)
            ),
        }

    def details(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "id": self.flight_id,
            "operator_id": self.operator_id or "",
            "operation_description": "SITL flight through the fake SP",
            "uas_id": {"serial_number": self.serial or ""},
            "eu_classification": {"category": "Open", "class": "EUClass1"},
        }
        if self.operator_lat_deg is not None and self.operator_lon_deg is not None:
            details["operator_location"] = {
                "position": {"lat": self.operator_lat_deg, "lng": self.operator_lon_deg}
            }
        return details


@dataclass
class FlightStore:
    clock_s: Callable[[], float] = time.monotonic
    flights: dict[str, Flight] = field(default_factory=dict)

    def put(self, flight: Flight) -> None:
        flight.updated_s = self.clock_s()
        self.flights[flight.flight_id] = flight

    def current(self) -> list[Flight]:
        now_s = self.clock_s()
        return [
            f for f in self.flights.values() if now_s - f.updated_s <= STALE_AFTER_S
        ]


def parse_view(view: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in view.split(",")]
    if len(parts) != 4 or not all(math.isfinite(p) for p in parts):
        raise ValueError("view is lat1,lng1,lat2,lng2")
    lat1, lng1, lat2, lng2 = parts
    return min(lat1, lat2), min(lng1, lng2), max(lat1, lat2), max(lng1, lng2)


def diagonal_km(box: tuple[float, float, float, float]) -> float:
    lat_min, lon_min, lat_max, lon_max = box
    dy = (lat_max - lat_min) * _KM_PER_DEG
    dx = (
        (lon_max - lon_min)
        * _KM_PER_DEG
        * math.cos(math.radians((lat_min + lat_max) / 2))
    )
    return math.hypot(dx, dy)


@dataclass
class Counters:
    tokens_issued: int = 0
    tokens_refused: int = 0
    flights_requests: int = 0
    too_large: int = 0
    unauthorised: int = 0
    details_requests: int = 0


def create_app(
    store: FlightStore,
    *,
    client_id: str,
    client_secret: str,
    max_diagonal_km: float = DEFAULT_MAX_DIAGONAL_KM,
    wall: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    lifespan: Callable[[FastAPI], Any] | None = None,
) -> FastAPI:
    app = FastAPI(title="fake F3411 Service Provider", lifespan=lifespan)
    tokens: set[str] = set()
    counters = Counters()
    app.state.counters = counters
    app.state.store = store

    def authorised(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        ok = header.startswith("Bearer ") and header[len("Bearer ") :] in tokens
        if not ok:
            counters.unauthorised += 1
        return ok

    @app.post("/token")
    async def token(request: Request) -> JSONResponse:
        # Parsed by hand: Starlette's form parser needs python-multipart,
        # a dependency for one urlencoded body in a test double.
        form = {
            key: values[0]
            for key, values in parse_qs((await request.body()).decode("utf-8")).items()
        }
        header = request.headers.get("authorization", "")
        given = ("", "")
        if header.startswith("Basic "):
            try:
                pair = base64.b64decode(header[6:]).decode("utf-8")
                user, _, password = pair.partition(":")
                given = (user, password)
            except ValueError:
                pass
        if form.get("grant_type") != "client_credentials" or not (
            secrets.compare_digest(given[0], client_id)
            and secrets.compare_digest(given[1], client_secret)
        ):
            counters.tokens_refused += 1
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        issued = secrets.token_urlsafe(24)
        tokens.add(issued)
        counters.tokens_issued += 1
        return JSONResponse(
            {
                "access_token": issued,
                "token_type": "Bearer",
                "expires_in": TOKEN_LIFETIME_S,
                "scope": str(form.get("scope", "")),
            }
        )

    @app.get("/uss/flights")
    async def flights(request: Request, view: str) -> JSONResponse:
        if not authorised(request):
            return JSONResponse({"message": "unauthorised"}, status_code=401)
        counters.flights_requests += 1
        try:
            box = parse_view(view)
        except ValueError as error:
            return JSONResponse({"message": str(error)}, status_code=400)
        if diagonal_km(box) > max_diagonal_km:
            counters.too_large += 1
            return JSONResponse({"message": "area too large"}, status_code=413)
        lat_min, lon_min, lat_max, lon_max = box
        inside = [
            {
                "id": f.flight_id,
                "aircraft_type": "Helicopter",
                "current_state": f.state(),
                "simulated": True,
                "recent_positions": [],
            }
            for f in store.current()
            if lat_min <= f.lat_deg <= lat_max and lon_min <= f.lon_deg <= lon_max
        ]
        return JSONResponse({"timestamp": rid_time(wall()), "flights": inside})

    @app.get("/uss/flights/{flight_id}/details")
    async def details(request: Request, flight_id: str) -> JSONResponse:
        if not authorised(request):
            return JSONResponse({"message": "unauthorised"}, status_code=401)
        counters.details_requests += 1
        flight = store.flights.get(flight_id)
        if flight is None:
            return JSONResponse({"message": "no such flight"}, status_code=404)
        return JSONResponse({"details": flight.details()})

    return app


# --- SITL -------------------------------------------------------------------


@dataclass
class SitlFlight:
    """One SITL vehicle served as one network Remote ID flight."""

    flight_id: str
    serial: str | None
    operator_id: str | None
    state: VehicleState
    source: MavlinkSource
    geoid: GeoidGrid | None

    def flight(self) -> Flight | None:
        position = self.state.position
        if position is None:
            return None
        unix_s = self.state.unix_s(position.time_boot_ms)
        undulation = (
            None
            if self.geoid is None
            else self.geoid.undulation_m(position.lat_deg, position.lon_deg)
        )
        speed = math.hypot(position.vn_ms, position.ve_ms)
        track = math.degrees(math.atan2(position.ve_ms, position.vn_ms)) % 360
        takeoff = self.state.takeoff
        return Flight(
            flight_id=self.flight_id,
            serial=self.serial,
            operator_id=self.operator_id,
            lat_deg=position.lat_deg,
            lon_deg=position.lon_deg,
            alt_hae_m=None if undulation is None else position.alt_amsl_m + undulation,
            timestamp=datetime.fromtimestamp(
                time.time() if unix_s is None else unix_s, tz=UTC
            ),
            airborne=self.state.armed,
            track_deg=track if speed > 0.3 else None,
            speed_ms=speed,
            vertical_speed_ms=-position.vd_ms,
            height_over_takeoff_m=position.alt_above_home_m,
            operator_lat_deg=None if takeoff is None else takeoff[0],
            operator_lon_deg=None if takeoff is None else takeoff[1],
        )


async def follow_sitl(
    vehicles: list[SitlFlight], store: FlightStore, *, poll_s: float = 0.05
) -> None:
    """Read every vehicle's MAVLink (never writing) and keep the store current."""
    while True:
        now_s = time.monotonic()
        for vehicle in vehicles:
            while (msg := vehicle.source.recv_match(blocking=False)) is not None:
                if msg.get_type() != "BAD_DATA":
                    vehicle.state.update(msg, now_s=now_s)
            flight = vehicle.flight()
            if flight is not None and vehicle.state.heard_s == now_s:
                store.put(flight)
        await asyncio.sleep(poll_s)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.fake_rid_sp")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret-file", type=Path, required=True)
    parser.add_argument(
        "--max-diagonal-km", type=float, default=DEFAULT_MAX_DIAGONAL_KM
    )
    parser.add_argument("--sysid", type=int, action="append", default=[])
    parser.add_argument("--serial", action="append", default=[])
    parser.add_argument("--operator-id", action="append", default=[])
    parser.add_argument(
        "--mavlink", action="append", default=[], help="pymavlink address per vehicle"
    )
    parser.add_argument("--geoid", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - wiring
    import uvicorn
    from pymavlink import mavutil

    args = build_parser().parse_args(argv)
    secret = args.client_secret_file.read_text(encoding="utf-8").strip()
    if not secret:
        print(f"error: {args.client_secret_file} is empty")
        return 2
    count = len(args.sysid)
    if not (len(args.serial) == len(args.operator_id) == len(args.mavlink) == count):
        print("error: give --serial, --operator-id and --mavlink once per --sysid")
        return 2
    geoid = GeoidGrid.load(args.geoid) if args.geoid else None
    store = FlightStore()
    vehicles = [
        SitlFlight(
            flight_id=f"sitl-{sysid}",
            serial=serial or None,
            operator_id=operator or None,
            state=VehicleState(sysid=sysid),
            source=mavutil.mavlink_connection(address),
            geoid=geoid,
        )
        for sysid, serial, operator, address in zip(
            args.sysid, args.serial, args.operator_id, args.mavlink, strict=True
        )
    ]

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(follow_sitl(vehicles, store))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            for vehicle in vehicles:
                vehicle.source.close()

    app = create_app(
        store,
        client_id=args.client_id,
        client_secret=secret,
        max_diagonal_km=args.max_diagonal_km,
        lifespan=lifespan,
    )
    for vehicle, address in zip(vehicles, args.mavlink, strict=True):
        print(f"SYSID {vehicle.state.sysid} on {address} -> flight {vehicle.flight_id}")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
