Example data: M3 blind-labelled validation items (Sangrur, 2025)

No model has been trained or fine-tuned yet. The U-Net (M4) is future work. These items are
from the 200-item validation sample that was labelled blind to measure how far the rule-based
M2.1 burnt-area estimate can be trusted. They would become training and test labels for M4.

Each JPG is the time strip the labeller saw for one item: a 310 m window around a crosshair,
one column per clear Sentinel-2 date (20 Sep - 29 Nov 2025). Each row block shows:
  - a chart at the crosshair: NBR (red), visible+NIR albedo x4 (dark), NDVI (green)
  - true colour (B4, B3, B2)
  - SWIR false colour (B12, B8A, B4)
  - dNBR from the previous date (red = drop)
A burn after harvest shows as a sudden drop in NBR and albedo, and a dark field in true colour.

File names: <sample id>_<final label>.jpg. The six examples were drawn with a fixed random
seed (1): two burnt and one not_burnt among M2.1 candidates (source "flagged"), and one each
of burnt, not_burnt and unsure among unflagged cropland cells.

examples.csv      labels of the six examples
all_200_labels.csv  final labels of all 200 items (after review round r2)

Columns: sample_id, source (flagged = M2.1 candidate polygon; unflagged_cropland = 1 ha
cropland cell not flagged by M2.1), sample_week (sampling stratum), label (burnt / not_burnt
/ unsure), burn_first_seen (date the labeller saw the field turn dark), detected_window (M2.1
burn-date window, flagged items only), area_ha.

Caveats: one labeller, no field visits; self-agreement 55 % (Cohen's kappa 0.17) on 20 hidden
repeats. Treat the labels as indicative, not ground truth.

Imagery: Copernicus Sentinel-2 L2A via Microsoft Planetary Computer (Copernicus open licence).
No personal data: sample ids are synthetic.
