"""Build reports/final/Stubble_Burn_Audit_PPT.pptx from the report figures.

Needs ``python-pptx`` (not a pipeline dependency): ``pip install python-pptx==1.0.2``.
Every number on the slides comes from the generated reports (M2, M2.1, M3 review r2, M5-lite).
Run from the repository root: ``python scripts/make_slides.py [LIVE_URL]``
(the live URL defaults to the deployed app).
"""

from __future__ import annotations

# ruff: noqa: RUF001  (typographic dashes, x and Greek letters are slide text)
import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

ROOT = Path(__file__).resolve().parents[1]
FIG = ROOT / "reports" / "final" / "figures"
OUT = ROOT / "reports" / "final" / "Stubble_Burn_Audit_PPT.pptx"
LIVE = sys.argv[1] if len(sys.argv) > 1 else "https://sangrur-burn-audit.streamlit.app"
NOTE = ("Free hosting sleeps when idle. If you see a wake-up screen, click the button and "
        "wait about 1 minute.")
REPO = "https://github.com/Somnath15dec/ComputerVision"
AREA = "M2.1 candidate burnt area (unvalidated estimate)"

INK = RGBColor(0x1F, 0x29, 0x37)
MUTED = RGBColor(0x5B, 0x65, 0x70)
ACCENT = RGBColor(0xC0, 0x39, 0x2B)
BLUE = RGBColor(0x1F, 0x77, 0xD0)
LIGHT = RGBColor(0xF3, 0xF4, 0xF6)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
BLANK = prs.slide_layouts[6]
W, H = prs.slide_width, prs.slide_height
n_slide = 0


def text(slide, x, y, w, h, body, size=18, color=INK, bold=False, align=PP_ALIGN.LEFT):
    """Add a text box; ``body`` is a string or a list of paragraphs (str or (str, size))."""
    tb = slide.shapes.add_textbox(x, y, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    paras = body if isinstance(body, list) else [body]
    for i, p in enumerate(paras):
        s = size
        if isinstance(p, tuple):
            p, s = p
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.alignment = align
        para.space_after = Pt(6)
        r = para.add_run()
        r.text = p
        r.font.size, r.font.color.rgb, r.font.bold = Pt(s), color, bold
    return tb


def bullets(slide, x, y, w, h, items, size=18):
    """Bulleted list (en-dash bullets) in a text box."""
    return text(slide, x, y, w, h, [f"–  {i}" for i in items], size=size)


def new(title, kicker=None):
    """New slide with a title bar, footer and slide number."""
    global n_slide
    n_slide += 1
    s = prs.slides.add_slide(BLANK)
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(0.18), H)
    bar.fill.solid()
    bar.fill.fore_color.rgb = ACCENT
    bar.line.fill.background()
    if kicker:
        text(s, Inches(0.6), Inches(0.3), Inches(12), Inches(0.4), kicker.upper(), 12, ACCENT, True)
    text(s, Inches(0.6), Inches(0.6), Inches(12.2), Inches(0.9), title, 30, INK, True)
    text(s, Inches(0.6), Inches(7.0), Inches(10), Inches(0.35),
         "Stubble burn-area audit, Sangrur 2025 · research prototype, decision support only",
         10, MUTED)
    text(s, Inches(12.2), Inches(7.0), Inches(0.8), Inches(0.35), str(n_slide), 10, MUTED,
         align=PP_ALIGN.RIGHT)
    return s


def pic(slide, name, x, y, w=None, h=None):
    """Place a figure from reports/final/figures."""
    return slide.shapes.add_picture(str(FIG / name), x, y, width=w, height=h)


def stat(slide, x, y, value, label, color=INK, w=None):
    """Stat tile: large value over a small label."""
    w = w or Inches(2.9)
    box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, Inches(1.45))
    box.fill.solid()
    box.fill.fore_color.rgb = LIGHT
    box.line.fill.background()
    box.adjustments[0] = 0.08
    text(slide, x + Inches(0.15), y + Inches(0.1), w - Inches(0.3), Inches(0.7), value, 28,
         color, True)
    text(slide, x + Inches(0.15), y + Inches(0.78), w - Inches(0.3), Inches(0.65), label, 12,
         MUTED)


