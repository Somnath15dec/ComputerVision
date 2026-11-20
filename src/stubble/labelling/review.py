"""Review rounds: re-show labels blind and report what changed.

Each round's queue is frozen in ``data/labels/m3_review_<round>.json`` the first time it is
built, so later saves cannot change it. Two selection rules (``labelling.review_rule``):

``fast_control`` (r1)
    The *current* label of every queue entry made in this tool (``display`` set) is put in
    save order (``created_utc``, whole seconds). **fast** = saved ``<= review_fast_gap_s``
    seconds after the previous save; **control** = ``review_n_control`` entries drawn at random
    (``review_seed``) from the rest (the slower labels, including the very first save).

``remaining`` (r2 onwards)
    **remaining** = every current label not in an earlier round's manifest; **carryover** =
    earlier-round entries that never got a review row. Then a **dates** pass: entries whose
    current label is ``burnt``, saved in an earlier round, with no burn date. Shown with the
    label visible, so the burn date can be added (or the label changed).

The blind groups are mixed in one seeded random order; the app shows nothing about the
group, the earlier label, the detection score or the source. The dates pass comes after them.

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
from stubble.labelling.store import all_labels, latest_labels, repeat_agreement


@dataclass(frozen=True)
class ReviewItem:
    """One item in a review round's queue, with its review group."""
    position: int     # 0-based position in the review queue
    sample_id: str
    pass_no: int
    group: str        # fast | control | remaining | carryover (never shown) | dates (shown)


BLIND_GROUPS = ("fast", "control", "remaining", "carryover")


def manifest_path(cfg: Config, rnd: str | None = None) -> Path:
    """Path of the frozen review manifest for round ``rnd`` (default: current round)."""
    return cfg.path("labels", f"m3_review_{rnd or cfg.labelling.review_round}.json")


def _entry(i: int, sid: str, pass_no: int, group: str, label: str, gap: Any = None,
           burn_first_seen: Any = None) -> dict[str, Any]:
    e = {"position": i, "sample_id": sid, "pass": int(pass_no), "group": group,
         "label_before": label, "gap_s": None if gap is None or pd.isna(gap) else float(gap)}
    if isinstance(burn_first_seen, str):
        e["burn_first_seen_before"] = burn_first_seen
    return e


def save_gaps(labels: pd.DataFrame) -> pd.DataFrame:
    """Current labels in save order with ``gap_s`` = seconds since the previous save."""
    df = labels[labels.display.notna()].copy()
    df["t"] = pd.to_datetime(df.created_utc, format="ISO8601")
    df = df.sort_values(["t", "id"]).reset_index(drop=True)
    df["gap_s"] = df.t.diff().dt.total_seconds()
    return df


def select_fast_control(cfg: Config, labels: pd.DataFrame) -> dict[str, Any]:
    """The ``fast_control`` manifest from the current labels (pure; nothing is written)."""
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
    entries = [_entry(i, *items[j]) for i, j in enumerate(order)]
    return {"round": lc.review_round, "rule_name": "fast_control",
            "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "rule": {"fast_gap_s": lc.review_fast_gap_s, "n_control": lc.review_n_control,
                     "seed": lc.review_seed,
                     "gap": "seconds between consecutive saves of the current labels "
                            "(created_utc, whole seconds)"},
            "n_labels_considered": len(df), "n_fast": len(fast), "n_slow_pool": len(slow),
            "n_control": k, "entries": entries}


def prior_rounds(cfg: Config) -> list[dict[str, Any]]:
    """Manifests of the rounds before the current one (by file name order)."""
    cur = manifest_path(cfg).name
    return [json.loads(p.read_text("utf-8"))
            for p in sorted(cfg.path("labels").glob("m3_review_*.json"))
            if not p.name.endswith("_report.json") and p.name < cur]


