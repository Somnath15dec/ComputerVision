"""Provider-agnostic imagery interface so Planetary Computer can be swapped for GEE."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol

import numpy as np

from stubble.aoi import GridSpec

Sensor = Literal["s2", "s1"]


@dataclass
class SceneRef:
    """One catalogue item (a single S2 tile-granule or S1 frame)."""

    id: str
    datetime: datetime
    platform: str
    orbit_state: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    handle: Any = None  # provider-specific object (e.g. pystac.Item); never serialised

    def to_record(self) -> dict[str, Any]:
        """JSON-serialisable summary of the scene for provenance files."""
        keep = ("eo:cloud_cover", "s2:mgrs_tile", "s2:processing_baseline",
                "sat:relative_orbit", "sat:orbit_state")
        return {
            "id": self.id,
            "datetime": self.datetime.isoformat(),
            "platform": self.platform,
            "orbit_state": self.orbit_state,
            **{k: self.properties[k] for k in keep if k in self.properties},
        }


@dataclass
class Acquisition:
    """Scenes from the same sensor, day (and orbit for S1), mosaicked as one raster."""

    sensor: Sensor
    day: date
    key: str  # filename stem, e.g. 20251006 or 20251006_asc
    scenes: list[SceneRef]


class ImageryProvider(Protocol):
    """What the pipeline needs from a data source."""

    name: str

    def search(self, sensor: Sensor, start: date, end: date,
               bbox_wgs84: tuple[float, float, float, float]) -> list[SceneRef]:
        """List scenes intersecting ``bbox_wgs84`` between ``start`` and ``end`` (inclusive)."""
        ...

    def load(self, acq: Acquisition, grid: GridSpec, bands: list[str]) -> dict[str, np.ndarray]:
        """Read and mosaic ``bands`` for an acquisition onto ``grid``.

        S2 values are returned as raw DN (uint16, 0 = no data). S1 values as linear
        gamma0 float32 (NaN = no data).
        """
        ...
