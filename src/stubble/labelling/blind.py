"""Blind labelling queue with hidden repeats, and the labeller's repeat agreement.

The queue shows every sample item once in an order shuffled with ``labelling.queue_seed``.
``n_repeats`` items (seeded draw) are shown a second time, unmarked, at least
``repeat_min_gap`` other items after their first showing. A queue entry is identified by
``(sample_id, pass)``: pass 1 is the first showing, pass 2 the hidden repeat. The app never
shows the sample id, stratum, score or source, so a repeat looks like any other item.

With ``n_items`` set, only the first ``n_items`` of the same shuffled order are queued (the
permutation is drawn first, so it is identical to the full queue's order) and the repeats are
drawn among them.

Agreement is measured on items whose two passes are both labelled: raw agreement, Cohen's
kappa over the classes (earlier 5-class labels folded into 3 with ``fold``),
the same for the binary burnt / not-burnt view (``unsure`` pairs dropped), and the
burn-date match where both passes are ``burnt`` with a date.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class QueueItem:
    """One showing of a sample item in the blind labelling queue."""
    position: int     # 0-based position in the queue
    sample_id: str
    pass_no: int      # 1 = first showing, 2 = hidden repeat


def build_queue(sample_ids: list[str], seed: int, n_repeats: int, min_gap: int,
                n_items: int | None = None) -> list[QueueItem]:
    """Seeded shuffled queue with ``n_repeats`` hidden repeats (see module docstring)."""
    rng = np.random.default_rng(seed)
    base = [sample_ids[i] for i in rng.permutation(len(sample_ids))]
    if n_items is not None:
        base = base[:n_items]
    n = len(base)
    eligible = [p for p in range(n) if p + min_gap + 1 <= n]
    k = min(n_repeats, len(eligible))
    rep_pos = sorted(int(x) for x in rng.choice(eligible, k, replace=False)) if k else []
    # repeat of base[p] is inserted before base[s] (s == n: at the end), s >= p + min_gap + 1,
    # so at least min_gap other first showings lie between the two passes
    slots: dict[int, list[str]] = {}
    for p in rep_pos:
        s = int(rng.integers(p + min_gap + 1, n + 1))
        slots.setdefault(s, []).append(base[p])
    seq: list[tuple[str, int]] = []
    for s in range(n + 1):
        seq += [(sid, 2) for sid in slots.get(s, [])]
        if s < n:
            seq.append((base[s], 1))
    return [QueueItem(i, sid, ps) for i, (sid, ps) in enumerate(seq)]


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    """Cohen's kappa between two label lists; None if empty."""
    if not a:
        return None
    cats = sorted(set(a) | set(b))
    po = float(np.mean([x == y for x, y in zip(a, b, strict=True)]))
    pa = pd.Series(a).value_counts(normalize=True)
    pb = pd.Series(b).value_counts(normalize=True)
    pe = float(sum(pa.get(c, 0.0) * pb.get(c, 0.0) for c in cats))
    if pe >= 1.0:
        return 1.0 if po == 1.0 else None
    return (po - pe) / (1 - pe)


def chart_effect(m: pd.DataFrame) -> dict[str, Any]:
    """Chart-on (pass 1) vs chart-off (pass 2) comparison on labelled pairs.

    ``m`` has ``label_1``, ``label_2`` (folded classes) and ``flagged`` (item is an M2.1
    candidate). Reported: burnt / unsure rates with and without the aids, the discordant
    burnt-vs-not pairs in each direction with an exact two-sided McNemar p, and how often the
    label matches the M2.1 flag (burnt == flagged, unsure dropped) with and without the aids.
    Repeat disagreement mixes labeller noise with the effect of the aids; there is no
    chart-off/chart-off control, so read this as an upper bound on the chart's influence.
    """
    from scipy.stats import binomtest

    out: dict[str, Any] = {"n_pairs": len(m)}
    if m.empty:
        return out

    def rate(s: pd.Series, v: str) -> float:
        return round(float((s == v).mean()), 3)

    out["burnt_rate"] = {"chart_on": rate(m.label_1, "burnt"),
                         "chart_off": rate(m.label_2, "burnt")}
    out["unsure_rate"] = {"chart_on": rate(m.label_1, "unsure"),
                          "chart_off": rate(m.label_2, "unsure")}
    b = m[(m.label_1 != "unsure") & (m.label_2 != "unsure")]
    on_only = int(((b.label_1 == "burnt") & (b.label_2 != "burnt")).sum())
    off_only = int(((b.label_1 != "burnt") & (b.label_2 == "burnt")).sum())
    out["burnt_discordant"] = {
        "n_pairs": len(b), "burnt_only_with_chart": on_only, "burnt_only_without_chart": off_only,
        "mcnemar_exact_p": (round(float(binomtest(on_only, on_only + off_only).pvalue), 3)
                            if on_only + off_only else None)}
    agree = {}
    for k, col in (("chart_on", "label_1"), ("chart_off", "label_2")):
        x = m[m[col] != "unsure"]
        agree[k] = {"n": len(x), "share": round(float(((x[col] == "burnt") == x.flagged).mean()), 3)
                    if len(x) else None}
    out["matches_m21_flag"] = agree
    return out


def agreement(first: pd.DataFrame, repeat: pd.DataFrame, fold: dict[str, str] | None = None
              ) -> dict[str, Any]:
    """Repeat agreement; both frames have ``sample_id``, ``label`` and ``burn_first_seen``.

    ``fold`` maps legacy labels onto the current classes before comparing.
    """
    m = first.merge(repeat, on="sample_id", suffixes=("_1", "_2"))
    if fold:
        m["label_1"] = m.label_1.replace(fold)
        m["label_2"] = m.label_2.replace(fold)
    out: dict[str, Any] = {"n_pairs": len(m)}
    if m.empty:
        return out
    a, b = m.label_1.tolist(), m.label_2.tolist()
    out["agreement"] = round(float(np.mean([x == y for x, y in zip(a, b, strict=True)])), 3)
    k = cohen_kappa(a, b)
    out["kappa"] = None if k is None else round(k, 3)
    bin_ = m[(m.label_1 != "unsure") & (m.label_2 != "unsure")]
    ba, bb = (bin_.label_1 == "burnt").tolist(), (bin_.label_2 == "burnt").tolist()
    kb = cohen_kappa([str(x) for x in ba], [str(x) for x in bb])
    out["burnt_vs_not"] = {
        "n_pairs": len(bin_),
        "agreement": round(float(np.mean([x == y for x, y in zip(ba, bb, strict=True)])), 3)
        if len(bin_) else None,
        "kappa": None if kb is None else round(kb, 3),
    }
    out["n_with_unsure"] = int(((m.label_1 == "unsure") | (m.label_2 == "unsure")).sum())
    both = m[(m.label_1 == "burnt") & (m.label_2 == "burnt")
             & m.burn_first_seen_1.notna() & m.burn_first_seen_2.notna()]
    out["burn_date"] = {"n_pairs": len(both),
                        "same_date": int((both.burn_first_seen_1 == both.burn_first_seen_2)
                                         .sum())}
    out["confusion"] = (pd.crosstab(m.label_1, m.label_2).rename_axis(index="first",
                                                                       columns="repeat")
                        .to_dict(orient="index"))
    out["disagreements"] = m.loc[m.label_1 != m.label_2, ["sample_id", "label_1", "label_2"]
                                 ].rename(columns={"label_1": "first", "label_2": "repeat"}
                                          ).to_dict("records")
    return out
