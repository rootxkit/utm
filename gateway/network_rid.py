"""Network Remote ID: a Display Provider client of ASTM F3411. U-02.

The EU model identifies a drone two ways (`ARCHITECTURE.md` §2): direct
broadcast, which `gateway/remote_id.py` takes from receivers, and network
Remote ID, which USSPs serve. This module is the second: it plays an
ASTM F3411 **Display Provider** towards a configured USSP **Service
Provider** (SP), polls the flights in configured areas, and maps each into
the internal track format (`gateway/publisher.py`), so the console and the
airspace monitor take it like any other source.

## The interface, and which version

F3411-22a ("v2"), as InterUSS publishes it in `interuss/monitoring`
(`interfaces/rid/v2/remoteid/updated.yaml`, the SP's `/uss/flights`
endpoints, and the `uss_qualifier` RID v2 checks). The standard itself was
not available here; every field name below is the InterUSS schema's, and
where F3411-19 ("v1") named a field differently it is accepted too and
noted. If the deployed SPs turn out to speak only v1, the fallbacks below
are the whole difference.

    GET {base_url}/uss/flights?view=lat1,lng1,lat2,lng2
        -> GetFlightsResponse {timestamp: Time, flights: [RIDFlight]}
    GET {base_url}/uss/flights/{id}/details
        -> GetFlightDetailsResponse {details: RIDFlightDetails}

    Time              {value: RFC 3339 string, format: "RFC3339"}
    RIDFlight         {id, aircraft_type, current_state: RIDAircraftState,
                       simulated, recent_positions, operating_area}
    RIDAircraftState  {timestamp: Time, timestamp_accuracy (s),
                       operational_status: Undeclared | Ground | Airborne |
                         Emergency | RemoteIDSystemFailure,
                       position: RIDAircraftPosition, track (deg, 361 unknown),
                       speed (m/s, 255 unknown), speed_accuracy,
                       vertical_speed (m/s up, 63 unknown)}
    RIDAircraftPosition {lat, lng, alt (m above the WGS-84 ellipsoid,
                       -1000 unknown), accuracy_h, accuracy_v (VAUnknown,
                       VA150mPlus, VA150m, VA45m, VA25m, VA10m, VA3m, VA1m),
                       extrapolated, pressure_altitude (m, -1000 unknown),
                       height: {distance, reference: TakeoffLocation |
                       GroundLevel}}
    RIDFlightDetails  {id, operator_id, operator_location: {position:
                       {lat, lng}}, operation_description,
                       uas_id: {serial_number, registration_id, utm_id,
                       specific_session_id}, eu_classification}

v1 differences accepted: `height` beside `position` rather than inside it;
`serial_number` and `registration_number` at the top of the details rather
than under `uas_id`; `operator_location` as a bare `{lat, lng}`.

## Areas and "paging"

F3411 has no paging token: an SP answers a view whose diagonal is at most
`NetMaxDisplayAreaDiagonalKm` (7 km in v22a, 3.6 km in v19) and refuses a
larger one with 413. So a configured area is split into tiles no larger
than `max_diagonal_km`, each tile is one request, and a flight seen in two
tiles is one flight. A tile the SP still refuses with 413 is split in four
and retried, at most `MAX_SPLIT_DEPTH` times.

## Auth

OAuth 2 client credentials (RFC 6749 §4.4): a POST of
`grant_type=client_credentials`, the scope (`rid.display_provider` in
v22a) and, when configured, the audience, with the client's id and secret
as HTTP Basic credentials. The token is reused until shortly before it
expires; a 401 from the SP drops it and the request is tried once more
with a new one. Secrets come from the environment and are never logged.

## Time (gateway/README.md, "Time on the bus")

- `ts`: the state's own `timestamp`, on the provider's clock.
- `rx_ts`: when this client received the response, on ours.
- `captured_at`: `rx_ts` less how far the state's timestamp is behind the
  response's own `timestamp`. That is the relay's rule for a batch
  (`captured_at = rx_ts - (newest ts - ts)`), with the response time as the
  newest: the provider's clock cancels, and its skew against ours cannot
  move the aircraft. A state ahead of its response is clamped to `rx_ts`
  and counted; one older than `max_age_s` (F3411's
  NetMaxNearRealTimeDataPeriod, 60 s) is not published and counted. A
  response without a timestamp falls back to the direct Remote ID rule:
  the state's time when within `time_tolerance_s` ahead and
  `max_latency_s` behind our clock, else `rx_ts`.

The flight's `extrapolated` flag and the time source are carried in
`network_rid`, so a position the SP projected forward is visibly one.

## Nothing here is authenticated beyond the provider

The SP is authenticated (OAuth, TLS); what it says about an aircraft is as
trustworthy as the SP. Every track says `source: "network_remote_id"`,
`trust: "provider"`, `authenticated: false`, and the console shows it as a
claim, like a broadcast.
"""

