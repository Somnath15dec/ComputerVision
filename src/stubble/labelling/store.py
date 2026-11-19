"""Label storage: append-only SQLite (latest row per queue entry wins) + GeoJSON export.

A queue entry is ``(sample_id, pass)``; pass 2 is the hidden repeat (see
:mod:`stubble.labelling.blind`). Every change in the app writes a new row immediately
(autosave), so the history is kept and nothing is lost when the app is closed.

No personal data is stored: no labeller names, only the label, the optional burn date, an
optional free-text note, the queue position and a UTC timestamp.

Classes: ``labelling.classes`` (burnt / not_burnt / unsure). Earlier 5-class labels
(``labelling.legacy_classes``: tilled_not_burnt, harvested_not_burnt, wet) are still accepted
and stored as written; reads keep the raw ``label`` and add ``label_3`` (folded).

Burn date: ``burn_first_seen`` is the first strip date on which the labeller sees the burn;
``burn_last_clear_before`` is the strip date just before it, so the burn happened in
``(burn_last_clear_before, burn_first_seen]``. Only kept for ``burnt`` labels.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    code_version TEXT,
    pass INTEGER NOT NULL DEFAULT 1,
    queue_position INTEGER,
    burn_first_seen TEXT,
    burn_last_clear_before TEXT
);
CREATE INDEX IF NOT EXISTS ix_labels_sample ON labels(sample_id);
"""
_ADDED = {"pass": "INTEGER NOT NULL DEFAULT 1", "queue_position": "INTEGER",
          "burn_first_seen": "TEXT", "burn_last_clear_before": "TEXT"}


def db_path(cfg: Config) -> Path:
    return cfg.path("labels", "labels.sqlite")


def connect(cfg: Config) -> sqlite3.Connection:
    p = db_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(labels)")}
    for col, decl in _ADDED.items():  # older stores
        if col not in have:
            con.execute(f"ALTER TABLE labels ADD COLUMN {col} {decl}")
    return con


def save_label(cfg: Config, sample_id: str, field_id: str, label: str, note: str = "",
               before: str | None = None, after: str | None = None,
               code_version: str | None = None, *, pass_no: int = 1,
               queue_position: int | None = None, burn_first_seen: str | None = None,
               burn_last_clear_before: str | None = None) -> None:
    """Append a label row (re-labelling keeps history; the latest row per pass counts)."""
    allowed = [*cfg.labelling.classes, *cfg.labelling.legacy_classes]
    if label not in allowed:
        raise ValueError(f"unknown label {label!r}; allowed: {allowed}")
    if pass_no not in (1, 2):
        raise ValueError("pass_no must be 1 or 2")
    if label != "burnt":
        burn_first_seen = burn_last_clear_before = None
    con = connect(cfg)
    try:
        with con:
            con.execute(
                "INSERT INTO labels (sample_id, field_id, label, note, before_date, after_date, "
                "created_utc, code_version, pass, queue_position, burn_first_seen, "
                "burn_last_clear_before) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sample_id, field_id, label, note or None, before, after,
                 datetime.now(UTC).isoformat(timespec="seconds"), code_version, pass_no,
                 queue_position, burn_first_seen, burn_last_clear_before))
    finally:
        con.close()


def latest_labels(cfg: Config, pass_no: int | None = 1) -> pd.DataFrame:
    """One row per labelled queue entry (its most recent label); ``pass_no=None`` = both."""
    con = connect(cfg)
    try:
        df = pd.read_sql_query(
            "SELECT l.* FROM labels l JOIN (SELECT sample_id, pass, MAX(id) AS id FROM labels "
            "GROUP BY sample_id, pass) m ON l.id = m.id ORDER BY l.sample_id, l.pass", con)
    finally:
        con.close()
    df["label_3"] = df.label.replace(cfg.labelling.legacy_classes)
    return df if pass_no is None else df[df["pass"] == pass_no].reset_index(drop=True)


def label_queue(cfg: Config, sample_ids: list[str]) -> list:
    """The blind labelling queue for the current config (see :mod:`.blind`)."""
    from stubble.labelling.blind import build_queue

    lc = cfg.labelling
    return build_queue(sorted(sample_ids), lc.queue_seed, lc.n_repeats, lc.repeat_min_gap,
                       lc.n_label)


