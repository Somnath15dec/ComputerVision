"""Review round: re-show fast labels (plus a slower control) and report what changed.

Selection (frozen in ``data/labels/m3_review_<round>.json`` the first time it runs, so later
saves cannot change it):

* the *current* label of every queue entry made in this tool (``display`` set) is put in save
  order (``created_utc``, whole seconds);
* **fast** = saved ``<= review_fast_gap_s`` seconds after the previous save;
* **control** = ``review_n_control`` entries drawn at random (``review_seed``) from the rest
  (the slower labels, including the very first save, which has no previous one);
* both groups are mixed in one seeded random order. The app shows nothing about the group,
  the earlier label, the detection score or the source.

A review label is appended as a new row for the same ``(sample_id, pass)`` with
``review_round`` set, so it becomes the current label and the original stays as history.
Each entry keeps its original display (chart + ΔNBR for first showings, imagery only for the
hidden repeats).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd

from stubble.config import Config
from stubble.labelling.store import latest_labels, repeat_agreement


@dataclass(frozen=True)
class ReviewItem:
    position: int     # 0-based position in the review queue
    sample_id: str
    pass_no: int
    group: str        # "fast" | "control" (never shown in the app)


def manifest_path(cfg: Config) -> Path:
    return cfg.path("labels", f"m3_review_{cfg.labelling.review_round}.json")


def save_gaps(labels: pd.DataFrame) -> pd.DataFrame:
    """Current labels in save order with ``gap_s`` = seconds since the previous save."""
    df = labels[labels.display.notna()].copy()
    df["t"] = pd.to_datetime(df.created_utc, format="ISO8601")
    df = df.sort_values(["t", "id"]).reset_index(drop=True)
    df["gap_s"] = df.t.diff().dt.total_seconds()
    return df


def select_review(cfg: Config, labels: pd.DataFrame) -> dict[str, Any]:
    """Build the review manifest from the current labels (pure; nothing is written)."""
    lc = cfg.labelling
    df = save_gaps(labels)
    fast = df[df.gap_s <= lc.review_fast_gap_s]
    slow = df[~(df.gap_s <= lc.review_fast_gap_s)]
    rng = np.random.default_rng(lc.review_seed)
    k = min(lc.review_n_control, len(slow))
    ctrl = slow.iloc[sorted(rng.choice(len(slow), k, replace=False))] if k else slow.iloc[:0]
    items = ([(r.sample_id, int(r["pass"]), "fast", r.label, r.gap_s)
              for _, r in fast.iterrows()]
             + [(r.sample_id, int(r["pass"]), "control", r.label,
                 None if pd.isna(r.gap_s) else r.gap_s) for _, r in ctrl.iterrows()])
    order = rng.permutation(len(items))
    entries = [{"position": i, "sample_id": items[j][0], "pass": items[j][1],
                "group": items[j][2], "label_before": items[j][3],
                "gap_s": None if items[j][4] is None else float(items[j][4])}
               for i, j in enumerate(order)]
    return {"round": lc.review_round, "created_utc": datetime.now(UTC).isoformat(
                timespec="seconds"),
            "rule": {"fast_gap_s": lc.review_fast_gap_s, "n_control": lc.review_n_control,
                     "seed": lc.review_seed,
                     "gap": "seconds between consecutive saves of the current labels "
                            "(created_utc, whole seconds)"},
            "n_labels_considered": len(df), "n_fast": len(fast), "n_slow_pool": len(slow),
            "n_control": k, "entries": entries}


def freeze_review(cfg: Config) -> tuple[Path, dict[str, Any], bool]:
    """Write the manifest once; later calls return the frozen one. Returns (path, m, new)."""
    p = manifest_path(cfg)
    if p.exists():
        return p, json.loads(p.read_text("utf-8")), False
    m = select_review(cfg, latest_labels(cfg, None))
    p.write_text(json.dumps(m, indent=1), encoding="utf-8")
    return p, m, True


def review_queue(cfg: Config) -> list[ReviewItem]:
    """The frozen review queue (empty if no manifest yet)."""
    p = manifest_path(cfg)
    if not p.exists():
        return []
    m = json.loads(p.read_text("utf-8"))
    return [ReviewItem(e["position"], e["sample_id"], int(e["pass"]), e["group"])
            for e in m["entries"]]


def _ci(k: int, n: int) -> list[float] | None:
    """Wilson 95 % interval for k / n."""
    if n == 0:
        return None
    z, p = 1.96, k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(float(c - h), 3), round(float(c + h), 3)]


def review_report(cfg: Config) -> dict[str, Any]:
    """Changes per group and the final-label summaries (see module docstring).

    Rates are unweighted shares of the labelled items (the design-weighted estimates belong
    to M4); intervals are Wilson 95 %.
    """
    lc = cfg.labelling
    p = manifest_path(cfg)
    if not p.exists():
        raise FileNotFoundError(f"{p} not found; run `stubble review-select` first")
    m = json.loads(p.read_text("utf-8"))
    fold = lc.legacy_classes
    cur = latest_labels(cfg, None)
    cur["key"] = list(zip(cur.sample_id, cur["pass"].astype(int), strict=True))
    rev = cur[cur.review_round == lc.review_round].set_index("key")
    out: dict[str, Any] = {"round": lc.review_round, "groups": {}}
    trans: list[dict[str, Any]] = []
    for g in ("fast", "control"):
        es = [e for e in m["entries"] if e["group"] == g]
        done = [e for e in es if (e["sample_id"], e["pass"]) in rev.index]
        changed = []
        for e in done:
            before = fold.get(e["label_before"], e["label_before"])
            after = rev.loc[[(e["sample_id"], e["pass"])], "label_3"].iloc[0]
            if before != after:
                changed.append((before, after))
                trans.append({"group": g, "sample_id": e["sample_id"], "pass": e["pass"],
                              "before": before, "after": after})
        cnt = pd.Series([f"{a}->{b}" for a, b in changed], dtype=object).value_counts()
        out["groups"][g] = {"n": len(es), "reviewed": len(done), "changed": len(changed),
                            "changed_share": round(len(changed) / len(done), 3)
                            if done else None,
                            "changed_ci95": _ci(len(changed), len(done)),
                            "transitions": {k: int(v) for k, v in cnt.items()}}
    out["changes"] = trans

    smp = gpd.read_file(cfg.path("labels", "m3_sample.geojson"), ignore_geometry=True)
    first = cur[(cur["pass"] == 1) & cur.display.notna()].merge(
        smp[["sample_id", "source", "sample_week"]], on="sample_id")
    out["final_counts_first_pass"] = {k: int(v) for k, v in
                                      first.label_3.value_counts().items()}
    first["burnt"] = first.label_3 == "burnt"

    def rates(by: str) -> dict[str, Any]:
        res = {}
        for k, x in first.groupby(by):
            b, n = int(x.burnt.sum()), len(x)
            res[str(k)] = {"burnt": b, "n": n, "rate": round(b / n, 3), "ci95": _ci(b, n)}
        return res

    out["burnt_rate_by_source"] = rates("source")
    out["burnt_rate_by_week"] = rates("sample_week")
    out["repeat_agreement"] = repeat_agreement(cfg)
    return out
