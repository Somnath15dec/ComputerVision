"""Stubble Burn-Area Audit, Sangrur 2025: public dashboard (research prototype).

Reads only ``deploy/data/`` (precomputed by ``stubble deploy-data``). Weeks are always the
season weeks of each polygon's first clear burnt view (``detect_date``): w00 = 1-5 Oct, then
7-day weeks from 6 Oct (w08 = 24-30 Nov).
"""

from __future__ import annotations

import json
from pathlib import Path

import altair as alt
import pandas as pd
import pydeck as pdk
import streamlit as st

DATA = Path(__file__).resolve().parent.parent / "deploy" / "data"
AREA = "M2.1 candidate burnt area (unvalidated estimate)"
DISCLAIMER = ("**Research prototype.** Burnt areas are unvalidated satellite estimates for "
              "decision support only. They are not evidence that any person or field burned "
              "residue, and must not be used for enforcement, fines or contacting anyone. "
              "Every candidate has status `pending_field_verification`. No personal data.")
WEEK_COLORS = {  # w00..w08, light to dark (sequential, colour-blind safe)
    "w00": [255, 237, 160], "w01": [254, 217, 118], "w02": [254, 178, 76],
    "w03": [253, 141, 60], "w04": [252, 78, 42], "w05": [227, 26, 28], "w06": [189, 0, 38],
    "w07": [128, 0, 38], "w08": [77, 0, 75]}

st.set_page_config(page_title="Stubble burn-area audit: Sangrur 2025", layout="wide",
                   page_icon=":fire:")


@st.cache_data
def load_json(name: str) -> dict:
    """Read a JSON file from deploy/data (cached)."""
    return json.loads((DATA / name).read_text("utf-8"))


@st.cache_data
def load_csv(name: str) -> pd.DataFrame:
    """Read a CSV file from deploy/data (cached)."""
    return pd.read_csv(DATA / name)


summary = load_json("summary.json")
m5 = summary["m5lite"]
weekly = load_csv("weekly.csv")
weeks = weekly.week.tolist()
week_label = {r.week: f"{r.week} ({pd.Timestamp(r.start):%d %b}-{pd.Timestamp(r.end):%d %b})"
              for r in weekly.itertuples()}

page = st.sidebar.radio("Page", ["Map", "Weekly comparison", "Method & limitations"])
st.sidebar.caption("Sangrur district, Punjab · 1 Oct - 30 Nov 2025 · Sentinel-2 / Sentinel-1 "
                   "burnt-area estimate vs NASA FIRMS VIIRS hotspots.")


