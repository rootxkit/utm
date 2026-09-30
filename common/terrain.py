"""Ground elevation for a position, from locally installed DEM tiles. P5-00.

Height above ground is not in any telemetry (`GLOBAL_POSITION_INT` gives
height above home, and ArduPilot's `TERRAIN_REPORT` was measured reporting
0.0 m through a whole real flight with no terrain loaded). It is computed:

    height_above_ground_m = alt_amsl_m - Terrain.elevation(lat, lon).elevation_m

## Where the tiles come from

`tools/terrain_fetch.py` converts Copernicus DEM tiles (GLO-30 where the
public release has them, GLO-90 elsewhere) into one PGM per 1 x 1 degree
cell, named after its south-west corner (`N41E044.pgm`), and writes an
`index.json` of every cell it was asked for: the dataset used, or "sea" for
a cell neither dataset has a tile for (Copernicus has no ocean tiles; the
height there is 0). A cell not in the index was never fetched, and its
elevation is unknown, not zero.

## What the number is

- Height of the **surface**, not the ground: Copernicus is a surface model,
  so over a city it is the roofs and over a forest the canopy. For clearance
  that errs safe (the aircraft looks lower than it is); for an altitude
  limit it means the limit is applied above the roofs.
- Orthometric height on **EGM2008**, as Copernicus publishes it.
- Accurate to a few metres, and interpolated between samples 30 or 90 m
  apart, so steep slopes add error. The dataset and spacing come with every
  answer so a caller can say which it had.
"""

from __future__ import annotations

import json
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from common import pgm
from common.pgm import Pgm

# Stored samples: elevation = OFFSET + SCALE * value; NODATA is reserved.
OFFSET_M = -500.0
SCALE_M = 0.2
NODATA = 0xFFFF
SEA = "sea"


class TerrainFileError(ValueError):
    """A tile or index that is not what tools/terrain_fetch.py writes."""


@dataclass(frozen=True, slots=True)
class Elevation:
    elevation_m: float
    # "COP-DEM GLO-30", "COP-DEM GLO-90", or "sea".
    dataset: str
    # Distance between samples along a meridian; 0 at sea.
    spacing_m: float


def cell_name(lat_deg: float, lon_deg: float) -> str:
    lat = math.floor(lat_deg)
    lon = math.floor(lon_deg)
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


@dataclass(frozen=True)
class TerrainTile:
    grid: Pgm
    dataset: str
    lat_first_deg: float
    lon_first_deg: float
    lat_step_deg: float
    lon_step_deg: float

    @classmethod
    def parse(cls, data: bytes) -> TerrainTile:
        try:
            grid = pgm.parse(data)
            offset, scale = grid.number("Offset"), grid.number("Scale")
            tile = cls(
                grid=grid,
                dataset=grid.header.get("Dataset", ""),
                lat_first_deg=grid.number("LatFirst"),
                lon_first_deg=grid.number("LonFirst"),
                lat_step_deg=grid.number("LatStep"),
                lon_step_deg=grid.number("LonStep"),
            )
        except pgm.PgmError as error:
            raise TerrainFileError(str(error)) from error
        if (offset, scale) != (OFFSET_M, SCALE_M):
            raise TerrainFileError(f"Offset/Scale {offset}/{scale}, not this format's")
        if not tile.dataset:
            raise TerrainFileError("no Dataset in the header")
        if tile.lat_step_deg <= 0 or tile.lon_step_deg <= 0:
            raise TerrainFileError("steps must be positive")
        return tile

    def elevation_m(self, lat_deg: float, lon_deg: float) -> float | None:
        """Bilinear between the four surrounding samples; None if any is
        nodata. At the tile's edge the nearest samples are used: the next
        tile's first row is not read."""
        fy = (self.lat_first_deg - lat_deg) / self.lat_step_deg
        fx = (lon_deg - self.lon_first_deg) / self.lon_step_deg
        fy = min(max(fy, 0.0), self.grid.height - 1.0)
        fx = min(max(fx, 0.0), self.grid.width - 1.0)
        iy = min(int(fy), self.grid.height - 2)
        ix = min(int(fx), self.grid.width - 2)
        fy -= iy
        fx -= ix
        corners = (
            self.grid.raw(ix, iy),
            self.grid.raw(ix + 1, iy),
            self.grid.raw(ix, iy + 1),
            self.grid.raw(ix + 1, iy + 1),
        )
        if NODATA in corners:
            return None
        v00, v01, v10, v11 = corners
        top = (1 - fx) * v00 + fx * v01
        bottom = (1 - fx) * v10 + fx * v11
        return OFFSET_M + SCALE_M * ((1 - fy) * top + fy * bottom)

    @property
    def spacing_m(self) -> float:
        return self.lat_step_deg * 111_320.0