from __future__ import annotations

import math
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from common import get_logger
from gateway import odid
from gateway.remote_id import (
    DEFAULT_MAX_LATENCY_S,
    DEFAULT_MIN_VERTICAL_ACCURACY,
    DEFAULT_TIME_TOLERANCE_S,
    REMOTE_ID_NAMESPACE,
    TIME_SOURCE_BROADCAST,
    TIME_SOURCE_RECEIVER,
    Geoid,
    aircraft_id,
    amsl,
)

_log = get_logger(__name__)

SOURCE = "network_remote_id"
TRUST = "provider"

# F3411-22a (InterUSS rid v2): NetMaxDisplayAreaDiagonalKm.
DEFAULT_MAX_DIAGONAL_KM = 7.0
# F3411: NetMaxNearRealTimeDataPeriodSeconds.
DEFAULT_MAX_AGE_S = 60.0
DEFAULT_DETAILS_TTL_S = 60.0
DEFAULT_SCOPE = "rid.display_provider"
# A tile the SP refuses as too large is split in four at most this often.
MAX_SPLIT_DEPTH = 3
# Renew a token this long before the provider says it expires.
TOKEN_MARGIN_S = 30.0
# Used when the token response gives no lifetime.
DEFAULT_TOKEN_LIFETIME_S = 300.0

_UNKNOWN_ALT_M = -1000.0
_UNKNOWN_TRACK_DEG = 361.0
_UNKNOWN_SPEED_MS = 255.0
_UNKNOWN_VSPEED_MS = 63.0
_KM_PER_DEG_LAT = 111.32

# accuracy_v to the ODID vertical accuracy code (MAV_ODID_VER_ACC), so the
# S-33 rule on a poor geodetic altitude is applied as for a broadcast.
# VA150mPlus has no ODID code of its own; it is as poor as code 1 or worse.
_VERTICAL_ACCURACY = {
    "VAUnknown": 0,
    "VA150mPlus": 1,
    "VA150m": 1,
    "VA45m": 2,
    "VA25m": 3,
    "VA10m": 4,
    "VA3m": 5,
    "VA1m": 6,
}
_STATUS = {
    "Undeclared": odid.Status.UNDECLARED,
    "Ground": odid.Status.GROUND,
    "Airborne": odid.Status.AIRBORNE,
    "Emergency": odid.Status.EMERGENCY,
    "RemoteIDSystemFailure": odid.Status.REMOTE_ID_SYSTEM_FAILURE,
}


class ProviderError(Exception):
    """The SP or its token endpoint could not be used for one poll."""


class AuthError(ProviderError):
    """The token endpoint refused the client, or gave no token."""


class FormatError(ProviderError):
    """A response that is not what F3411 says it is."""


