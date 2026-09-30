"""Which aircraft are near which, on every telemetry tick. P5-06.

A uniform grid over latitude and longitude, with cells at least as large as
the search radius, so every neighbour within the radius is in the query's own
cell or one of the eight around it. The candidates are then filtered by exact
distance (`airspace.cpa.horizontal_distance_m`), so the grid only has to be
conservative, never exact.

A cell's east-west size in degrees is chosen at the poleward edge of its
latitude band, where a degree of longitude is shortest, which keeps it at
least `radius` wide across the whole band.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

from airspace.cpa import Track, horizontal_distance_m

# Metres per degree of latitude, a lower bound over the ellipsoid (the
# equatorial value), so cells are never smaller than intended.
_M_PER_DEG_LAT_MIN = 110_574.0
_M_PER_DEG_LON_EQUATOR = 111_320.0
# Beyond this latitude the grid gives up on longitude and uses one column per
# band. Nothing here flies there; it only keeps the arithmetic finite.
_MAX_LAT_DEG = 89.0

Cell = tuple[int, int]


@dataclass
class NeighbourIndex:
    radius_m: float

    _tracks: dict[UUID, Track] = field(default_factory=dict, init=False)
    _cell_of: dict[UUID, Cell] = field(default_factory=dict, init=False)
    _cells: dict[Cell, set[UUID]] = field(
        default_factory=lambda: defaultdict(set), init=False
    )

    def __post_init__(self) -> None:
        if self.radius_m <= 0:
            raise ValueError("radius_m must be positive")
        self._dlat_deg = self.radius_m / _M_PER_DEG_LAT_MIN

    def __len__(self) -> int:
        return len(self._tracks)

    def upsert(self, track: Track) -> None:
        cell = self._cell(track.lat_deg, track.lon_deg)
        previous = self._cell_of.get(track.drone_id)
        if previous is not None and previous != cell:
            self._cells[previous].discard(track.drone_id)
            if not self._cells[previous]:
                del self._cells[previous]
        self._cells[cell].add(track.drone_id)
        self._cell_of[track.drone_id] = cell
        self._tracks[track.drone_id] = track

    def remove(self, drone_id: UUID) -> None:
        cell = self._cell_of.pop(drone_id, None)
        self._tracks.pop(drone_id, None)
        if cell is not None:
            self._cells[cell].discard(drone_id)
            if not self._cells[cell]:
                del self._cells[cell]

    def track(self, drone_id: UUID) -> Track | None:
        return self._tracks.get(drone_id)

    def tracks(self) -> list[Track]:
        """Every tracked aircraft, for rebuilding the index at a new radius."""
        return list(self._tracks.values())

    def neighbours(self, drone_id: UUID) -> list[Track]:
        """Every other tracked aircraft within `radius_m` of this one."""
        me = self._tracks.get(drone_id)
        if me is None:
            return []
        row, _ = self._cell_of[drone_id]
        found: list[Track] = []
        for d_row in (-1, 0, 1):
            band = row + d_row
            # Neighbouring bands have their own column widths, so the query
            # position is re-bucketed in each band rather than offset.
            _, column = self._cell_in_band(band, me.lon_deg)
            columns = self._columns_in_band(band)
            for d_column in (-1, 0, 1):
                # Wrapped: the column west of the first is the last, so a pair
                # straddling the antimeridian is found (S-12).
                cell = (band, (column + d_column) % columns)
                for other_id in self._cells.get(cell, ()):
                    if other_id == drone_id:
                        continue
                    other = self._tracks[other_id]
                    if horizontal_distance_m(me, other) <= self.radius_m:
                        found.append(other)
        return found

    def _cell(self, lat_deg: float, lon_deg: float) -> Cell:
        band = math.floor(lat_deg / self._dlat_deg)
        return self._cell_in_band(band, lon_deg)

    def _columns_in_band(self, band: int) -> int:
        """How many columns go round the band: as many as fit at the poleward
        edge, where a degree of longitude is shortest, so every column is at
        least `radius_m` wide and they divide 360 degrees exactly. An uneven
        last column could be narrower than the radius, and a neighbour two
        columns west across the antimeridian would then be missed."""
        edge_deg = min(
            max(abs(band * self._dlat_deg), abs((band + 1) * self._dlat_deg)),
            _MAX_LAT_DEG,
        )
        m_per_deg_lon = _M_PER_DEG_LON_EQUATOR * math.cos(math.radians(edge_deg))
        return max(1, math.floor(360.0 * m_per_deg_lon / self.radius_m))

    def _cell_in_band(self, band: int, lon_deg: float) -> Cell:
        columns = self._columns_in_band(band)
        # Longitude normalised into [0, 360) so 180 and -180 are one column.
        return band, math.floor((lon_deg + 180.0) % 360.0 / 360.0 * columns) % columns
