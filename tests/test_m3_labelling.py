"""Unit tests for the blind M3 queue, repeat agreement and the label store (no rasters)."""

import json
import sqlite3
from collections import Counter

import pytest

from stubble.config import load_config
from stubble.labelling import store
from stubble.labelling.blind import agreement, build_queue, cohen_kappa

IDS = [f"M3-{i:04d}" for i in range(1, 401)]


def test_queue_reproducible_shuffled_with_hidden_repeats():
    q1 = build_queue(IDS, 20251119, 40, 40)
    q2 = build_queue(IDS, 20251119, 40, 40)
    assert q1 == q2 and len(q1) == 440
    assert [it.position for it in q1] == list(range(440))
    firsts = [it.sample_id for it in q1 if it.pass_no == 1]
    assert sorted(firsts) == IDS and firsts != IDS  # every item once, shuffled
    cnt = Counter(it.sample_id for it in q1)
    reps = [s for s, n in cnt.items() if n == 2]
    assert len(reps) == 40 and max(cnt.values()) == 2
    pos = {(it.sample_id, it.pass_no): it.position for it in q1}
    for s in reps:  # repeat after the first showing, with >= 40 first showings between
        between = [it for it in q1[pos[(s, 1)] + 1:pos[(s, 2)]] if it.pass_no == 1]
        assert len(between) >= 40
    assert build_queue(IDS, 1, 40, 40) != q1


def test_kappa_and_agreement():
    assert cohen_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == pytest.approx(1.0)
    assert cohen_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == pytest.approx(0.0)
    import pandas as pd

    f = pd.DataFrame({"sample_id": ["x", "y", "z", "w"],
                      "label": ["burnt", "burnt", "wet", "unsure"],
                      "burn_first_seen": ["2025-11-02", "2025-11-04", None, None]})
    r = pd.DataFrame({"sample_id": ["x", "y", "z", "w", "v"],
                      "label": ["burnt", "burnt", "tilled_not_burnt", "burnt", "wet"],
                      "burn_first_seen": ["2025-11-02", "2025-11-06", None, None, None]})
    a = agreement(f, r)
    assert a["n_pairs"] == 4 and a["agreement"] == 0.5
    assert a["burnt_vs_not"]["n_pairs"] == 3 and a["burnt_vs_not"]["agreement"] == 1.0
    assert a["burn_date"] == {"n_pairs": 2, "same_date": 1}
    assert {d["sample_id"] for d in a["disagreements"]} == {"z", "w"}
    assert agreement(f.iloc[:0], r)["n_pairs"] == 0


def test_store_passes_burn_date_and_export(tmp_path, monkeypatch):
    cfg = load_config()
    monkeypatch.setattr(store, "db_path", lambda c: tmp_path / "l.sqlite")
    monkeypatch.setattr(store, "labels_path", lambda c: tmp_path / "m3_labels.geojson")
    monkeypatch.setattr(store, "agreement_path", lambda c: tmp_path / "agr.json")
    store.save_label(cfg, "M3-0001", "F1", "burnt", pass_no=1, queue_position=3,
                     burn_first_seen="2025-11-04", burn_last_clear_before="2025-11-02")
    store.save_label(cfg, "M3-0001", "F1", "burnt", pass_no=2, queue_position=90,
                     burn_first_seen="2025-11-04")
    store.save_label(cfg, "M3-0002", "F2", "wet", burn_first_seen="2025-11-04")
    first = store.latest_labels(cfg, 1)
    assert first.sample_id.tolist() == ["M3-0001", "M3-0002"]
    assert first.burn_first_seen.isna().tolist() == [False, True]  # burn date dropped for non-burnt
    assert len(store.latest_labels(cfg, None)) == 3
    store.save_label(cfg, "M3-0001", "F1", "tilled_not_burnt", pass_no=2)  # autosave edit
    assert store.latest_labels(cfg, 2).label.tolist() == ["tilled_not_burnt"]
    with pytest.raises(ValueError):
        store.save_label(cfg, "M3-0003", "F3", "burnt", pass_no=3)

    if not cfg.path("labels", "m3_sample.geojson").exists():
        pytest.skip("sample file not present")
    p, agr = store.export_geojson(cfg)
    gj = json.loads(p.read_text(encoding="utf-8"))
    props = {f["properties"]["sample_id"]: f["properties"] for f in gj["features"]}
    assert set(props) == {"M3-0001", "M3-0002"}
    assert props["M3-0001"]["repeat_label"] == "tilled_not_burnt"
    assert props["M3-0001"]["burn_last_clear_before"] == "2025-11-02"
    assert agr["n_pairs"] == 1 and agr["agreement"] == 0.0


def test_store_migrates_old_schema(tmp_path, monkeypatch):
    cfg = load_config()
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE labels (id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id TEXT "
                "NOT NULL, field_id TEXT NOT NULL, label TEXT NOT NULL, note TEXT, before_date "
                "TEXT, after_date TEXT, created_utc TEXT NOT NULL, code_version TEXT)")
    con.execute("INSERT INTO labels (sample_id, field_id, label, created_utc) VALUES "
                "('M3-0001', 'F1', 'wet', '2025-01-01')")
    con.commit()
    con.close()
    monkeypatch.setattr(store, "db_path", lambda c: db)
    assert store.latest_labels(cfg, 1).label.tolist() == ["wet"]


def test_blind_window_identical_and_anchor_inside():
    from shapely.geometry import Polygon, box

    from stubble.labelling.chips import anchor_point, chip_window

    cfg = load_config()
    r, n = cfg.aoi.resolution_m, cfg.labelling.chip_window_px
    # concave L-shaped field (centroid outside) and a grid-aligned 100 m cell
    ell = Polygon([(500000, 3400000), (500300, 3400000), (500300, 3400030), (500030, 3400030),
                   (500030, 3400300), (500000, 3400300)])
    cell = box(510000, 3410000, 510100, 3410100)
    assert not ell.contains(ell.centroid)
    for g in (ell, cell):
        a = anchor_point(cfg, g)
        px = box(a.x - r / 2, a.y - r / 2, a.x + r / 2, a.y + r / 2)
        assert px.covers(g.representative_point())  # anchor pixel holds the rep. point
        assert (a.x / r) % 1 == 0.5 and (a.y / r) % 1 == 0.5  # pixel centre
        cw = chip_window(cfg, g)
        x0, y0, x1, y1 = cw.bounds
        assert (x1 - x0, y1 - y0) == (n * r, n * r)  # same extent for every item
        assert ((x0 + x1) / 2, (y0 + y1) / 2) == (a.x, a.y)  # anchor exactly centred
        assert cw.box.area == pytest.approx(cfg.labelling.crosshair_box_m ** 2)
        assert all(v % r == 0 for v in cw.bounds)  # window edges on pixel edges
    assert ell.covers(anchor_point(cfg, ell))
