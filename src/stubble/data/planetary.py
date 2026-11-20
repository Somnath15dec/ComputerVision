"""Microsoft Planetary Computer STAC provider (anonymous access, no account needed)."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np
import planetary_computer as pc
import pystac_client
from odc.geo.geobox import GeoBox
from odc.stac import configure_rio
from odc.stac import load as stac_load

from stubble.aoi import GridSpec
from stubble.config import Config
from stubble.data.base import Acquisition, SceneRef, Sensor

log = logging.getLogger(__name__)

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"


class PlanetaryComputerProvider:
    """Searches PC STAC and reads COG windows with odc-stac."""

    name = "planetary_computer"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # Unsigned client; items are signed right before reading so SAS tokens are fresh.
        self.client = pystac_client.Client.open(STAC_URL)
        configure_rio(cloud_defaults=True, GDAL_HTTP_MAX_RETRY="5", GDAL_HTTP_RETRY_DELAY="2")

    def _collection(self, sensor: Sensor) -> str:
        return self.cfg.sentinel2.collection if sensor == "s2" else self.cfg.sentinel1.collection

    def search(self, sensor: Sensor, start: date, end: date,
               bbox_wgs84: tuple[float, float, float, float]) -> list[SceneRef]:
        """List items; end date is inclusive."""
        dt = f"{start.isoformat()}T00:00:00Z/{(end + timedelta(days=1)).isoformat()}T00:00:00Z"
        query = None
        if sensor == "s2":
            query = {"eo:cloud_cover": {"lt": self.cfg.sentinel2.max_scene_cloud_pct}}
        items = self.client.search(
            collections=[self._collection(sensor)], bbox=list(bbox_wgs84), datetime=dt,
            query=query,
        ).item_collection()
        refs = [
            SceneRef(
                id=it.id, datetime=it.datetime, platform=it.properties.get("platform", ""),
                orbit_state=it.properties.get("sat:orbit_state"),
                properties=dict(it.properties), handle=it,
            )
            for it in items
        ]
        if sensor == "s1" and self.cfg.sentinel1.orbit_state != "any":
            refs = [r for r in refs if r.orbit_state == self.cfg.sentinel1.orbit_state]
        return sorted(refs, key=lambda r: (r.datetime, r.id))

    def get_items(self, sensor: Sensor, ids: list[str]) -> list[SceneRef]:
        """Fetch catalogue items by ID (adds assets for already-cached acquisitions)."""
        items = self.client.search(collections=[self._collection(sensor)], ids=ids
                                   ).item_collection()
        refs = [SceneRef(id=it.id, datetime=it.datetime,
                         platform=it.properties.get("platform", ""),
                         orbit_state=it.properties.get("sat:orbit_state"),
                         properties=dict(it.properties), handle=it) for it in items]
        missing = set(ids) - {r.id for r in refs}
        if missing:
            raise RuntimeError(f"items not found in catalogue: {sorted(missing)}")
        return sorted(refs, key=lambda r: (r.datetime, r.id))

    def load_static(self, collection: str, asset: str, grid: GridSpec,
                    bbox_wgs84: tuple[float, float, float, float]
                    ) -> tuple[np.ndarray, list[str]]:
        """Mosaic a static categorical layer (e.g. ESA WorldCover) onto ``grid`` (nearest).

        Returns the uint8 array (0 = no data) and the item IDs used.
        """
        items = list(self.client.search(collections=[collection], bbox=list(bbox_wgs84)
                                        ).items())
        if collection == "esa-worldcover":  # keep the newest version only (2021 v200)
            key = "esa_worldcover:product_version"
            newest = max(it.properties.get(key, "") for it in items)
            items = [it for it in items if it.properties.get(key) == newest]
        items = [pc.sign(it) for it in items]
        geobox = GeoBox((grid.height, grid.width), grid.transform, grid.crs)
        ds = stac_load(items, bands=[asset], geobox=geobox, groupby="id",
                       chunks={"x": 2048, "y": 2048}, resampling="nearest", dtype="uint8",
                       nodata=0)
        arr = ds[asset].max("time").compute().values  # tiles do not overlap: max = mosaic
        return arr, [it.id for it in items]

    def load(self, acq: Acquisition, grid: GridSpec, bands: list[str]) -> dict[str, np.ndarray]:
        """Load and mosaic the acquisition's bands onto the AOI grid (signed Planetary Computer
        URLs).
        """
        items = [pc.sign(s.handle) for s in acq.scenes]
        geobox = GeoBox((grid.height, grid.width), grid.transform, grid.crs)
        chunk = self.cfg.io.download_chunk
        if acq.sensor == "s2":
            resampling = {b: "bilinear" for b in bands}
            resampling[self.cfg.sentinel2.scl_band] = "nearest"
            ds = stac_load(
                items, bands=bands, geobox=geobox, groupby="solar_day",
                chunks={"x": chunk, "y": chunk}, resampling=resampling, dtype="uint16", nodata=0,
            )
        else:
            ds = stac_load(
                items, bands=bands, geobox=geobox, groupby="solar_day",
                chunks={"x": chunk, "y": chunk}, resampling="bilinear", dtype="float32",
                nodata=float("nan"),
            )
        if ds.sizes["time"] != 1:
            raise RuntimeError(f"{acq.key}: expected 1 solar day, got {ds.sizes['time']}")
        ds = ds.isel(time=0).compute()
        return {b: ds[b].values for b in bands}