def labels_path(cfg: Config) -> Path:
    return cfg.path("labels", "m3_labels.geojson")


def agreement_path(cfg: Config) -> Path:
    return cfg.path("labels", "m3_repeat_agreement.json")


def repeat_agreement(cfg: Config) -> dict[str, Any]:
    from stubble.labelling.blind import agreement

    lc = cfg.labelling
    res = agreement(latest_labels(cfg, 1), latest_labels(cfg, 2), lc.legacy_classes)
    return {"queue_seed": lc.queue_seed, "n_label": lc.n_label,
            "n_repeats_in_queue": lc.n_repeats, "classes": lc.classes,
            "repeat_min_gap": lc.repeat_min_gap, **res}


def export_geojson(cfg: Config) -> tuple[Path, dict[str, Any]]:
    """Write ``data/labels/m3_labels.geojson`` and ``m3_repeat_agreement.json``.

    One feature per labelled sample item: the original sample geometry (polygon or 100 m
    cell) and design fields (stratum, inclusion weight, ...) + the first-pass label; items that
    were repeated also carry the repeat label (``repeat_label``, ``repeat_burn_first_seen``).
    ``label`` is as stored; ``label_3`` folds legacy labels into the 3 classes.

    Weights: only the first ``n_label`` items of the seeded shuffled order are queued, a simple
    random subsample of the stratified sample. ``labelling_subset`` marks them and
    ``subset_inclusion_weight`` = stratum population / items of that stratum in the subset
    (``inclusion_weight`` is the full-sample weight). Use the subset weight for estimates.
    The label describes the field under the crosshair (``crosshair_lon`` / ``crosshair_lat``,
    the centre of the pixel holding the geometry's representative point;
    ``crosshair_in_geometry`` says whether that pixel centre lies in the geometry).
    Returns (path, agreement).
    """
    smp = gpd.read_file(cfg.path("labels", "m3_sample.geojson"))
    cols = ["sample_id", "label", "label_3", "burn_first_seen", "burn_last_clear_before", "note",
            "queue_position", "created_utc", "code_version"]
    first = latest_labels(cfg, 1)[cols].rename(columns={"code_version": "label_code_version"})
    rep = latest_labels(cfg, 2)[["sample_id", "label", "label_3", "burn_first_seen",
                                 "queue_position"]]
    rep = rep.rename(columns={"label": "repeat_label", "label_3": "repeat_label_3",
                              "burn_first_seen": "repeat_burn_first_seen",
                              "queue_position": "repeat_queue_position"})
    subset = {it.sample_id for it in label_queue(cfg, smp.sample_id.tolist())}
    smp["labelling_subset"] = smp.sample_id.isin(subset)
    n_sub = smp[smp.labelling_subset].groupby("stratum").size()
    smp["subset_inclusion_weight"] = [
        round(pop / n_sub[s], 3) if ins and s in n_sub else None
        for pop, s, ins in zip(smp.stratum_population, smp.stratum, smp.labelling_subset,
                               strict=True)]
    out = smp.merge(first, on="sample_id", how="inner").merge(rep, on="sample_id", how="left")
    if not out.empty:  # what the labeller saw: the crosshair (anchor pixel centre)
        from stubble.labelling.chips import anchor_point

        g = out.geometry.to_crs(cfg.aoi.crs)
        anc = gpd.GeoSeries([anchor_point(cfg, x) for x in g], crs=cfg.aoi.crs)
        ll = anc.to_crs(4326)
        out["crosshair_lon"], out["crosshair_lat"] = ll.x.round(7), ll.y.round(7)
        out["crosshair_in_geometry"] = [bool(x.covers(a)) for x, a in zip(g, anc, strict=True)]
    p = labels_path(cfg)
    if out.empty:
        p.write_text(json.dumps({"type": "FeatureCollection", "features": []}),
                     encoding="utf-8")
    else:
        out.sort_values("sample_id").to_file(p, driver="GeoJSON")
    agr = repeat_agreement(cfg)
    agreement_path(cfg).write_text(json.dumps(agr, indent=1, default=str), encoding="utf-8")
    return p, agr
