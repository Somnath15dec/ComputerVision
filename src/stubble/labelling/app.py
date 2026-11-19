"""M3 labelling tool (Streamlit), blind. Run with ``stubble label``.

* Blind: no sample id, detection score, stratum / score bin or flagged / unflagged source is
  shown. Items come in a seeded shuffled order (``labelling.queue_seed``) with
  ``n_repeats`` hidden repeats (:mod:`stubble.labelling.blind`).
* Evidence: a time strip of every usable clear date (true colour above SWIR false colour,
  fixed stretch, polygon outlined; :mod:`stubble.labelling.chips`).
* Keys: 1 burnt, 2 tilled_not_burnt, 3 harvested_not_burnt, 4 wet, 5 unsure (saves and moves
  on); left / right arrows = previous / next item. The burn date (first date the burn is
  visible) is optional and is picked before pressing 1.
* Autosave: every label, burn-date or note change is written to SQLite at once.
"""

from __future__ import annotations

import contextlib
import threading
from datetime import date

import geopandas as gpd
import streamlit as st

from stubble.config import load_config
from stubble.labelling.blind import build_queue
from stubble.labelling.chips import strip
from stubble.labelling.store import export_geojson, latest_labels, save_label
from stubble.provenance import code_version

st.set_page_config(page_title="Stubble labelling (M3)", layout="wide")
cfg = load_config()
lc = cfg.labelling
classes = lc.classes


@st.cache_resource
def load_sample() -> gpd.GeoDataFrame:
    g = gpd.read_file(cfg.path("labels", "m3_sample.geojson")).to_crs(cfg.aoi.crs)
    return g[["sample_id", "field_id", "geometry"]].set_index("sample_id")


@st.cache_resource
def queue() -> list:
    return build_queue(sorted(load_sample().index), lc.queue_seed, lc.n_repeats,
                       lc.repeat_min_gap)


@st.cache_resource
def _prefetch_state() -> dict:
    return {"lock": threading.Lock(), "busy": set()}


def get_strip(sample_id: str) -> tuple[list[date], bytes]:
    return strip(cfg, sample_id, load_sample().geometry[sample_id])


def prefetch(sample_ids: list[str]) -> None:
    """Render upcoming strips into the disk cache on a background thread."""
    stt = _prefetch_state()
    with stt["lock"]:
        todo = [s for s in sample_ids if s not in stt["busy"]]
        stt["busy"].update(todo)
    if not todo:
        return

    def run() -> None:
        try:
            for s in todo:
                with contextlib.suppress(Exception):  # best effort; the page retries
                    get_strip(s)
        finally:
            with stt["lock"]:
                stt["busy"].difference_update(todo)

    threading.Thread(target=run, daemon=True).start()


smp, q = load_sample(), queue()
saved = {(r.sample_id, int(r["pass"])): r for _, r in latest_labels(cfg, None).iterrows()}
n_q = len(q)


def first_unlabelled() -> int:
    for it in q:
        if (it.sample_id, it.pass_no) not in saved:
            return it.position
    return n_q - 1


if "pos" not in st.session_state:
    st.session_state.pos = first_unlabelled()


def go(delta: int) -> None:
    st.session_state.pos = min(max(st.session_state.pos + delta, 0), n_q - 1)


def go_to(pos: int) -> None:
    st.session_state.pos = min(max(pos, 0), n_q - 1)


def _save(pos: int, label: str, days: list[date], advance: bool) -> None:
    it = q[pos]
    bd = st.session_state.get(f"bd_{pos}")
    first = prev = None
    if label == "burnt" and bd is not None:
        k = days.index(bd)
        first, prev = bd.isoformat(), (days[k - 1].isoformat() if k > 0 else None)
    save_label(cfg, it.sample_id, smp.field_id[it.sample_id], label,
               st.session_state.get(f"note_{pos}", ""), code_version=code_version(),
               pass_no=it.pass_no, queue_position=pos, burn_first_seen=first,
               burn_last_clear_before=prev)
    st.toast(f"item {pos + 1}: saved {label}")
    if advance:
        go(1)


def _resave(pos: int, days: list[date]) -> None:
    """Autosave a burn-date / note change on an item that already has a label."""
    cur = saved.get((q[pos].sample_id, q[pos].pass_no))
    if cur is not None:
        _save(pos, cur.label, days, advance=False)


# ---------------------------------------------------------------------------- sidebar
n_done = sum((it.sample_id, it.pass_no) in saved for it in q)
st.sidebar.markdown(f"**{n_done} / {n_q} labelled**")
st.sidebar.progress(n_done / max(n_q, 1))
st.sidebar.button("Next unlabelled", on_click=lambda: go_to(first_unlabelled()),
                  shortcut="n")
st.sidebar.number_input("Go to item", 1, n_q, value=st.session_state.pos + 1,
                        key=f"goto_{st.session_state.pos}",
                        on_change=lambda: go_to(
                            st.session_state[f"goto_{st.session_state.pos}"] - 1))
st.sidebar.markdown(
    "**Keys**  \n" + "  \n".join(f"`{i + 1}` {c}" for i, c in enumerate(classes))
    + "  \n`←` / `→` previous / next  \n`N` next unlabelled")
if st.sidebar.button("Export labels"):
    p, agr = export_geojson(cfg)
    st.sidebar.success(f"wrote {p.name} ({agr['n_pairs']} repeat pairs so far)")

# ---------------------------------------------------------------------------- item
pos = st.session_state.pos
it = q[pos]
cur = saved.get((it.sample_id, it.pass_no))
st.caption("Decision support only: labels describe what the imagery shows, not an offence. "
           "No personal data is collected.")
st.subheader(f"Item {pos + 1} / {n_q}")

days, png = get_strip(it.sample_id)
prefetch([q[k].sample_id for k in range(pos + 1, min(pos + 4, n_q))])
st.image(png, width="stretch")
st.caption("Top: true colour (B4, B3, B2). Bottom: SWIR false colour (B12, B8A, B4). Same "
           "stretch on every date; cyan = unit outline. Only usable clear dates are shown.")

cur_bd = (date.fromisoformat(cur.burn_first_seen)
          if cur is not None and isinstance(cur.burn_first_seen, str) else None)
# pills (buttons), not a selectbox: a focused text input would swallow the 1-5 keys
st.pills("Burn first visible on (optional, for burnt; click again to clear)", days,
         default=cur_bd if cur_bd in days else None,
         format_func=lambda d: d.strftime("%d %b"), key=f"bd_{pos}",
         on_change=_resave, args=(pos, days))
st.text_input("Note (optional, no personal data; click outside the box before using keys)",
              value=(cur.note or "") if cur is not None and isinstance(cur.note, str) else "",
              key=f"note_{pos}", on_change=_resave, args=(pos, days))

cols = st.columns(len(classes))
for i, c in enumerate(classes):
    cols[i].button(f"{i + 1} · {c}", key=f"lab_{c}", width="stretch",
                   type="primary" if cur is not None and cur.label == c else "secondary",
                   shortcut=str(i + 1), on_click=_save, args=(pos, c, days, True))
n1, n2, _ = st.columns([1, 1, 4])
n1.button("← previous", on_click=go, args=(-1,), shortcut="left", disabled=pos == 0)
n2.button("next →", on_click=go, args=(1,), shortcut="right", disabled=pos == n_q - 1)
if cur is not None:
    extra = f", burn first visible {cur.burn_first_seen}" if isinstance(
        cur.burn_first_seen, str) else ""
    st.info(f"Saved: **{cur.label}**{extra}")