@dataclass
class Terrain:
    """The tiles in one directory, read when first needed and kept, up to
    `max_tiles` of them (least recently used out first; each is about 26 MB).

    Reading a tile is synchronous. A service on an event loop calls `load`
    through `asyncio.to_thread` before it asks `elevation` on the loop, and
    `is_loaded` says whether it needs to; both are safe from any thread.
    """

    directory: Path
    max_tiles: int = 8
    index: dict[str, str] = field(init=False)
    _tiles: OrderedDict[str, TerrainTile] = field(
        default_factory=OrderedDict, init=False
    )
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        if self.max_tiles < 1:
            raise ValueError("max_tiles must be at least 1")
        path = self.directory / "index.json"
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise TerrainFileError(f"cannot read {path}: {error}") from error
        cells = index.get("cells") if isinstance(index, dict) else None
        if not isinstance(cells, dict):
            raise TerrainFileError(f"{path} has no cells")
        self.index = {str(k): str(v) for k, v in cells.items()}

    def elevation(self, lat_deg: float, lon_deg: float) -> Elevation | None:
        """None when the position is outside what was fetched, or a sample
        around it is nodata: unknown, never zero."""
        if not (math.isfinite(lat_deg) and math.isfinite(lon_deg)):
            raise ValueError("latitude and longitude must be finite")
        name = cell_name(lat_deg, lon_deg)
        dataset = self.index.get(name)
        if dataset is None:
            return None
        if dataset == SEA:
            return Elevation(elevation_m=0.0, dataset=SEA, spacing_m=0.0)
        tile = self._tile(name)
        value = tile.elevation_m(lat_deg, lon_deg)
        if value is None:
            return None
        return Elevation(
            elevation_m=value, dataset=tile.dataset, spacing_m=tile.spacing_m
        )

    @property
    def cached(self) -> list[str]:
        """Cell names held in memory, least recently used first."""
        with self._lock:
            return list(self._tiles)

    def _needs_tile(self, lat_deg: float, lon_deg: float) -> str | None:
        """The cell's name if answering there means reading a tile; None for
        a cell that is unknown or sea."""
        name = cell_name(lat_deg, lon_deg)
        dataset = self.index.get(name)
        return None if dataset is None or dataset == SEA else name

    def is_loaded(self, lat_deg: float, lon_deg: float) -> bool:
        """Whether `elevation` here would answer without reading a file."""
        name = self._needs_tile(lat_deg, lon_deg)
        if name is None:
            return True
        with self._lock:
            return name in self._tiles

    def load(self, lat_deg: float, lon_deg: float) -> None:
        """Read the tile under the position into the cache, if there is one.
        Blocking: meant for a worker thread. Raises `TerrainFileError` as
        `elevation` would."""
        name = self._needs_tile(lat_deg, lon_deg)
        if name is not None:
            self._tile(name)

    def _tile(self, name: str) -> TerrainTile:
        # The lock covers the cache, never the disk: a cached lookup on the
        # event loop must not wait behind a worker reading another 26 MB
        # tile. Two threads reading the same tile at once both parse it and
        # the first to insert wins; that costs a duplicate read, not a wait.
        with self._lock:
            tile = self._tiles.get(name)
            if tile is not None:
                self._tiles.move_to_end(name)
                return tile
        path = self.directory / f"{name}.pgm"
        try:
            read = TerrainTile.parse(path.read_bytes())
        except OSError as error:
            raise TerrainFileError(f"index lists {name} but {path}: {error}") from error
        with self._lock:
            tile = self._tiles.get(name)
            if tile is None:
                tile = self._tiles[name] = read
                while len(self._tiles) > self.max_tiles:
                    self._tiles.popitem(last=False)
            else:
                self._tiles.move_to_end(name)
            return tile
