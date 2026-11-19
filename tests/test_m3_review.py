"""Review round: frozen selection of fast labels + control, appended review rows, report."""

import json
from collections import Counter

import pandas as pd

from stubble.config import load_config
from stubble.labelling import review, store


def _labels(gaps):
    """Current-label frame with saves separated by ``gaps`` seconds."""
    t = pd.Timestamp("2026-11-19T10:00:00Z")
    rows = []
    for i, g in enumerate([0, *gaps]):
        t = t + pd.Timedelta(seconds=g)
        rows.append({"id": i + 1, "sample_id": f"M3-{i + 1:04d}", "pass": 1,
                     "label": "not_burnt", "display": "aids", "created_utc": t.isoformat()})
    return pd.DataFrame(rows)


def test_select_fast_plus_control_seeded():
    cfg = load_config()
    gaps = [1, 2, 3, 10, 1, 5, 2.5, 0, 7, 9] * 5  # 51 saves
    m = review.select_review(cfg, _labels(gaps))
    n_fast = sum(g <= cfg.labelling.review_fast_gap_s for g in gaps)
    assert m["n_fast"] == n_fast
    assert m["n_control"] == min(cfg.labelling.review_n_control, 51 - n_fast)
    grp = Counter(e["group"] for e in m["entries"])
    assert grp == {"fast": n_fast, "control": m["n_control"]}
    assert len({(e["sample_id"], e["pass"]) for e in m["entries"]}) == len(m["entries"])
    assert m["entries"] == review.select_review(cfg, _labels(gaps))["entries"]  # seeded
    assert [e["group"] for e in m["entries"]] != sorted(e["group"] for e in m["entries"])
    lab = _labels(gaps).set_index("sample_id")
    for e in m["entries"]:  # fast really fast, control really slower (or the first save)
        g = None if e["sample_id"] == "M3-0001" else gaps[int(e["sample_id"][3:]) - 2]
        assert (e["group"] == "fast") == (g is not None and g <= 2)
        assert e["label_before"] == lab.label[e["sample_id"]]


def test_review_rows_appended_and_report(tmp_path, monkeypatch):
    cfg = load_config()
    monkeypatch.setattr(store, "db_path", lambda c: tmp_path / "l.sqlite")
    monkeypatch.setattr(review, "manifest_path", lambda c: tmp_path / "rev.json")
    monkeypatch.setattr(store, "agreement_path", lambda c: tmp_path / "agr.json")
    ids = ["M3-0001", "M3-0002", "M3-0003"]
    for s in ids:
        store.save_label(cfg, s, "F", "not_burnt", display="aids", view_seconds=1.234)
    m = {"round": "r1", "entries": [
        {"position": 0, "sample_id": "M3-0001", "pass": 1, "group": "fast",
         "label_before": "not_burnt", "gap_s": 1.0},
        {"position": 1, "sample_id": "M3-0002", "pass": 1, "group": "control",
         "label_before": "not_burnt", "gap_s": 9.0},
        {"position": 2, "sample_id": "M3-0003", "pass": 1, "group": "fast",
         "label_before": "not_burnt", "gap_s": 0.0}]}
    (tmp_path / "rev.json").write_text(json.dumps(m), encoding="utf-8")
    assert [it.group for it in review.review_queue(cfg)] == ["fast", "control", "fast"]
    store.save_label(cfg, "M3-0001", "F", "burnt", display="aids", review_round="r1")
    store.save_label(cfg, "M3-0002", "F", "not_burnt", display="aids", review_round="r1")
    cur = store.latest_labels(cfg, 1)
    assert cur.set_index("sample_id").label["M3-0001"] == "burnt"   # review row wins
    assert cur.set_index("sample_id").view_seconds["M3-0003"] == 1.23
    import sqlite3
    n = sqlite3.connect(tmp_path / "l.sqlite").execute("select count(*) from labels").fetchone()
    assert n == (5,)                                                 # originals kept
    if not cfg.path("labels", "m3_sample.geojson").exists():
        return
    r = review.review_report(cfg)
    assert r["groups"]["fast"] | {"transitions": None} == {
        "n": 2, "reviewed": 1, "changed": 1, "changed_share": 1.0,
        "changed_ci95": r["groups"]["fast"]["changed_ci95"], "transitions": None}
    assert r["groups"]["fast"]["transitions"] == {"not_burnt->burnt": 1}
    assert r["groups"]["control"]["changed"] == 0
    assert r["final_counts_first_pass"] == {"not_burnt": 2, "burnt": 1}


def test_wilson_ci():
    lo, hi = review._ci(5, 10)
    assert lo < 0.5 < hi and lo >= 0 and hi <= 1
    assert review._ci(0, 0) is None