# ------------------------------------------------------------------------------------ map
def map_page() -> None:
    """Map page: week slider, metrics, pydeck layers and the per-cell table."""
    st.title("Stubble burn-area audit: Sangrur 2025")
    opts = ["All season", *weeks]
    sel = st.select_slider("Week (week of first clear burnt view / hotspot date, IST)", opts,
                           value="All season",
                           format_func=lambda w: week_label.get(w, w))
    c1, c2, c3, c4 = st.columns(4)
    show_poly = c1.checkbox("Candidate polygons (largest)", True)
    show_pts = c2.checkbox("All candidates (points)", False)
    show_fire = c3.checkbox("VIIRS hotspots", True)
    show_lab = c4.checkbox("Labelled sample (200)", False)
    wsel = weeks if sel == "All season" else [sel]

    ha = float(weekly[weekly.week.isin(wsel)].burnt_ha.sum())
    hs = int(weekly[weekly.week.isin(wsel)].hotspots.sum())
    k1, k2, k3 = st.columns(3)
    k1.metric(AREA, f"{ha:,.0f} ha")
    k2.metric("VIIRS hotspot detections (S-NPP + NOAA-20)", f"{hs:,}")
    views = pd.Series([f["properties"]["mean_clear_views"] for f in
                       load_json("grid_week_summary.geojson")["features"]]).dropna()
    k3.metric("Clear Sentinel-2 views (median per cell)", f"{views.median():.1f}",
              help="Season-wide, not per week: per 5 km cell, the mean number of clear "
                   "Sentinel-2 views of its M2.1 candidate polygons; median over cells. Fewer "
                   "views mean wider burn-date windows and less certain estimates.")

    layers = [pdk.Layer("GeoJsonLayer", load_json("aoi.geojson"), stroked=True, filled=False,
                        get_line_color=[60, 60, 60], line_width_min_pixels=1.5)]
    if show_poly:
        poly = load_json("m21_polygons_top.geojson")
        feats = [f for f in poly["features"] if f["properties"]["week"] in wsel]
        for f in feats:
            f["properties"]["color"] = WEEK_COLORS[f["properties"]["week"]]
        layers.append(pdk.Layer(
            "GeoJsonLayer", {"type": "FeatureCollection", "features": feats}, pickable=True,
            stroked=False, filled=True, get_fill_color="properties.color", opacity=0.75))
    if show_pts:
        p = load_csv("m21_centroids.csv")
        p = p[p.week.isin(wsel)].copy()
        p["color"] = p.week.map(WEEK_COLORS)
        layers.append(pdk.Layer("ScatterplotLayer", p, get_position=["lon", "lat"],
                                get_fill_color="color", get_radius=40, radius_min_pixels=1,
                                opacity=0.6))
    if show_fire:
        fire = load_json("firms_hotspots.geojson")
        ff = [f for f in fire["features"] if f["properties"].get("week") in wsel]
        layers.append(pdk.Layer(
            "GeoJsonLayer", {"type": "FeatureCollection", "features": ff}, pickable=True,
            get_fill_color=[30, 136, 229, 230], get_line_color=[255, 255, 255],
            point_radius_min_pixels=4, get_point_radius=180, line_width_min_pixels=1))
    if show_lab:
        lab = load_json("labelled_sample.geojson")
        cmap = {"burnt": [0, 0, 0], "not_burnt": [0, 158, 115], "unsure": [150, 150, 150]}
        for f in lab["features"]:
            f["properties"]["color"] = cmap.get(f["properties"]["label"], [150, 150, 150])
        layers.append(pdk.Layer("GeoJsonLayer", lab, pickable=True,
                                get_fill_color="properties.color", point_radius_min_pixels=5,
                                get_point_radius=150, get_line_color=[255, 255, 255],
                                line_width_min_pixels=1))
    tip = {"html": "<b>{field_id}{sample_id}</b> {week}<br/>area {area_ha} ha · detected "
                   "{detected_window}<br/>{status}<br/>hotspot {acq_date_ist} {acq_time_ist} "
                   "{sensor}<br/>label {label} {burn_first_seen}",
           "style": {"fontSize": "12px"}}
    st.pydeck_chart(pdk.Deck(layers=layers, map_style=None, tooltip=tip,
                             initial_view_state=pdk.ViewState(latitude=30.14, longitude=75.88,
                                                              zoom=9.2)),
                    height=620)
    pm = summary["polygons"]
    st.caption(
        f"Polygons: {AREA}, coloured by week (light = early October, dark = late November). "
        f"For speed the map draws the {pm['n_shown']:,} largest polygons (>= "
        f"{pm['min_area_ha_shown']} ha, {pm['ha_shown']:,.0f} ha, simplified 10 m); the points "
        f"layer shows all {pm['n_total_in_season']:,} in-season candidates. Blue dots: VIIRS "
        "375 m active-fire detections. Labelled sample: black = labelled burnt, green = not "
        "burnt, grey = unsure (one labeller; see Method & limitations).")
    st.markdown("**Colour key (week):** " + " ".join(
        f"<span style='background:rgb({c[0]},{c[1]},{c[2]});padding:1px 6px;border-radius:3px;"
        f"color:{'#000' if i < 5 else '#fff'}'>{w}</span>"
        for i, (w, c) in enumerate(WEEK_COLORS.items())), unsafe_allow_html=True)

    st.subheader("Per 5 km cell")
    grid = load_json("grid_week_summary.geojson")
    rows = []
    for f in grid["features"]:
        p = f["properties"]
        rows.append({"cell": p["grid_cell"], "region": p["region"],
                     "area_ha": round(sum(p[f"ha_{w}"] for w in wsel), 1),
                     "hotspots": int(sum(p[f"hs_{w}"] for w in wsel)),
                     "mean clear S2 views": p["mean_clear_views"]})
    df = pd.DataFrame(rows).sort_values("area_ha", ascending=False)
    df = df.rename(columns={"area_ha": f"{AREA}, ha"})
    st.dataframe(df, hide_index=True, width="stretch", height=300)