# --- areas ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Area:
    """A box, in degrees: south-west and north-east corners."""

    lat_min: float
    lon_min: float
    lat_max: float
    lon_max: float

    def __post_init__(self) -> None:
        if not (-90 <= self.lat_min < self.lat_max <= 90):
            raise ValueError(f"latitudes out of order or range: {self}")
        if not (-180 <= self.lon_min < self.lon_max <= 180):
            raise ValueError(f"longitudes out of order or range: {self}")

    def diagonal_km(self) -> float:
        """An upper bound on the box's diagonal: its east-west side measured
        at the latitude where a degree of longitude is longest."""
        widest = (
            0.0
            if self.lat_min <= 0 <= self.lat_max
            else min(abs(self.lat_min), abs(self.lat_max))
        )
        dy = (self.lat_max - self.lat_min) * _KM_PER_DEG_LAT
        dx = (
            (self.lon_max - self.lon_min)
            * _KM_PER_DEG_LAT
            * math.cos(math.radians(widest))
        )
        return math.hypot(dx, dy)

    def view(self) -> str:
        """The `view` query parameter: lat1,lng1,lat2,lng2."""
        return f"{self.lat_min:.7f},{self.lon_min:.7f},{self.lat_max:.7f},{self.lon_max:.7f}"

    def quarters(self) -> list[Area]:
        lat_mid = (self.lat_min + self.lat_max) / 2
        lon_mid = (self.lon_min + self.lon_max) / 2
        return [
            Area(self.lat_min, self.lon_min, lat_mid, lon_mid),
            Area(self.lat_min, lon_mid, lat_mid, self.lon_max),
            Area(lat_mid, self.lon_min, self.lat_max, lon_mid),
            Area(lat_mid, lon_mid, self.lat_max, self.lon_max),
        ]

    def tiles(self, max_diagonal_km: float) -> list[Area]:
        """Tiles covering the area, each no larger than `max_diagonal_km`."""
        if max_diagonal_km <= 0:
            raise ValueError("max_diagonal_km must be positive")
        # Each of n x n tiles is at most 1/n of the box's diagonal bound.
        n = max(1, math.ceil(self.diagonal_km() / max_diagonal_km))
        dlat = (self.lat_max - self.lat_min) / n
        dlon = (self.lon_max - self.lon_min) / n
        return [
            Area(
                self.lat_min + i * dlat,
                self.lon_min + j * dlon,
                self.lat_min + (i + 1) * dlat,
                self.lon_min + (j + 1) * dlon,
            )
            for i in range(n)
            for j in range(n)
        ]


# --- parsing ----------------------------------------------------------------


def parse_time(value: Any) -> datetime | None:
    """An F3411 `Time` ({value, format}) or a bare RFC 3339 string; None
    when absent. Raises FormatError for something that is neither."""
    if value is None:
        return None
    text = value.get("value") if isinstance(value, dict) else value
    if not isinstance(text, str):
        raise FormatError(f"not a Time: {value!r}")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise FormatError(f"not an RFC 3339 time: {text!r}") from error
    if moment.tzinfo is None:
        raise FormatError(f"a time without a zone: {text!r}")
    return moment


def _number(raw: dict[str, Any], name: str) -> float | None:
    value = raw.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FormatError(f"{name} is not a number: {value!r}")
    if not math.isfinite(value):
        raise FormatError(f"{name} is not finite: {value!r}")
    return float(value)


@dataclass(frozen=True, slots=True)
class Details:
    """What a flight's details say about who it is."""

    serial: str | None = None
    registration_id: str | None = None
    operator_id: str | None = None
    operator_lat_deg: float | None = None
    operator_lon_deg: float | None = None
    eu_category: str | None = None
    eu_class: str | None = None


def _text(raw: Any) -> str | None:
    return raw.strip() or None if isinstance(raw, str) else None


def parse_details(body: Any) -> Details:
    """A GetFlightDetailsResponse (v22a), or its v19 shape."""
    if not isinstance(body, dict) or not isinstance(body.get("details"), dict):
        raise FormatError("no details object")
    details: dict[str, Any] = body["details"]
    uas_id = details.get("uas_id") if isinstance(details.get("uas_id"), dict) else {}
    assert isinstance(uas_id, dict)
    location = details.get("operator_location")
    position = location.get("position", location) if isinstance(location, dict) else {}
    if not isinstance(position, dict):
        position = {}
    classification = details.get("eu_classification")
    classification = classification if isinstance(classification, dict) else {}
    return Details(
        serial=_text(uas_id.get("serial_number"))
        or _text(details.get("serial_number")),
        registration_id=_text(uas_id.get("registration_id"))
        or _text(details.get("registration_number")),
        operator_id=_text(details.get("operator_id")),
        operator_lat_deg=_number(position, "lat"),
        operator_lon_deg=_number(position, "lng"),
        eu_category=_text(classification.get("category")),
        eu_class=_text(classification.get("class")),
    )


@dataclass(frozen=True, slots=True)
class FlightState:
    """One RIDFlight's current state, parsed."""

    flight_id: str
    aircraft_type: str | None
    simulated: bool
    timestamp: datetime
    timestamp_accuracy_s: float | None
    status: int
    lat_deg: float
    lon_deg: float
    alt_hae_m: float | None
    alt_pressure_m: float | None
    vertical_accuracy: int
    extrapolated: bool
    height_m: float | None
    height_reference: str | None
    track_deg: float | None
    speed_ms: float | None
    vertical_speed_ms: float | None


