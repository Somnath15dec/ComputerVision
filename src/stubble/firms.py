"""NASA FIRMS VIIRS active-fire hotspots for the AOI and season (FIRMS area API).

* Key: ``FIRMS_MAP_KEY`` from ``.env`` (never logged or written to any output).
* Sensors: VIIRS S-NPP and NOAA-20, 375 m. For each sensor the standard-processing archive
  (``*_SP``) is used where FIRMS says it covers the date, otherwise near-real-time (``*_NRT``);
  the product used per day is recorded in the provenance file.
* The area API returns at most 5 days per request, so the season is fetched in 5-day chunks.
  Each raw response is cached under ``data/raw/firms/`` and never downloaded again.
* Output: ``data/processed/firms/viirs_hotspots.gpkg`` (points inside the AOI polygon) with
  ``acq_datetime_utc`` and ``acq_time_ist`` (UTC + 5:30, the overpass time in India).
"""

from __future__ import annotations

import io
import json
import time
from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

from stubble.aoi import build_aoi
from stubble.config import Config, secret
from stubble.provenance import write_provenance

BASE = "https://firms.modaps.eosdis.nasa.gov/api"
MAX_DAYS = 5  # area API limit per request (it was 10 earlier)
SENSORS = {"VIIRS_SNPP": "S-NPP", "VIIRS_NOAA20": "NOAA-20"}


def _get(url: str, retries: int = 3) -> str:
    last: Exception | None = None
    for i in range(retries):
        if i:
            time.sleep(5 * i)
        try:
            r = requests.get(url, timeout=120)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:  # retried; the key is not in the message we log
            last = e
    raise RuntimeError(f"FIRMS request failed: {type(last).__name__}") from None


def availability(key: str) -> pd.DataFrame:
    """FIRMS data availability (min_date / max_date per product)."""
    return pd.read_csv(io.StringIO(_get(f"{BASE}/data_availability/csv/{key}/all")))


def _product_for(avail: pd.DataFrame, sensor: str, day: date) -> str:
    sp = avail[avail.data_id == f"{sensor}_SP"]
    if len(sp) and pd.Timestamp(sp.min_date.iloc[0]) <= pd.Timestamp(day) <= pd.Timestamp(
            sp.max_date.iloc[0]):
        return f"{sensor}_SP"
    return f"{sensor}_NRT"


def fetch_hotspots(cfg: Config) -> Path:
    """Fetch (or reuse cached) VIIRS hotspots for the season; returns the GeoPackage path."""
    key = secret("FIRMS_MAP_KEY")
    if not key:
        raise RuntimeError("FIRMS_MAP_KEY is not set: add it to .env (see .env.example)")
    aoi = build_aoi(cfg).to_crs("EPSG:4326")
    w, s, e, n = (round(v, 4) for v in aoi.total_bounds)
    raw = cfg.path("raw", "firms")
    raw.mkdir(parents=True, exist_ok=True)
    avail = availability(key)
    start, end = cfg.season.start, cfg.season.end
    frames, used = [], []
    for sensor in SENSORS:
        d = start
        while d <= end:
            n_days = min(MAX_DAYS, (end - d).days + 1)
            prod = _product_for(avail, sensor, d)
            f = raw / f"{prod}_{d:%Y%m%d}_{n_days}d_{w}_{s}_{e}_{n}.csv"
            if not f.exists():
                txt = _get(f"{BASE}/area/csv/{key}/{prod}/{w},{s},{e},{n}/{n_days}/{d:%Y-%m-%d}")
                if not txt.startswith("latitude"):
                    raise RuntimeError(f"FIRMS returned no CSV for {prod} {d}: {txt[:120]!r}")
                f.write_text(txt, encoding="utf-8")
            df = pd.read_csv(f)
            df["product"], df["sensor"] = prod, SENSORS[sensor]
            frames.append(df)
            used.append({"product": prod, "start": d.isoformat(), "days": n_days,
                         "rows": len(df), "file": f.name})
            d += timedelta(days=n_days)
    df = pd.concat(frames, ignore_index=True)
    df["acq_date"] = pd.to_datetime(df.acq_date)
    hhmm = df.acq_time.astype(int).astype(str).str.zfill(4)
    df["acq_datetime_utc"] = df.acq_date + pd.to_timedelta(
        hhmm.str[:2].astype(int) * 60 + hhmm.str[2:].astype(int), unit="min")
    ist = df.acq_datetime_utc + pd.Timedelta(hours=5, minutes=30)
    df["acq_date_ist"] = ist.dt.date.astype(str)
    df["acq_time_ist"] = ist.dt.strftime("%H:%M")
    df = df[(df.acq_date.dt.date >= start) & (df.acq_date.dt.date <= end)]
    g = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.longitude, df.latitude),
                         crs="EPSG:4326")
    g = g[g.within(aoi.union_all())].drop_duplicates(
        ["latitude", "longitude", "acq_date", "acq_time", "sensor"]).reset_index(drop=True)
    out = cfg.path("processed", "firms", "viirs_hotspots.gpkg")
    out.parent.mkdir(parents=True, exist_ok=True)
    g["acq_date"] = g.acq_date.dt.strftime("%Y-%m-%d")
    g["acq_datetime_utc"] = g.acq_datetime_utc.dt.strftime("%Y-%m-%dT%H:%M")
    g.to_file(out, driver="GPKG")
    write_provenance(out, step="firms_viirs", params={
        "bbox_wsen": [w, s, e, n], "season": [start.isoformat(), end.isoformat()],
        "sensors": list(SENSORS.values()), "requests": used,
        "source": "NASA FIRMS area API (VIIRS 375 m active fire)"})
    (out.parent / "viirs_requests.json").write_text(json.dumps(used, indent=1), "utf-8")
    return out