def caption(slide, x, y, w, body):
    """Small grey caption text."""
    text(slide, x, y, w, Inches(0.5), body, 11, MUTED)


# 1 title -----------------------------------------------------------------------------------
n_slide += 1
s = prs.slides.add_slide(BLANK)
bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, W, H)
bg.fill.solid()
bg.fill.fore_color.rgb = INK
bg.line.fill.background()
pic(s, "study_area.jpg", Inches(8.1), 0, h=H)
text(s, Inches(0.7), Inches(1.6), Inches(8), Inches(2.2),
     "Burnt-area mapping of paddy stubble fires with Sentinel-2", 40, WHITE, True)
text(s, Inches(0.7), Inches(3.55), Inches(7.8), Inches(1),
     "A harvest-aware audit of Sangrur district, Punjab, 1 Oct – 30 Nov 2025, compared with "
     "NASA FIRMS VIIRS hotspots", 20, RGBColor(0xD1, 0xD5, 0xDB))
text(s, Inches(0.7), Inches(4.75), Inches(7.2), Inches(1.0),
     ["NAME · REG NO", "Lovely Professional University"], 18, WHITE)
text(s, Inches(0.7), Inches(5.85), Inches(7.2), Inches(1.4),
     [(f"Live: {LIVE}", 14), (f"Code: {REPO}", 14), (NOTE, 11)], 14,
     RGBColor(0xD1, 0xD5, 0xDB))

# 2 problem ---------------------------------------------------------------------------------
s = new("Hotspot counts miss fires that burn between satellite passes", "Problem")
bullets(s, Inches(0.6), Inches(1.7), Inches(6.6), Inches(4.8), [
    "Official “farm fire” numbers are active-fire detections from MODIS / VIIRS on "
    "polar orbits, seen at fixed overpass times (early afternoon over Punjab).",
    "ISRO SAC (Current Science 2025, geostationary MSG data): peak burning moved from about "
    "13:30 IST in 2020 to about 17:00 IST in 2024.",
    "A burnt field stays dark for days, whatever the hour it was lit, so burnt-area mapping "
    "cannot be dodged by timing.",
    "Goal: estimate burnt area for one district and season from open Sentinel-2 data, compare "
    "it with FIRMS, and show the gap on a dashboard, for human decision support only.",
], 18)
stat(s, Inches(7.8), Inches(1.8), "852 / 857", "VIIRS detections in Sangrur between 12:00 and "
     "14:59 IST (this study)", ACCENT, Inches(4.9))
stat(s, Inches(7.8), Inches(3.5), "13:30 → 17:00", "reported shift of peak burning, "
     "2020 → 2024 (ISRO SAC)", INK, Inches(4.9))
stat(s, Inches(7.8), Inches(5.2), "No enforcement", "no contact, no fines, no personal data; "
     "every polygon pending field verification", BLUE, Inches(4.9))

# 3 data ------------------------------------------------------------------------------------
s = new("Study area and open data", "Data")
pic(s, "weekly_composites.jpg", Inches(0.6), Inches(1.5), h=Inches(5.3))
bullets(s, Inches(5.6), Inches(1.6), Inches(7.3), Inches(5.3), [
    "Sangrur district (OSM boundary), 293,551 ha; 10 m grid, UTM 43N; 162 cells of 5 km.",
    "Sentinel-2 L2A (Planetary Computer): 37 acquisitions, 20 Sep – 29 Nov 2025; SCL cloud "
    "mask, Sen2Cor aerosol optical thickness.",
    "Sentinel-1 RTC VV/VH: supporting evidence per polygon only, never creates a detection.",
    "NASA FIRMS VIIRS 375 m, S-NPP + NOAA-20 archive: 857 detections in the district.",
    "ESA WorldCover 10 m for pseudo-invariant pixels (radiometric normalisation).",
    "Weeks: w00 = 1–5 Oct, then 7-day weeks w01–w08 from 6 Oct.",
], 17)
caption(s, Inches(0.6), Inches(6.85), Inches(5),
        "Sentinel-2 true-colour composites: pre-season, w01–w08")

