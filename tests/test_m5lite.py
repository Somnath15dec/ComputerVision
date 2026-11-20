"""M5-lite week assignment (no network)."""

import pandas as pd

from stubble.config import load_config
from stubble.m5lite import week_of, week_table


def test_week_of_season_calendar():
    cfg = load_config()
    d = pd.Series(["2025-09-30", "2025-10-01", "2025-10-05", "2025-10-06", "2025-10-12",
                   "2025-10-13", "2025-11-30", "2025-12-01"])
    w = week_of(d, cfg)
    assert w.isna().tolist() == [True] + [False] * 6 + [True]
    assert w.dropna().tolist() == ["w00", "w00", "w01", "w01", "w02", "w08"]
    wt = week_table(cfg)
    assert wt.week.tolist() == [f"w{k:02d}" for k in range(9)]
    assert str(wt.end.iloc[-1]) == "2025-11-30"