def parse_flights(body: Any) -> tuple[datetime | None, list[FlightState]]:
    """A GetFlightsResponse: its own timestamp, and each flight with a
    current state. A flight without one (the SP knows it, but has no
    position to give) is left out."""
    if not isinstance(body, dict):
        raise FormatError("not a JSON object")
    flights = body.get("flights", [])
    if not isinstance(flights, list):
        raise FormatError("flights is not a list")
    out = []
    for raw in flights:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise FormatError(f"a flight without an id: {raw!r}")
        state = raw.get("current_state")
        if state is None:
            continue
        out.append(_parse_state(raw, state))
    return parse_time(body.get("timestamp")), out


def _parse_state(flight: dict[str, Any], state: Any) -> FlightState:
    if not isinstance(state, dict):
        raise FormatError("current_state is not an object")
    position = state.get("position")
    if not isinstance(position, dict):
        raise FormatError("current_state has no position")
    lat, lon = _number(position, "lat"), _number(position, "lng")
    if lat is None or lon is None:
        raise FormatError("a position without lat or lng")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise FormatError(f"a position out of range: {lat}, {lon}")
    timestamp = parse_time(state.get("timestamp"))
    if timestamp is None:
        raise FormatError("current_state has no timestamp")
    # v22a puts height inside position; v19 beside it.
    height = position.get("height", state.get("height"))
    height = height if isinstance(height, dict) else {}
    status = state.get("operational_status", "Undeclared")
    return FlightState(
        flight_id=flight["id"],
        aircraft_type=_text(flight.get("aircraft_type")),
        simulated=flight.get("simulated") is True,
        timestamp=timestamp,
        timestamp_accuracy_s=_number(state, "timestamp_accuracy"),
        status=_STATUS.get(str(status), odid.Status.UNDECLARED),
        lat_deg=lat,
        lon_deg=lon,
        alt_hae_m=_unknown(_number(position, "alt"), _UNKNOWN_ALT_M),
        alt_pressure_m=_unknown(_number(position, "pressure_altitude"), _UNKNOWN_ALT_M),
        vertical_accuracy=_VERTICAL_ACCURACY.get(str(position.get("accuracy_v")), 0),
        extrapolated=position.get("extrapolated") is True,
        height_m=_number(height, "distance"),
        height_reference=_text(height.get("reference")),
        track_deg=_below(_number(state, "track"), _UNKNOWN_TRACK_DEG),
        speed_ms=_below(_number(state, "speed"), _UNKNOWN_SPEED_MS),
        vertical_speed_ms=_unknown(
            _number(state, "vertical_speed"), _UNKNOWN_VSPEED_MS
        ),
    )


def _unknown(value: float | None, marker: float) -> float | None:
    return None if value is None or value == marker else value


def _below(value: float | None, limit: float) -> float | None:
    """Track 361 and speed 255 (and above) are F3411's "unknown"."""
    return None if value is None or value >= limit else value


# --- the client ---------------------------------------------------------------


@dataclass
class TokenSource:
    """OAuth 2 client credentials for one provider."""

    http: httpx.AsyncClient
    token_url: str
    client_id: str
    client_secret: str
    scope: str = DEFAULT_SCOPE
    audience: str | None = None
    clock_s: Callable[[], float] = time.monotonic
    _token: str | None = field(default=None, init=False, repr=False)
    _expires_s: float = field(default=0.0, init=False)

    def drop(self) -> None:
        self._token = None

    async def token(self) -> str:
        if self._token is not None and self.clock_s() < self._expires_s:
            return self._token
        form = {"grant_type": "client_credentials", "scope": self.scope}
        if self.audience:
            form["audience"] = self.audience
        try:
            response = await self.http.post(
                self.token_url, data=form, auth=(self.client_id, self.client_secret)
            )
        except httpx.HTTPError as error:
            raise ProviderError(f"token endpoint unreachable: {error!r}") from error
        if response.status_code != 200:
            raise AuthError(f"token endpoint answered {response.status_code}")
        try:
            body = response.json()
        except ValueError as error:
            raise AuthError("token response is not JSON") from error
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise AuthError("token response has no access_token")
        lifetime = body.get("expires_in", DEFAULT_TOKEN_LIFETIME_S)
        lifetime_s = (
            float(lifetime)
            if isinstance(lifetime, int | float)
            else DEFAULT_TOKEN_LIFETIME_S
        )
        self._token = token
        self._expires_s = self.clock_s() + max(0.0, lifetime_s - TOKEN_MARGIN_S)
        return token


