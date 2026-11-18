"""M3 labelling tool (Streamlit). Run with ``stubble label``.

Shows the evidence chips for each sample item in the seeded queue order and stores one of
the configured classes per item in ``data/labels/labels.sqlite``. Detector information
(source, score, stratum) is hidden by default so it does not anchor the label.
"""

from __future__ import annotations

import io
from datetime import date

import geopandas as gpd
import matplotlib.pyplot as plt
import streamlit as st
from shapely import wkb

from stubble.config import load_config
from stubble.detect.timeseries import _s2_days
from stubble.labelling.chips import chip_window, render, snap_day, time_series
from stubble.labelling.store import export_geojson, latest_labels, save_label
from stubble.provenance import code_version

st.set_page_config(page_title="Stubble labelling (M3)", layout="wide")
cfg = load_config()


@st.cache_data
def load_sample() -> gpd.GeoDataFrame:
    g = gpd.read_file(cfg.path("labels", "m3_sample.geojson")).to_crs(cfg.aoi.crs)
    return g.sort_values("queue_index").reset_index(drop=True)


@st.cache_data
def s2_days() -> list[date]:
    return list(_s2_days(cfg))


@st.cache_data(show_spinner="reading time series ...")
def series(sample_id: str, geom_wkb: bytes) -> list[dict]:
    return time_series(cfg, chip_window(cfg, wkb.loads(geom_wkb)))


@st.cache_data(show_spinner="rendering chips ...")
def figure_png(sample_id: str, geom_wkb: bytes, before: date, after: date,
               orbit: str | None) -> bytes:
    fig = render(cfg, wkb.loads(geom_wkb), before, after, orbit,
                 ts=series(sample_id, geom_wkb))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    return buf.getvalue()


smp = load_sample()
days = s2_days()
labels = latest_labels(cfg).set_index("sample_id")
ids = smp.sample_id.tolist()
if "i" not in st.session_state:
    todo = [k for k, s in enumerate(ids) if s not in labels.index]
    st.session_state.i = todo[0] if todo else 0

st.sidebar.markdown(f"**{len(labels)} / {len(ids)} labelled**")
st.sidebar.progress(len(labels) / max(len(ids), 1))
if st.sidebar.button("Next unlabelled"):
    todo = [k for k, s in enumerate(ids) if s not in labels.index]
    if todo:
        st.session_state.i = todo[0]
pick = st.sidebar.selectbox("Go to", ids, index=st.session_state.i)
if ids.index(pick) != st.session_state.i:
    st.session_state.i = ids.index(pick)
show_det = st.sidebar.toggle("Show detector information", value=False,
                             help="Hidden by default so the detector does not anchor the label.")
if st.sidebar.button("Export labels.geojson"):
    st.sidebar.success(f"wrote {export_geojson(cfg).name}")

row = smp.iloc[st.session_state.i]
st.caption("Decision support only: labels describe what the imagery shows, not an offence. "
           "No personal data is collected.")
st.subheader(f"{row.sample_id}  ({st.session_state.i + 1} / {len(ids)})")
if show_det:
    st.json({k: (None if (isinstance(v, float) and v != v) else v)
             for k, v in row.drop(labels="geometry").to_dict().items()})

d_before = snap_day(days, date.fromisoformat(str(row.default_before)[:10]), "before")
d_after = snap_day(days, date.fromisoformat(str(row.default_after)[:10]), "after")
c1, c2 = st.columns(2)
before = c1.selectbox("Before view", days, index=days.index(d_before),
                      format_func=lambda d: d.strftime("%d %b"), key=f"b_{row.sample_id}")
after = c2.selectbox("After view", days, index=days.index(d_after),
                     format_func=lambda d: d.strftime("%d %b"), key=f"a_{row.sample_id}")
orbit = row.get("s1_orbit") if isinstance(row.get("s1_orbit"), str) and row.get("s1_orbit") \
    else None
st.image(figure_png(row.sample_id, row.geometry.wkb, before, after, orbit),
         width="stretch")

cur = labels.label.get(row.sample_id) if row.sample_id in labels.index else None
classes = cfg.labelling.classes
with st.form(f"f_{row.sample_id}"):
    lab = st.radio("Label", classes, horizontal=True,
                   index=classes.index(cur) if cur in classes else None)
    note = st.text_input("Note (optional, no personal data)",
                         value=labels.note.get(row.sample_id) or "" if cur else "")
    if st.form_submit_button("Save and next", type="primary"):
        if lab is None:
            st.warning("Pick a label first.")
        else:
            save_label(cfg, row.sample_id, row.field_id, lab, note, before.isoformat(),
                       after.isoformat(), code_version())
            st.session_state.i = min(st.session_state.i + 1, len(ids) - 1)
            st.rerun()
if cur:
    st.info(f"Current label: **{cur}**")
