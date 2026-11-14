"""Unit tests for M1 data utilities (no network)."""

import itertools
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pytest

from stubble.config import load_config
from stubble.data.base import SceneRef
from stubble.data.composites import (
    NODATA,
    db_int16_to_linear,
    group_acquisitions,
    linear_to_db_int16,
    s2_dn_to_int16,
    scl_valid,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cfg():
    return load_config(str(ROOT / "config.yaml"))


def test_s2_offset_applied_for_new_baseline():
    dn = np.array([0, 1000, 1500, 11000], dtype=np.uint16)
    out = s2_dn_to_int16(dn, "05.11", -1000)
    assert out.tolist() == [NODATA, 0, 500, 10000]


def test_s2_no_offset_for_old_baseline():
    dn = np.array([0, 1500], dtype=np.uint16)
    assert s2_dn_to_int16(dn, "03.01", -1000).tolist() == [NODATA, 1500]


def test_s2_offset_never_produces_nodata_sentinel():
    dn = np.array([1], dtype=np.uint16)  # 1 - 1000 = -999, valid
    assert s2_dn_to_int16(dn, "05.00", -1000)[0] == -999


def test_scl_mask_keeps_dark_area_class(cfg):
    scl = np.arange(12)
    ok = scl_valid(scl, cfg.sentinel2.scl_invalid)
    assert ok[2], "class 2 (dark area) must stay valid: burn scars land there"
    assert not ok[[0, 1, 3, 8, 9, 10]].any()
    assert ok[[4, 5, 6, 7, 11]].all()


def test_db_roundtrip():
    lin = np.array([0.1, 0.01, 1.0, 0.0, np.nan], dtype=np.float32)
    q = linear_to_db_int16(lin, 100)
    assert q.tolist()[:3] == [-1000, -2000, 0]
    assert q[3] == NODATA and q[4] == NODATA
    back = db_int16_to_linear(q, 100)
    np.testing.assert_allclose(back[:3], lin[:3], rtol=1e-3)
    assert np.isnan(back[3:]).all()


def test_windows(cfg):
    wins = cfg.compositing_windows()
    assert wins[0].id == "pre"
    assert [w.id for w in wins[1:]] == [f"w{i:02d}" for i in range(1, 9)]
    assert wins[1].start == date(2025, 10, 6)
    assert wins[-1].end == date(2025, 11, 30)
    for a, b in itertools.pairwise(wins):
        assert (b.start - a.end).days == 1  # contiguous, non-overlapping


def test_grouping_splits_platform_and_orbit():
    t = datetime(2025, 10, 6, 5, 30, tzinfo=UTC)
    s2 = [SceneRef("a", t, "Sentinel-2B"), SceneRef("b", t, "Sentinel-2B"),
          SceneRef("c", t, "Sentinel-2C")]
    acqs = group_acquisitions("s2", s2)
    assert [a.key for a in acqs] == ["20251006_s2b", "20251006_s2c"]
    assert len(acqs[0].scenes) == 2
    s1 = [SceneRef("x", t, "sentinel-1a", "ascending"),
          SceneRef("y", t, "sentinel-1a", "descending")]
    assert [a.key for a in group_acquisitions("s1", s1)] == ["20251006_asc", "20251006_des"]