# --------------------------------------------------------------------------------- weekly
def weekly_page() -> None:
    """Weekly comparison page: area vs hotspot chart, IST-hour chart, grid maps."""
    st.title("Our burnt-area estimate vs FIRMS hotspots, per week")
    w = weekly.copy()
    w["label"] = w.week.map(week_label)
    base = alt.Chart(w).encode(x=alt.X("label:N", sort=None, title="week (season calendar)"))
    bars = base.mark_bar(color="#c0392b", opacity=0.85).encode(
        y=alt.Y("burnt_ha:Q", title=f"{AREA}, ha"),
        tooltip=["label", alt.Tooltip("burnt_ha:Q", format=",.0f"), "hotspots"])
    line = base.mark_line(color="#1e88e5", point=True, strokeWidth=2.5).encode(
        y=alt.Y("hotspots:Q", title="VIIRS hotspot detections (blue line)"))
    st.altair_chart(alt.layer(bars, line).resolve_scale(y="independent").properties(height=380),
                    width="stretch")
    st.caption(f"Red bars: {AREA}, by week of the first clear Sentinel-2 view showing the burn "
               "(the burn happened up to the burn-date window earlier). Blue line: NASA FIRMS "
               "VIIRS S-NPP + NOAA-20 detections in the district, by IST date. Hotspots peak in "
               "w04, the area estimate in w05: the 28 Oct scene was 1.4 % clear and the 30 Oct "
               "scene was excluded, so late-October burns are first seen in November.")
    st.dataframe(w[["week", "start", "end", "burnt_ha", "hotspots"]].rename(
        columns={"burnt_ha": f"{AREA}, ha"}), hide_index=True)

    st.subheader("When does VIIRS look? Hotspots by overpass hour (IST)")
    hrs = m5["hotspot_ist_hour_counts"]
    h = pd.DataFrame({"hour": list(range(24))})
    h["hotspots"] = h.hour.map(lambda x: hrs.get(str(x), 0))
    st.altair_chart(alt.Chart(h).mark_bar(color="#2c3e50").encode(
        x=alt.X("hour:O", title="acquisition hour (IST)"),
        y=alt.Y("hotspots:Q", title="VIIRS hotspots")).properties(height=240), width="stretch")
    n_mid = sum(v for k, v in hrs.items() if 12 <= int(k) <= 14)
    st.caption(
        f"{n_mid} of {m5['season_hotspots']} detections were acquired 12:00-14:59 IST, the rest "
        "at night (01:00-02:59). A study by ISRO's Space Applications Centre (SAC), published in "
        "Current Science, reports that peak stubble burning in Punjab shifted from about 13:30 "
        "to about 17:00 IST over 2020-2024, i.e. after these afternoon overpasses. Fires that "
        "start and end between passes are not detected by VIIRS, while a burn scar stays "
        "visible to Sentinel-2 for days. These data alone do not measure how much burning is "
        "missed.")

    st.subheader("Per cell: estimate vs hotspots")
    st.image(str(DATA / "figures" / "grid_maps.png"),
             caption=f"Left: {AREA} per 5 km cell. Middle: VIIRS hotspots. Right: hotspot share "
                     "minus area share (red = fewer hotspots than the cell's share of the "
                     "estimate). A mismatch is a prompt for field checks, not a finding about "
                     "any village.")
    c = st.columns(2)
    c[0].image(str(DATA / "figures" / "cell_scatter.png"),
               caption=f"Per cell: {AREA} vs hotspots (Spearman "
                       f"{m5['cell_spearman_burnt_vs_hotspots']}).")
    reg = pd.DataFrame(m5["region"]).rename(columns={"burnt_ha": f"{AREA}, ha"})
    c[1].markdown("**West vs east** (west has fewer clear Sentinel-2 views, so less certain "
                  "estimates)")
    c[1].dataframe(reg, hide_index=True)


