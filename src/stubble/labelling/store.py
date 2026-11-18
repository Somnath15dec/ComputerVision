"""Label storage: append-only SQLite (latest row per sample wins) + GeoJSON export.

No personal data is stored: no labeller names, only the label, an optional free-text note,
the dates that were inspected and a UTC timestamp.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd

from stubble.config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    label TEXT NOT NULL,
    note TEXT,
    before_date TEXT,
    after_date TEXT,
    created_utc TEXT NOT NULL,
    code_version TEXT
);
CREATE INDEX IF NOT EXISTS ix_labels_sample ON labels(sample_id);
"""


def db_path(cfg: Config) -> Path:
    return cfg.path("labels", "labels.sqlite")


def connect(cfg: Config) -> sqlite3.Connection:
    p = db_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    return con


def save_label(cfg: Config, sample_id: str, field_id: str, label: str, note: str = "",
               before: str | None = None, after: str | None = None,
               code_version: str | None = None) -> None:
    """Append a label row (re-labelling keeps history; the latest row counts)."""
    if label not in cfg.labelling.classes:
        raise ValueError(f"unknown label {label!r}; allowed: {cfg.labelling.classes}")
    with connect(cfg) as con:
        con.execute(
            "INSERT INTO labels (sample_id, field_id, label, note, before_date, after_date, "
            "created_utc, code_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sample_id, field_id, label, note or None, before, after,
             datetime.now(UTC).isoformat(timespec="seconds"), code_version))


def latest_labels(cfg: Config) -> pd.DataFrame:
    """One row per labelled sample (its most recent label)."""
    with connect(cfg) as con:
        return pd.read_sql_query(
            "SELECT l.* FROM labels l JOIN (SELECT sample_id, MAX(id) AS id FROM labels "
            "GROUP BY sample_id) m ON l.id = m.id ORDER BY l.sample_id", con)


def export_geojson(cfg: Config) -> Path:
    """Write ``data/labels/labels.geojson``: sample geometry + stratum + latest label."""
    smp = gpd.read_file(cfg.path("labels", "m3_sample.geojson"))
    lab = latest_labels(cfg)[["sample_id", "label", "note", "before_date", "after_date",
                              "created_utc"]]
    out = smp.merge(lab, on="sample_id", how="inner")
    p = cfg.path("labels", "labels.geojson")
    if out.empty:
        p.write_text(json.dumps({"type": "FeatureCollection", "features": []}),
                     encoding="utf-8")
    else:
        out.to_file(p, driver="GeoJSON")
    return p