# 4 pipeline --------------------------------------------------------------------------------
s = new("Pipeline: from catalogue to dashboard", "Method")


def node(x, y, label, fill, w=None, h=None):
    """Rounded box for the pipeline diagram: bold first line, small second line."""
    w, h = w or Inches(2.25), h or Inches(1.05)
    b = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    b.fill.solid()
    b.fill.fore_color.rgb = fill
    b.line.color.rgb = RGBColor(0x9C, 0xA3, 0xAF)
    b.adjustments[0] = 0.12
    tf = b.text_frame
    tf.word_wrap = True
    for i, ln in enumerate(label.split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.CENTER
        r = p.add_run()
        r.text = ln
        r.font.size, r.font.color.rgb, r.font.bold = Pt(14 if i == 0 else 11), INK, i == 0
    return b


def seg(x1, y1, x2, y2, head=False):
    """One straight line segment; ``head`` adds an arrowhead at the end."""
    c = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, x1, y1, x2, y2)
    c.line.color.rgb = MUTED
    c.line.width = Pt(1.75)
    if head:
        c.line._get_or_add_ln().append(_arrow())


def path(*pts):
    """Polyline through ``pts`` with an arrowhead on the last segment."""
    for i in range(len(pts) - 1):
        seg(*pts[i], *pts[i + 1], head=i == len(pts) - 2)


def _arrow():
    from lxml import etree
    from pptx.oxml.ns import qn
    e = etree.SubElement(etree.Element("dummy"), qn("a:tailEnd"))
    e.set("type", "triangle")
    return e


def rmid(b):  # right-middle
    """Right-middle point of a shape."""
    return b.left + b.width, b.top + b.height // 2


def lmid(b):  # left-middle
    """Left-middle point of a shape."""
    return b.left, b.top + b.height // 2


def tmid(b):  # top-middle
    """Top-middle point of a shape."""
    return b.left + b.width // 2, b.top


def bmid(b):  # bottom-middle
    """Bottom-middle point of a shape."""
    return b.left + b.width // 2, b.top + b.height


SRC, PROC, REJ, LAB, OUTC = (RGBColor(0xDB, 0xEA, 0xFE), LIGHT, RGBColor(0xFD, 0xE2, 0xE2),
                             RGBColor(0xDC, 0xFC, 0xE7), RGBColor(0xFE, 0xF3, 0xC7))
col = [Inches(x) for x in (0.6, 3.15, 5.7, 8.25, 10.8)]
row = [Inches(y) for y in (1.65, 3.25, 4.85)]
s2 = node(col[0], row[1], "Sentinel-2 L2A\nSCL, AOT, 10–20 m", SRC)
s1 = node(col[1], row[2], "Sentinel-1 RTC\nVV, VH (support only)", SRC)
fi = node(col[3], row[0], "FIRMS VIIRS\nS-NPP + NOAA-20", SRC)
m1 = node(col[1], row[1], "M1 data\ncache, mosaic, COGs", PROC)
m2 = node(col[2], row[0], "M2 baseline\ndNBR > 0.27 (rejected)", REJ)
m21 = node(col[2], row[1], "M2.1 detection\nharvest-aware, windows", PROC)
m5 = node(col[3], row[1], "M5-lite\n5 km cell × week", PROC)
m3 = node(col[3], row[2], "M3 validation\n200 blind labels", LAB)
da = node(col[4], row[1], "Dashboard\nStreamlit, 13 MB data", OUTC)
path(rmid(s2), lmid(m1))
path(tmid(s1), bmid(m1))
path(rmid(m1), lmid(m21))
path(tmid(m1), (tmid(m1)[0], lmid(m2)[1]), lmid(m2))
path(rmid(m21), lmid(m5))
path(bmid(fi), tmid(m5))
path(bmid(m21), (bmid(m21)[0], lmid(m3)[1]), lmid(m3))
path(rmid(m5), lmid(da))
path(rmid(m3), (bmid(da)[0], rmid(m3)[1]), bmid(da))
caption(s, Inches(0.6), Inches(6.45), Inches(12),
        "Every intermediate file has a provenance sidecar (code version, parameters); downloads "
        "are cached and never repeated. One CLI: stubble <step>.")

