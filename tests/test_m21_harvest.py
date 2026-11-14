"""Unit tests for M2.1 harvest-aware detection (synthetic series, no data files)."""

from datetime import date

import numpy as np
import pytest

from stubble.data.composites import NODATA
from stubble.detect.harvest import (
    BURNT,
    NO_HARVEST,
    NOT_BURNT,
    UNOBSERVED,
    Thresholds,
    albedo,
    scan_harvest_burn,
)

TH = Thresholds(ndvi_green=0.6, min_green_views=1, ndvi_harvest=0.3, dnbr_short_min=0.10,
                albedo_rel_drop_min=0.15, dnbr2_min=0.02, confirm_days=10)
DAYS = np.array([date(2025, 10, d).toordinal() for d in (1, 6, 11, 16, 21, 26)])


def run(ndvi, nbr, nbr2, alb, usable=None, th=TH):
    a = [np.array(x, np.float32)[:, None] for x in (ndvi, nbr, nbr2, alb)]
    u = np.isfinite(a[0]) if usable is None else np.array(usable, bool)[:, None]
    hz = np.zeros_like(u)
    r = scan_harvest_burn(DAYS, *a, u, hz, th)
    return {k: v[0] for k, v in r.items()}


# green, green, harvested stubble, stubble, BURNT, burnt
GREEN_HARVEST_BURN = dict(
    ndvi=[0.8, 0.75, 0.25, 0.24, 0.12, 0.13],
    nbr=[0.6, 0.55, 0.15, 0.14, -0.15, -0.12],
    nbr2=[0.20, 0.18, 0.10, 0.10, 0.04, 0.05],
    alb=[0.08, 0.08, 0.18, 0.18, 0.08, 0.09],
)


def test_harvest_then_burn():
    r = run(**GREEN_HARVEST_BURN)
    assert r["label"] == BURNT
    assert r["harvest_day"] == DAYS[2] and r["harvest_prev_day"] == DAYS[1]
    assert r["burn_prev_day"] == DAYS[3] and r["burn_day"] == DAYS[4]
    assert r["dnbr_short"] == pytest.approx(0.29)
    assert r["albedo_drop_rel"] == pytest.approx(0.10 / 0.18, rel=1e-4)
    assert r["n_support"] == 2  # burn view + the next view that stays low
    assert r["n_views_post_harvest"] == 3


def test_harvest_drop_alone_is_not_a_burn():
    r = run(ndvi=[0.8, 0.75, 0.25, 0.24, 0.24, 0.23], nbr=[0.6, 0.55, 0.15, 0.14, 0.13, 0.13],
            nbr2=[0.2, 0.18, 0.1, 0.1, 0.1, 0.1], alb=[0.08, 0.08, 0.18, 0.18, 0.18, 0.18])
    assert r["label"] == NOT_BURNT and r["burn_day"] == 0


def test_unobserved_when_no_view_after_harvest():
    nan = np.nan
    r = run(ndvi=[0.8, 0.75, 0.25, nan, nan, nan], nbr=[0.6, 0.55, 0.15, nan, nan, nan],
            nbr2=[0.2, 0.18, 0.1, nan, nan, nan], alb=[0.08, 0.08, 0.18, nan, nan, nan])
    assert r["label"] == UNOBSERVED


def test_no_harvest_without_green_period():
    r = run(ndvi=[0.4, 0.35, 0.25, 0.2, 0.1, 0.1], nbr=[0.2, 0.2, 0.1, 0.1, -0.2, -0.2],
            nbr2=[0.1] * 4 + [0.0, 0.0], alb=[0.15] * 4 + [0.07, 0.07])
    assert r["label"] == NO_HARVEST and r["harvest_day"] == 0


def test_all_three_drops_required():
    kw = dict(GREEN_HARVEST_BURN)
    kw["alb"] = [0.08, 0.08, 0.18, 0.18, 0.17, 0.17]  # no albedo drop (e.g. wet but bright)
    assert run(**kw)["label"] == NOT_BURNT
    kw = dict(GREEN_HARVEST_BURN)
    kw["nbr2"] = [0.2, 0.18, 0.1, 0.1, 0.1, 0.1]       # no NBR2 drop
    assert run(**kw)["label"] == NOT_BURNT


def test_skipped_view_widens_window():
    usable = [1, 1, 1, 1, 0, 1]  # view on day index 4 unusable (e.g. hazy)
    kw = dict(GREEN_HARVEST_BURN)
    kw["ndvi"] = [0.8, 0.75, 0.25, 0.24, 0.12, 0.12]
    kw["nbr"] = [0.6, 0.55, 0.15, 0.14, -0.15, -0.15]
    kw["nbr2"] = [0.2, 0.18, 0.1, 0.1, 0.04, 0.04]
    kw["alb"] = [0.08, 0.08, 0.18, 0.18, 0.08, 0.08]
    r = run(**kw, usable=usable)
    assert r["label"] == BURNT
    assert r["burn_prev_day"] == DAYS[3] and r["burn_day"] == DAYS[5]


def test_albedo_mean_and_nodata():
    b = [np.array([1000, NODATA], np.int16) for _ in range(4)]
    out = albedo(*b)
    assert out[0] == pytest.approx(0.1) and np.isnan(out[1])