@dataclass
class ServiceProviderClient:
    """GETs against one SP, authenticated, for one Display Provider."""

    http: httpx.AsyncClient
    base_url: str
    tokens: TokenSource

    async def _get(
        self, path: str, params: dict[str, str] | None = None
    ) -> httpx.Response:
        url = f"{self.base_url.rstrip('/')}{path}"
        for attempt in (1, 2):
            token = await self.tokens.token()
            try:
                response = await self.http.get(
                    url, params=params, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as error:
                raise ProviderError(f"{path}: {error!r}") from error
            if response.status_code == 401 and attempt == 1:
                # The token may have been revoked or expired early.
                self.tokens.drop()
                continue
            return response
        raise AuthError(f"{path}: refused with a fresh token")  # pragma: no cover

    async def flights(self, area: Area) -> httpx.Response:
        return await self._get("/uss/flights", {"view": area.view()})

    async def details(self, flight_id: str) -> Details:
        response = await self._get(f"/uss/flights/{flight_id}/details")
        if response.status_code == 401:
            raise AuthError("details refused with a fresh token")
        if response.status_code != 200:
            raise ProviderError(f"details answered {response.status_code}")
        try:
            return parse_details(response.json())
        except ValueError as error:
            raise FormatError("details response is not JSON") from error


class TooLargeError(ProviderError):
    """The SP refused a view as too large (413)."""


async def flights_in(
    client: ServiceProviderClient, area: Area, *, depth: int = 0
) -> tuple[datetime | None, list[FlightState]]:
    """The flights in one tile, splitting it while the SP says 413."""
    response = await client.flights(area)
    if response.status_code == 413:
        if depth >= MAX_SPLIT_DEPTH:
            raise TooLargeError(
                f"view still too large after {depth} splits: {area.view()}"
            )
        newest: datetime | None = None
        found: list[FlightState] = []
        for quarter in area.quarters():
            at, flights = await flights_in(client, quarter, depth=depth + 1)
            found.extend(flights)
            if at is not None and (newest is None or at > newest):
                newest = at
        return newest, found
    if response.status_code == 401:
        raise AuthError("flights refused with a fresh token")
    if response.status_code != 200:
        raise ProviderError(f"flights answered {response.status_code}")
    try:
        body = response.json()
    except ValueError as error:
        raise FormatError("flights response is not JSON") from error
    return parse_flights(body)


# --- mapping ------------------------------------------------------------------


def flight_aircraft_id(provider: str, flight_id: str) -> UUID:
    """The id of a flight whose serial is not known: per provider and flight."""
    return uuid.uuid5(REMOTE_ID_NAMESPACE, f"network:{provider}:{flight_id}")


@dataclass(frozen=True, slots=True)
class Placement:
    ts: datetime
    captured_at: datetime
    time_source: str
    # Why the state's own time was not used, or was clamped; None if it was.
    note: str | None


def place(
    state: FlightState,
    *,
    response_at: datetime | None,
    received_at: datetime,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    time_tolerance_s: float = DEFAULT_TIME_TOLERANCE_S,
    max_latency_s: float = DEFAULT_MAX_LATENCY_S,
) -> Placement | None:
    """Where the state sits in time on our clock; None when it is older than
    `max_age_s` and must not be shown as current (module docstring)."""
    ts = state.timestamp
    if response_at is not None:
        behind_s = (response_at - ts).total_seconds()
        if behind_s > max_age_s:
            return None
        if behind_s < 0:
            return Placement(ts, received_at, TIME_SOURCE_RECEIVER, "ahead_of_response")
        return Placement(
            ts,
            received_at - timedelta(seconds=behind_s),
            TIME_SOURCE_BROADCAST,
            None,
        )
    age_s = (received_at - ts).total_seconds()
    if age_s > max_age_s:
        return None
    if age_s < -time_tolerance_s:
        return Placement(ts, received_at, TIME_SOURCE_RECEIVER, "clock_ahead")
    if age_s > max_latency_s:
        return Placement(ts, received_at, TIME_SOURCE_RECEIVER, "too_old")
    return Placement(ts, ts, TIME_SOURCE_BROADCAST, None)


def to_location(state: FlightState) -> odid.Location:
    """The state as a direct Remote ID Location, so altitude and velocity are
    derived by exactly the rules a broadcast's are (S-33)."""
    over_takeoff = state.height_reference == "TakeoffLocation"
    return odid.Location(
        status=state.status,
        direction_deg=state.track_deg,
        speed_horizontal_ms=state.speed_ms,
        speed_vertical_ms=state.vertical_speed_ms,
        lat_deg=state.lat_deg,
        lon_deg=state.lon_deg,
        alt_baro_m=state.alt_pressure_m,
        alt_hae_m=state.alt_hae_m,
        height_reference=(
            odid.HeightReference.OVER_TAKEOFF
            if over_takeoff
            else odid.HeightReference.OVER_GROUND
        ),
        height_m=state.height_m,
        horiz_accuracy=0,
        vert_accuracy=state.vertical_accuracy,
        baro_accuracy=0,
        speed_accuracy=0,
        ts_accuracy=0,
        seconds_after_hour=None,
    )


def observation(
    state: FlightState,
    details: Details | None,
    placement: Placement,
    *,
    provider: str,
    received_at: datetime,
    geoid: Geoid | None,
    min_vertical_accuracy: int = DEFAULT_MIN_VERTICAL_ACCURACY,
    registered: tuple[UUID, str] | None = None,
) -> dict[str, Any]:
    """A flight as a `telemetry.<id>` message. `registered` is the registry's
    (drone_id, label) when the flight's serial is a registered aircraft."""
    location = to_location(state)
    alt_amsl_m, alt_source = amsl(
        location, geoid, min_vertical_accuracy=min_vertical_accuracy
    )
    serial = None if details is None else details.serial
    if registered is not None:
        drone_id, label = registered
    elif serial is not None:
        drone_id = aircraft_id(
            odid.BasicId(id_type=odid.IdType.SERIAL_NUMBER, ua_type=0, ua_id=serial)
        )
        label = serial
    else:
        drone_id = flight_aircraft_id(provider, state.flight_id)
        label = (details.registration_id if details else None) or (
            f"{provider}:{state.flight_id}"
        )
    vn, ve = _velocity(state)
    vd = None if state.vertical_speed_ms is None else -state.vertical_speed_ms
    over_takeoff = state.height_reference == "TakeoffLocation"
    return {
        "drone_id": str(drone_id),
        "label": label,
        "source": SOURCE,
        "trust": TRUST,
        "authenticated": False,
        "link": None,
        "firmware": None,
        "ts": placement.ts.isoformat(),
        "rx_ts": received_at.isoformat(),
        "captured_at": placement.captured_at.isoformat(),
        "backlog": False,
        # The instance U-15 switches: the provider.
        "station_id": provider,
        "lat_deg": state.lat_deg,
        "lon_deg": state.lon_deg,
        "alt_amsl_m": alt_amsl_m,
        "alt_source": alt_source,
        "alt_hae_m": state.alt_hae_m,
        "alt_pressure_m": state.alt_pressure_m,
        "alt_above_home_m": state.height_m if over_takeoff else None,
        "heading_deg": None,
        "track_deg": state.track_deg,
        "vx_ms": vn,
        "vy_ms": ve,
        "vz_ms": vd,
        "batt_pct": None,
        "batt_voltage_v": None,
        "batt_consumed_wh": None,
        "mode": None,
        "armed": None,
        "airborne": state.status != odid.Status.GROUND,
        "gps_fix_type": None,
        "sat_count": None,
        "groundspeed_ms": state.speed_ms,
        "climb_ms": state.vertical_speed_ms,
        "network_rid": {
            "provider": provider,
            "flight_id": state.flight_id,
            "aircraft_type": state.aircraft_type,
            "simulated": state.simulated,
            "extrapolated": state.extrapolated,
            "serial": serial,
            "registration_id": None if details is None else details.registration_id,
            "operator_id": None if details is None else details.operator_id,
            "operator_lat_deg": None if details is None else details.operator_lat_deg,
            "operator_lon_deg": None if details is None else details.operator_lon_deg,
            "time_source": placement.time_source,
            "ts_accuracy_s": state.timestamp_accuracy_s,
        },
    }


def _velocity(state: FlightState) -> tuple[float | None, float | None]:
    if state.speed_ms is None or state.track_deg is None:
        return None, None
    track = math.radians(state.track_deg)
    return state.speed_ms * math.cos(track), state.speed_ms * math.sin(track)


def unique(flights: list[FlightState]) -> Iterator[FlightState]:
    """One state per flight id, the newest, across overlapping tiles."""
    newest: dict[str, FlightState] = {}
    for flight in flights:
        held = newest.get(flight.flight_id)
        if held is None or flight.timestamp > held.timestamp:
            newest[flight.flight_id] = flight
    return iter(newest.values())
