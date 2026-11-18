"""Unit tests for the M3 sampler allocation and label store (no data files)."""

import numpy as np
import pandas as pd
import pytest

from stubble.config import load_config
from stubble.labelling.sample import _draw, allocate


def test_allocate_weights_and_total():
    a = allocate(90, {"w01": 1, "w04": 2, "w05": 1, "w06": 1, "w07": 1, "w08": 1, "w02": 1,
                      "w03": 1}, dict.fromkeys(["w01", "w02", "w03", "w04", "w05", "w06",
                                                "w07", "w08"], 1000))
    assert sum(a.values()) == 90 and a["w04"] == 20 and a["w01"] == 10


def test_allocate_redistributes_small_strata():
    a = allocate(64, {"a": 1, "b": 1, "c": 2}, {"a": 3, "b": 100, "c": 100})
    assert a["a"] == 3 and sum(a.values()) == 64 and a["c"] > a["b"]


def test_allocate_capacity_limit():
    a = allocate(50, {"a": 1, "b": 1}, {"a": 5, "b": 10})
    assert a == {"a": 5, "b": 10}


def test_draw_reproducible_and_weights():
    df = pd.DataFrame({"bin": np.repeat(["b1", "b2"], 500),
                       "week": np.tile(["w03", "w04", "w05", "w06"], 250), "v": range(1000)})
    s1, info = _draw(df, ["bin", "week"], 40, {"w04": 2.0}, np.random.default_rng(7))
    s2, _ = _draw(df, ["bin", "week"], 40, {"w04": 2.0}, np.random.default_rng(7))
    assert s1.v.tolist() == s2.v.tolist() and len(s1) == 40
    assert (s1.groupby("bin").size() == 20).all()
    assert info["b1|w04"]["sampled"] == 8 and info["b1|w03"]["sampled"] == 4
    assert s1.inclusion_weight.min() == pytest.approx(125 / 8)


def test_store_rejects_unknown_label(tmp_path, monkeypatch):
    from stubble.labelling import store

    cfg = load_config()
    monkeypatch.setattr(store, "db_path", lambda c: tmp_path / "l.sqlite")
    store.save_label(cfg, "M3-0001", "SGR-0001", "burnt")
    store.save_label(cfg, "M3-0001", "SGR-0001", "wet", note="relabel")
    assert store.latest_labels(cfg).label.tolist() == ["wet"]
    with pytest.raises(ValueError):
        store.save_label(cfg, "M3-0002", "SGR-0002", "fire")