def select_remaining(cfg: Config, labels: pd.DataFrame, rows: pd.DataFrame,
                     prior: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``remaining`` manifest (pure): ``labels`` = current labels, ``rows`` = full
    history, ``prior`` = earlier manifests."""
    lc = cfg.labelling
    cur = labels[labels.display.notna()].sort_values(["sample_id", "pass"])
    in_prior = {(e["sample_id"], int(e["pass"])) for m in prior for e in m["entries"]}
    prior_names = {m["round"] for m in prior}
    rr = rows[rows.review_round.isin(prior_names)]
    reviewed = set(zip(rr.sample_id, rr["pass"].astype(int), strict=True))
    blind, dates = [], []
    for _, r in cur.iterrows():
        k = (r.sample_id, int(r["pass"]))
        item = (r.sample_id, k[1], r.label, r.burn_first_seen)
        if k not in in_prior:
            blind.append(("remaining", item))
        elif k not in reviewed:
            blind.append(("carryover", item))
        elif (r.label_3 == "burnt" and r.review_round in prior_names
              and not isinstance(r.burn_first_seen, str)):
            dates.append(("dates", item))
    rng = np.random.default_rng(lc.review_seed)
    ob, od = rng.permutation(len(blind)), rng.permutation(len(dates))
    seq = [blind[j] for j in ob] + [dates[j] for j in od]
    entries = [_entry(i, sid, p, g, lab, None, bfs)
               for i, (g, (sid, p, lab, bfs)) in enumerate(seq)]
    n = {g: sum(e["group"] == g for e in entries) for g in ("remaining", "carryover", "dates")}
    return {"round": lc.review_round, "rule_name": "remaining",
            "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "rule": {"seed": lc.review_seed, "prior_rounds": sorted(prior_names),
                     "remaining": "current labels not in an earlier round's manifest",
                     "carryover": "earlier-round entries with no review row",
                     "dates": "current label burnt, saved in an earlier round, no burn date; "
                              "shown after the blind items, label visible"},
            "n_labels_considered": len(cur), "n_remaining": n["remaining"],
            "n_carryover": n["carryover"], "n_dates": n["dates"], "entries": entries}


def select_review(cfg: Config) -> dict[str, Any]:
    """The manifest for the current round under ``labelling.review_rule``."""
    cur = latest_labels(cfg, None)
    if cfg.labelling.review_rule == "fast_control":
        return select_fast_control(cfg, cur)
    if cfg.labelling.review_rule == "remaining":
        return select_remaining(cfg, cur, all_labels(cfg), prior_rounds(cfg))
    raise ValueError(f"unknown review_rule {cfg.labelling.review_rule!r}")


def freeze_review(cfg: Config) -> tuple[Path, dict[str, Any], bool]:
    """Write the manifest once; later calls return the frozen one. Returns (path, m, new)."""
    p = manifest_path(cfg)
    if p.exists():
        return p, json.loads(p.read_text("utf-8")), False
    m = select_review(cfg)
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


def round_rows(cfg: Config, rnd: str) -> pd.DataFrame:
    """Latest row of round ``rnd`` per ``(sample_id, pass)``, indexed by that key (stable
    even after a later round supersedes it)."""
    rows = all_labels(cfg)
    rows = rows[rows.review_round == rnd].drop_duplicates(["sample_id", "pass"], keep="last")
    rows.index = pd.Index(list(zip(rows.sample_id, rows["pass"].astype(int), strict=True)))
    return rows


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
    rev = round_rows(cfg, m["round"])
    out: dict[str, Any] = {"round": m["round"], "groups": {}}
    trans: list[dict[str, Any]] = []
    for g in dict.fromkeys(e["group"] for e in m["entries"]):
        es = [e for e in m["entries"] if e["group"] == g]
        done = [e for e in es if (e["sample_id"], e["pass"]) in rev.index]
        changed, dated = [], 0
        for e in done:
            before = fold.get(e["label_before"], e["label_before"])
            r = rev.loc[[(e["sample_id"], e["pass"])]].iloc[0]
            after = r.label_3
            dated += after == "burnt" and isinstance(r.burn_first_seen, str)
            if before != after:
                changed.append((before, after))
                trans.append({"group": g, "sample_id": e["sample_id"], "pass": e["pass"],
                              "before": before, "after": after})
        cnt = pd.Series([f"{a}->{b}" for a, b in changed], dtype=object).value_counts()
        out["groups"][g] = {"n": len(es), "reviewed": len(done), "changed": len(changed),
                            "changed_share": round(len(changed) / len(done), 3)
                            if done else None,
                            "changed_ci95": _ci(len(changed), len(done)),
                            "transitions": {k: int(v) for k, v in cnt.items()},
                            "burnt_with_date": int(dated)}
    out["changes"] = trans

    cur = latest_labels(cfg, None)
    smp = gpd.read_file(cfg.path("labels", "m3_sample.geojson"), ignore_geometry=True)
    first = cur[(cur["pass"] == 1) & cur.display.notna()].merge(
        smp[["sample_id", "source", "sample_week"]], on="sample_id")
    out["final_counts_first_pass"] = {k: int(v) for k, v in
                                      first.label_3.value_counts().items()}
    first["burnt"] = first.label_3 == "burnt"
    bt = cur[(cur.label_3 == "burnt") & cur.display.notna()]
    out["burnt_with_date"] = {"burnt": len(bt),
                              "with_date": int(bt.burn_first_seen.notna().sum())}

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
