"""Unit tests for M2 baseline detection (synthetic arrays, no network or data files)."""

from datetime import date

import numpy as np
import pytest

from stubble.data.composites import NODATA
from stubble.detect.indices import dnbr, haze_flag, nbr, near_class
from stubble.detect.polygons import clean_mask, label_by_event, min_patch_px
from stubble.detect.timeseries import SeriesSpec, scan_pixels


def o(m: int, d: int) -> int:
    return date(2025, m, d).toordinal()


# ---------------------------------------------------------------------------- indices

def test_nbr_values_and_nodata():
    b8 = np.array([3000, 1000, NODATA, 0], dtype=np.int16)
    b12 = np.array([1000, 3000, 1000, 0], dtype=np.int16)
    out = nbr(b8, b12)
    np.testing.assert_allclose(out[:2], [0.5, -0.5])
    assert np.isnan(out[2]), "nodata band -> NaN"
    assert np.isnan(out[3]), "zero denominator -> NaN"


def test_dnbr_positive_for_drop():
    assert dnbr(np.float32(0.6), np.float32(-0.1)) == pytest.approx(0.7)


def test_near_class_radius():
    scl = np.full((7, 7), 4)
    scl[3, 3] = 9
    m = near_class(scl, [8, 9, 10], 1)
    assert m[2:5, 2:5].all() and m.sum() == 9


def test_haze_flag_blue_and_cloud_proximity():
    b02 = np.array([[500, 1600, 500, 500]], dtype=np.int16)
    scl = np.array([[4, 4, 4, 8]])
    hz = haze_flag(b02, scl, blue_threshold=0.15, scale=10000, cloud_classes=[8, 9, 10],
                   radius_px=1)
    assert hz.tolist() == [[False, True, True, True]]


# ---------------------------------------------------------------------------- time series

def _spec(days: list[int]) -> SeriesSpec:
    d = np.array(days)
    return SeriesSpec(days=d, pre=d <= o(10, 5), post=d >= o(10, 6),
                      weeks=[("w01", o(10, 6), o(10, 12)), ("w02", o(10, 13), o(10, 19))],
                      season_start=o(10, 1), season_end=o(10, 19))


def _series(vals, clear=None, hazy=None):
    v = np.array(vals, dtype=np.float32)[:, None]
    c = np.isfinite(v) if clear is None else np.array(clear)[:, None]
    h = np.zeros_like(c) if hazy is None else np.array(hazy)[:, None]
    return v, c, h


DAYS = [o(9, 25), o(10, 1), o(10, 6), o(10, 9), o(10, 12), o(10, 15), o(10, 18)]


def test_window_is_last_clear_before_to_first_post_burn():
    v, c, h = _series([0.6, 0.6, 0.55, np.nan, -0.2, -0.2, -0.15])  # cloudy on 10-09
    r = scan_pixels(v, c, h, _spec(DAYS), threshold=0.27, confirm_days=10)
    assert r["burnt_raw"][0]
    assert r["detect_day"][0] == o(10, 12)
    assert r["window_start_day"][0] == o(10, 6), "cloudy 10-09 must widen the window"
    assert r["n_support"][0] == 3  # 10-12, 10-15, 10-18 all exceed
    assert r["dnbr_detect"][0] == pytest.approx(0.8)
    assert r["dnbr_local"][0] == pytest.approx(0.75)


def test_single_observation_support():
    v, c, h = _series([0.6, 0.6, 0.55, 0.5, 0.2, 0.5, 0.5], hazy=[0, 0, 0, 0, 1, 0, 0])
    r = scan_pixels(v, c, h, _spec(DAYS), 0.27, 10)
    assert r["burnt_raw"][0] and r["n_support"][0] == 1
    assert r["n_clear_after"][0] == 2
    assert r["haze_at_detect"][0]


def test_no_burn_below_threshold_and_pre_only_drop_ignored():
    v, c, h = _series([0.6, 0.2, 0.5, 0.5, 0.45, 0.4, 0.4])  # drop only in pre window
    r = scan_pixels(v, c, h, _spec(DAYS), 0.27, 10)
    assert not r["burnt_raw"][0]
    assert r["detect_day"][0] == 0 and np.isnan(r["dnbr_detect"][0])


def test_no_preseason_obs_means_no_detection():
    v, c, h = _series([np.nan, np.nan, -0.2, -0.2, -0.2, -0.2, -0.2])
    r = scan_pixels(v, c, h, _spec(DAYS), 0.27, 10)
    assert not r["burnt_raw"][0] and np.isnan(r["nbr_pre"][0])
    assert r["n_clear_pre"][0] == 0


def test_quality_bands():
    v, c, h = _series([0.6, np.nan, 0.6, np.nan, np.nan, 0.6, np.nan],
                      hazy=[0, 0, 1, 0, 0, 0, 0])
    r = scan_pixels(v, c, h, _spec(DAYS), 0.27, 10)
    assert r["n_clear"][0] == 3 and r["n_clear_pre"][0] == 1 and r["n_clear_post"][0] == 2
    assert r["n_hazy"][0] == 1
    assert r["max_gap_days"][0] == 9          # 10-06 -> 10-15
    assert r["days_since_last_clear"][0] == 4  # 10-15 -> season end 10-19
    assert r["n_clear_w01"][0] == 1 and r["n_clear_w02"][0] == 1


# ---------------------------------------------------------------------------- cleaning

def test_min_patch_px():
    assert min_patch_px(0.1, 10) == 10
    assert min_patch_px(0.25, 10) == 25


def test_opening_removes_thin_lines():
    m = np.zeros((10, 10), bool)
    m[1, :] = True          # 1-px line
    m[4:9, 2:8] = True      # block
    c = clean_mask(m, 1)
    assert not c[1].any() and c[4:9, 2:8].all()


def test_label_by_event_splits_and_drops_small():
    m = np.zeros((6, 12), bool)
    m[:, :8] = True
    m[0, 10] = True                      # single pixel -> dropped
    ev = np.zeros((6, 12), np.int16)
    ev[:, :4], ev[:, 4:8], ev[0, 10] = 290, 295, 290
    lab = label_by_event(m, ev, min_px=10)
    assert lab.max() == 2
    assert len(np.unique(lab[:, :4])) == 1 and len(np.unique(lab[:, 4:8])) == 1
    assert lab[0, 0] != lab[0, 5] and lab[0, 10] == 0