# --------------------------------------------------------------------------------- method
def method_page() -> None:
    """Method & limitations page: disclaimer, method, validation numbers, ethics."""
    lb = summary["labels"]
    ra = lb["repeat_agreement"]
    ce = ra.get("chart_effect") or {}
    d = ce.get("burnt_discordant", {})
    fc = lb["final_counts_first_pass"]
    br = lb["burnt_rate_by_source"]
    st.title("Method & limitations")
    st.warning(DISCLAIMER, icon="⚠️")
    st.markdown(f"""
### Why burnt area?
Official counts of stubble burning rely on active-fire hotspots from polar-orbiting satellites
(MODIS, VIIRS) that pass over Punjab around 13:30 IST and at night. If burning happens at other
times, it is not counted. A burnt field stays dark for days, so mapping **burnt area** from
Sentinel-2 does not depend on the time of the fire.

### Data
- **Sentinel-2 L2A** (10-20 m; B02, B03, B04, B08, B8A, B11, B12, SCL, AOT) via Microsoft
  Planetary Computer, every usable view 20 Sep - 30 Nov 2025.
- **Sentinel-1 RTC** (VV, VH): stored as supporting evidence only, never used to flag alone.
- **NASA FIRMS VIIRS** 375 m active fire, S-NPP and NOAA-20 standard-processing archive.
- **AOI:** Sangrur district (OpenStreetMap relation 9513553, ODbL); 5 km analysis grid.

### Method (M2.1, harvest-aware)
1. Clouds, shadow and haze masked per view (SCL classes, blue reflectance, cloud buffer,
   Sen2Cor aerosol optical thickness); per-acquisition radiometric normalisation on
   pseudo-invariant pixels.
2. **Harvest** per pixel: first clear view with NDVI < 0.3 after a green view (NDVI > 0.6).
3. **Burn** tested only between consecutive clear views after harvest: NBR drop > 0.10,
   relative albedo drop > 0.15 and NBR2 drop > 0.02, all three.
4. Each candidate gets a **burn-date window** [last clear unburnt view, first clear burnt
   view]; weeks here use the end of that window.
5. Cleaning: morphological opening, patches >= 0.1 ha. The first baseline (pre-season dNBR >
   0.27) flagged 80.6 % of the district: it was largely measuring harvest, not fire, which is
   why M2.1 was built.

{AREA}: **{m5['season_burnt_ha']:,.0f} ha** in the season, against **{m5['season_hotspots']}**
VIIRS detections.

### Validation study (small, one labeller)
A blind, stratified random sample of 200 items (159 M2.1 candidates, 41 unflagged cropland
cells), labelled in a purpose-built tool (time strip of every clear Sentinel-2 view, chart of
NBR / albedo / NDVI, ΔNBR row), with 20 hidden repeats and two blind review rounds.
- Final labels: burnt {fc.get('burnt', 0)}, not burnt {fc.get('not_burnt', 0)}, unsure
  {fc.get('unsure', 0)}.
- Labelled burnt: candidates {br['flagged']['burnt']}/{br['flagged']['n']}
  ({br['flagged']['rate']:.0%}); unflagged cropland {br['unflagged_cropland']['burnt']}/
  {br['unflagged_cropland']['n']} ({br['unflagged_cropland']['rate']:.0%}).
- **Repeat agreement {ra['agreement']:.0%} of {ra['n_pairs']} pairs (Cohen's kappa
  {ra['kappa']})**: low. Labels shifted strongly toward "burnt" across review rounds.
- Chart-on vs chart-off (repeats shown with imagery only): burnt only with the chart
  {d.get('burnt_only_with_chart')}, only without {d.get('burnt_only_without_chart')}, exact
  McNemar p = {d.get('mcnemar_exact_p')}. Not significant with 20 pairs, but the chart may pull
  labels toward the rule.

These labels are **not** reliable enough to calibrate or correct the estimate; they are shown
for transparency.

### Limitations
- **Unvalidated estimate.** Threshold rules, no calibrated confidence; label reliability is low
  (kappa above).
- **10-20 m resolution:** small fields and mixed pixels at field edges.
- **Tilled or wet soil** can look like a burn scar (darker, lower NBR).
- **Haze and smoke** remove views; the 28 Oct scene was 1.4 % clear and the 30 Oct scene was
  excluded, which shifts first detections into November.
- **Observation gaps in the west:** about 21 clear views per candidate vs 30 in the east.
- **Hotspots are not area:** VIIRS duplicates between satellites are not merged; small or short
  fires can be missed.

### Ethics
Decision support only: no contacting anyone, no fines, no automated enforcement. Every
candidate is `pending_field_verification` until a human checks it. Fields have synthetic IDs
derived from geometry; no owner names, phone numbers or land records are used.
""")
    st.image(str(DATA / "figures" / "random_candidates_rgb.png"),
             caption="Random M2.1 candidates (true colour before / after); for illustration.")


{"Map": map_page, "Weekly comparison": weekly_page,
 "Method & limitations": method_page}[page]()
