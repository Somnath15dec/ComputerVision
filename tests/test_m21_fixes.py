"""Unit tests for the M2.1 fixes: early burns, haze components, PIF offsets, scene rule."""

from datetime import date

import numpy as np
import pytest

from stubble.config import load_config
from stubble.data.composites import NODATA
from stubble.detect.harvest import (
    BURNT,
    NOT_BURNT,
    SAME_WINDOW,
    Thresholds,
    apply_offsets,
    scan_harvest_burn,
)
from stubble.detect.polygons import detection_score
from stubble.detect.radiometry import (
    decide_exclusions,
    haze_components,
    offsets_from_pifs,
    select_pifs,
)

TH = Thresholds(ndvi_green=0.6, min_green_views=1, ndvi_harvest=0.3, dnbr_short_min=0.10,
                albedo_rel_drop_min=0.15, dnbr2_min=0.02, confirm_days=10,
                char_nbr_max=-0.24, char_albedo_max=0.09)
DAYS = np.array([date(2025, 10, d).toordinal() for d in (1, 6, 11, 16, 21, 26)])


def run(ndvi, nbr, nbr2, alb, th=TH):
    a = [np.array(x, np.float32)[:, None] for x in (ndvi, nbr, nbr2, alb)]
    u = np.isfinite(a[0])
    r = scan_harvest_burn(DAYS, *a, u, np.zeros_like(u), th)
    return {k: v[0] for k, v in r.items()}


# green, green, already charred at the first non-green view, stays charred
GREEN_TO_CHAR = dict(ndvi=[0.8, 0.75, 0.10, 0.11, 0.12, 0.14],
                     nbr=[0.6, 0.55, -0.30, -0.28, -0.25, -0.2],
                     nbr2=[0.2, 0.18, 0.02, 0.02, 0.03, 0.03],
                     alb=[0.08, 0.08, 0.06, 0.065, 0.07, 0.075])


def test_green_to_char_is_same_window():
    r = run(**GREEN_TO_CHAR)
    assert r["label"] == SAME_WINDOW
    assert r["burn_prev_day"] == DAYS[1] and r["burn_day"] == DAYS[2]
    assert r["harvest_day"] == DAYS[2]
    assert r["dnbr_short"] == pytest.approx(0.85)        # from the last green view
    assert r["n_support"] >= 2


def test_green_to_char_disabled_without_thresholds():
    th = Thresholds(**{**TH.__dict__, "char_nbr_max": None, "char_albedo_max": None})
    assert run(**GREEN_TO_CHAR, th=th)["label"] == NOT_BURNT


def test_dark_but_not_charred_harvest_view_stays_not_same_window():
    kw = dict(GREEN_TO_CHAR)
    kw["nbr"] = [0.6, 0.55, -0.10, -0.10, -0.10, -0.10]  # dark (wet/tilled) but NBR not low
    assert run(**kw)["label"] == NOT_BURNT


def test_consecutive_burn_unchanged_by_char_rule():
    r = run(ndvi=[0.8, 0.75, 0.25, 0.24, 0.12, 0.13], nbr=[0.6, 0.55, 0.15, 0.14, -0.15, -0.12],
            nbr2=[0.20, 0.18, 0.10, 0.10, 0.04, 0.05], alb=[0.08, 0.08, 0.18, 0.18, 0.08, 0.09])
    assert r["label"] == BURNT and r["burn_day"] == DAYS[4]


def test_apply_offsets_keeps_nodata():
    r = {"B02": np.array([1000, NODATA], np.int16), "SCL": np.array([4, 4], np.int16)}
    out = apply_offsets(r, {"B02": 0.01}, 10000)
    assert out["B02"].tolist() == [900, NODATA] and out["SCL"].tolist() == [4, 4]


def test_haze_components_aot():
    cfg = load_config()
    b02 = np.full((5, 5), 500, np.int16)
    scl = np.full((5, 5), 4, np.int16)
    aot = np.full((5, 5), 200, np.int16)
    aot[0, 0] = 900                      # 0.9 > 0.6
    aot[1, 1] = NODATA                   # missing AOT never flags
    hz = haze_components(b02, scl, aot, cfg, radius_px=1)
    assert hz["aot"].sum() == 1 and hz["aot"][0, 0]
    assert not hz["blue"].any() and not hz["buffer"].any()


def test_pif_selection_and_offsets():
    rng = np.random.default_rng(0)
    f, b, p = 10, 2, 2000
    base = rng.uniform(0.15, 0.3, (1, b, p))
    shift = np.linspace(-0.02, 0.02, f)[:, None, None]
    vals = (base + shift + rng.normal(0, 0.002, (f, b, p))).astype(np.float32)
    ndvi = np.full((f, p), 0.1, np.float32)
    ndvi[:, :100] = 0.7                  # vegetated: never a PIF
    vals[:, :, 100:200] *= np.linspace(0.5, 1.5, f)[:, None, None]  # unstable
    pif = select_pifs(vals, ndvi, min_views=8, ndvi_max=0.3, max_rel_spread=0.15)
    assert not pif[:100].any() and pif[200:].mean() > 0.99 and pif[100:200].mean() < 0.5
    off, spread, n = offsets_from_pifs(vals[:, :, pif], min_px=500)
    assert off[:, 0] == pytest.approx(shift[:, 0, 0], abs=2e-3)
    assert (n > 500).all() and np.nanmax(spread) < 0.01


def test_scene_rule():
    days = ["2025-11-02", "2025-11-04", "2025-11-06"]
    spread = np.array([[0.01, 0.01], [0.05, 0.01], [0.011, 0.012]])
    ex = decide_exclusions(days, {"2025-11-02": 0.9, "2025-11-04": 0.5, "2025-11-06": 0.1},
                           spread, min_frac=0.2, factor=3.0, manual=[])
    assert set(ex) == {"2025-11-04", "2025-11-06"}


def test_detection_score_weakest_link():
    cfg = load_config()
    s = detection_score(np.array([0.2]), np.array([0.45]), np.array([0.03]),
                        cfg.burn_detection)
    assert s[0] == pytest.approx(1.5)    # dNBR2 is the weakest: 0.03 / 0.02