# 5 M2 vs M2.1 ------------------------------------------------------------------------------
s = new("The dNBR baseline measured harvest, not fire", "M2 vs M2.1")
pic(s, "m2_baseline.jpg", Inches(0.5), Inches(1.5), h=Inches(5.3))
pic(s, "m21_labels.jpg", Inches(5.0), Inches(1.5), w=Inches(6.4))
stat(s, Inches(5.0), Inches(5.45), "80.6 % → 46.3 %",
     "share of district flagged: M2 baseline → M2.1", ACCENT, Inches(3.6))
text(s, Inches(8.8), Inches(5.35), Inches(4.3), Inches(1.6), [
    ("M2: pre-season NBR median minus current NBR > 0.27. Harvest alone drops NBR from ~0.6 "
     "to ~−0.1, so 236,566 ha were flagged.", 12),
    ("M2.1: harvest date first (NDVI > 0.6 then < 0.3); burn tested only between consecutive "
     "clear views after it: ΔNBR > 0.10, albedo drop > 15 %, ΔNBR2 > 0.02. "
     "Result: 135,979 ha, 97,137 polygons.", 12)], 12, INK)

# 6 windows & quality -----------------------------------------------------------------------
s = new("Burn-date windows and quality bands", "M2.1")
pic(s, "w04_windows.png", Inches(0.5), Inches(1.6), w=Inches(7.4))
bullets(s, Inches(8.2), Inches(1.6), Inches(4.8), Inches(5.2), [
    "Window = [last clear unburnt view, first clear burnt view]; weeks use its end "
    "(detect_date), the first view showing the scar.",
    "Haze: blue > 0.15, 300 m cloud buffer, AOT > 0.6. Scene rule dropped 30 Oct and 22 Nov; "
    "28 Oct was 1.4 % clear.",
    "80,637 ha have a window overlapping w04; only 166 ha lie fully inside it.",
    "Per polygon: clear-view counts, gaps, haze at detection, single-observation flag, S1 "
    "change, rule-based score (not a probability).",
], 15)
caption(s, Inches(0.5), Inches(6.45), Inches(7.4), "Windows of candidates overlapping w04 "
        "(27 Oct – 2 Nov)")

# 7 validation design -----------------------------------------------------------------------
s = new("Validation: a blind, stratified sample of 200", "M3")
pic(s, "label_tool.jpg", Inches(0.5), Inches(1.5), h=Inches(5.4))
bullets(s, Inches(5.7), Inches(1.6), Inches(7.3), Inches(5.3), [
    "400 items drawn with a fixed seed: 320 M2.1 polygons (5 score bins × 8 weeks, w04 "
    "double weight) + 80 unflagged 1 ha cropland cells; first 200 labelled (159 + 41).",
    "Tool: time strip of every clear view (true colour, SWIR, ΔNBR) and an NBR / albedo / "
    "NDVI chart. No model output, score or stratum shown.",
    "Labels: burnt / not burnt / unsure + burn date; append-only SQLite.",
    "20 hidden repeats shown without chart and ΔNBR row: self-agreement and chart-on/off "
    "test. Chart-on labels are primary.",
    "Two blind review rounds with a 4 s save delay; r2 required a burn date for every burnt "
    "label.",
], 16)

# 8 validation results ----------------------------------------------------------------------
s = new("Validation results: the labels are the weakest link", "M3")
stat(s, Inches(0.6), Inches(1.7), "55 % · κ 0.17", "self-agreement on 20 hidden "
     "repeat pairs", ACCENT, Inches(3.9))
stat(s, Inches(4.75), Inches(1.7), "p = 0.289", "McNemar, chart on vs off: burnt only with "
     "chart 6, only without 2", INK, Inches(3.9))
