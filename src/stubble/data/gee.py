"""Google Earth Engine provider: placeholder implementing the ``ImageryProvider`` protocol."""

from __future__ import annotations

from datetime import date

import numpy as np

from stubble.aoi import GridSpec
from stubble.config import Config
from stubble.data.base import Acquisition, SceneRef, Sensor


class EarthEngineProvider:
    """Not implemented yet. Credentials would come from GEE_* variables in ``.env``."""

    name = "google_earth_engine"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def search(self, sensor: Sensor, start: date, end: date,
               bbox_wgs84: tuple[float, float, float, float]) -> list[SceneRef]:
        """Not implemented: the Earth Engine provider is a placeholder."""
        raise NotImplementedError("GEE provider is not implemented yet; use provider: planetary")

    def load(self, acq: Acquisition, grid: GridSpec, bands: list[str]) -> dict[str, np.ndarray]:
        """Not implemented: the Earth Engine provider is a placeholder."""
        raise NotImplementedError("GEE provider is not implemented yet; use provider: planetary")