stat(s, Inches(8.9), Inches(1.7), "134 / 57 / 9", "final labels: burnt / not burnt / unsure; "
     "143 of 143 burnt dated", INK, Inches(3.9))
stat(s, Inches(0.6), Inches(3.5), "76 % · 90 %", "M2.1 polygons labelled burnt, "
     "design-weighted, by count · by area", BLUE, Inches(3.9))
stat(s, Inches(4.75), Inches(3.5), "38 %", "unflagged cropland cells labelled burnt "
     "(design-weighted): M2.1 misses burns", BLUE, Inches(3.9))
stat(s, Inches(8.9), Inches(3.5), "57 · 10 · 71", "labels changed: r1 fast (of 96) · r1 "
     "control (of 20) · r2 remaining (of 103)", INK, Inches(3.9))
bullets(s, Inches(0.6), Inches(5.3), Inches(12.2), Inches(1.6), [
    "Labels moved strongly towards burnt across review rounds (r1: 52 + 8, r2: 54 "
    "not burnt → burnt); one labeller, no field visits.",
    "So no precision or recall is claimed, and the shares are not used to correct the area: "
    "every area figure stays an unvalidated estimate.",
], 16)

# 9 weekly results --------------------------------------------------------------------------
s = new("135,835 ha estimated in season vs 857 VIIRS detections", "Results")
pic(s, "weekly.png", Inches(0.5), Inches(1.6), w=Inches(8.2))
bullets(s, Inches(9.0), Inches(1.6), Inches(4.0), Inches(5.2), [
    "Area peaks in w05 (3–9 Nov, 69,473 ha), hotspots in w04 (416).",
    "The one-week lag fits the gap: 28 Oct almost fully cloudy, 30 Oct excluded, so late-"
    "October burns are first seen on 2 Nov or later.",
    "About 159 ha per detection, but a detection is a 375 m pixel at two moments a day: "
    "not an undercount factor.",
], 15)
caption(s, Inches(0.5), Inches(5.6), Inches(8.2), f"Bars: {AREA}, by week of detect_date. "
        "Line: VIIRS S-NPP + NOAA-20 detections by IST date.")

# 10 spatial --------------------------------------------------------------------------------
s = new("Both sources agree moderately on where burning happened", "Results")
pic(s, "grid_maps.png", Inches(1.6), Inches(1.4), w=Inches(10.0))
pic(s, "scatter.png", Inches(0.6), Inches(4.85), h=Inches(2.1))
bullets(s, Inches(3.5), Inches(4.95), Inches(9.4), Inches(2.0), [
    "Spearman ρ = 0.55 over 162 cells of 5 km; both concentrate in the centre and south.",
    "Largest gaps (few hotspots for the area): SGR-G-127, -124, -117, -043, -107, each "
    "1,284–1,486 ha with 1–2 detections.",
    "A gap can mean off-pass burning, false positives (tilled or wet soil), small fires or "
    "haze: a prompt for field checks, not a finding about any village.",
], 14)

# 11 hours + west/east ----------------------------------------------------------------------
s = new("VIIRS sees two short windows of the day", "Results")
pic(s, "hours.png", Inches(0.5), Inches(1.6), w=Inches(7.2))
caption(s, Inches(0.5), Inches(5.55), Inches(7.2),
        "VIIRS detections by IST hour. Peak burning reportedly shifted from 13:30 to 17:00 IST "
        "between 2020 and 2024 (ISRO SAC, Current Science 2025).")
stat(s, Inches(8.1), Inches(1.7), "12h 94 · 13h 528 · 14h 230",
     "detections by IST hour; 5 more at 01–02 h", INK, Inches(4.8))
text(s, Inches(8.1), Inches(3.5), Inches(4.8), Inches(0.4), "West vs east", 18, INK, True)
rows = [("", "West", "East"), ("Est. area (ha)", "69,321", "66,514"),
        ("VIIRS detections", "414", "443"), ("Cells", "85", "77"),
        ("Clear S2 views / polygon", "21.2", "30.1")]
tbl = s.shapes.add_table(len(rows), 3, Inches(8.1), Inches(4.0), Inches(4.8),
                         Inches(2.2)).table
for i, r in enumerate(rows):
    for j, v in enumerate(r):
        c = tbl.cell(i, j)
        c.text = v
        p = c.text_frame.paragraphs[0]
        for run in p.runs:
            run.font.size = Pt(13)
            run.font.bold = i == 0
        p.alignment = PP_ALIGN.RIGHT if j else PP_ALIGN.LEFT
caption(s, Inches(8.1), Inches(6.3), Inches(4.8), "Fewer views in the west: wider windows, "
        "less certain estimates.")

# 12 dashboard ------------------------------------------------------------------------------
s = new("Public dashboard (Streamlit Community Cloud)", "Deployment")
pic(s, "dash_map.jpg", Inches(0.5), Inches(1.5), h=Inches(4.3))
pic(s, "dash_weekly.jpg", Inches(5.4), Inches(1.5), h=Inches(4.3))
bullets(s, Inches(0.5), Inches(5.95), Inches(12.4), Inches(1.0), [
    "Reads only deploy/data/ (13 MB): grid by week, 857 hotspots, 96,806 candidate centroids, "
    "4,427 largest polygons (≥ 5.3 ha), 200 labelled points without field outlines.",
    "Five pinned packages (Streamlit, pandas, NumPy, Altair, pydeck); week slider, layers, "
    "IST-hour chart, Method & limitations page with the full disclaimer.",
], 14)

# 13 limitations & ethics -------------------------------------------------------------------
s = new("Limitations and ethics", "Read before use")
text(s, Inches(0.6), Inches(1.55), Inches(6), Inches(0.5), "Limitations", 20, ACCENT, True)
bullets(s, Inches(0.6), Inches(2.1), Inches(6.1), Inches(4.8), [
    "Label reliability: one labeller, κ 0.17, labels drifted towards burnt.",
    "10–20 m pixels: small fields, mixed pixels at bunds.",
    "Haze and smoke remove views (28 Oct, 30 Oct) and widen windows.",
    "Tilled or wet soil can look like a burn scar.",
    "Observation gaps in the west (21 vs 30 clear views).",
    "Threshold dependence: tens of percent; not tunable without labels.",
    "Hotspots: S-NPP / NOAA-20 duplicates not merged.",
], 15)
text(s, Inches(7.0), Inches(1.55), Inches(6), Inches(0.5), "Ethics", 20, BLUE, True)
bullets(s, Inches(7.0), Inches(2.1), Inches(5.9), Inches(4.8), [
    "Decision support only: no contact, no fines, no automated enforcement.",
    "Every polygon: status pending_field_verification until a human changes it.",
    "No personal data: no owners, phone numbers or land records; synthetic field IDs.",
    "Mismatch cells are prompts for checks, not findings about villages.",
    "Open data only; API keys kept out of the repository.",
], 15)

# 14 future work & links --------------------------------------------------------------------
s = new("Future work and links", "Next")
bullets(s, Inches(0.6), Inches(1.6), Inches(6.6), Inches(5), [
    "M4: U-Net initialised from SSL4EO-S12 Sentinel-2 weights, spatially split tiles, "
    "calibrated per-polygon confidence.",
    "Label re-test (r3) and a second labeller before training.",
    "Photo assessment after M4.",
    "Evidence packs per field for human verifiers.",
    "Merge VIIRS duplicates; add MODIS and NOAA-21.",
], 18)
stat(s, Inches(7.6), Inches(1.7), "Live dashboard", LIVE, BLUE, Inches(5.3))
stat(s, Inches(7.6), Inches(3.4), "Code", REPO, INK, Inches(5.3))
text(s, Inches(7.6), Inches(5.1), Inches(5.3), Inches(1.4),
     NOTE, 13, MUTED)

prs.save(OUT)
print(OUT, f"{OUT.stat().st_size / 1e6:.2f} MB, {n_slide} slides", file=sys.stderr)
