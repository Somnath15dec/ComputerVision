"""Configuration loading: ``config.yaml`` (+ secrets from ``.env``)."""

from __future__ import annotations

import os
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel


class FallbackBBox(BaseModel):
    lat: float
    lon: float
    buffer_km: float


class AOIConfig(BaseModel):
    source: str = "osm"
    osm_relation_id: int | None = None
    fallback_bbox: FallbackBBox
    crs: str = "EPSG:32643"
    resolution_m: float = 10.0
    context_buffer_m: float = 500.0
    grid_km: float = 5.0
    subset_bbox: list[float] | None = None


class DateRange(BaseModel):
    start: date
    end: date


class PostWindows(DateRange):
    step_days: int = 7


class WindowsConfig(BaseModel):
    pre: DateRange
    post: PostWindows


class S2Config(BaseModel):
    collection: str
    bands: list[str]
    scl_band: str = "SCL"
    scl_invalid: list[int]
    max_scene_cloud_pct: float = 90
    reflectance_scale: int = 10000
    boa_offset: int = -1000


class S1Config(BaseModel):
    collection: str
    bands: list[str]
    orbit_state: str = "any"
    db_scale: int = 100


class PathsConfig(BaseModel):
    raw: Path
    cache: Path
    processed: Path
    labels: Path
    outputs: Path
    reports: Path


class IOConfig(BaseModel):
    block_rows: int = 512
    download_chunk: int = 2048
    max_retries: int = 3


class DetectionConfig(BaseModel):
    dnbr_threshold: float = 0.27
    opening_radius_px: int = 1
    min_patch_ha: float = 0.1
    connectivity: int = 8
    confirm_days: int = 10
    same_day_merge: str = "mean"


class QualityConfig(BaseModel):
    haze_blue_threshold: float = 0.15
    haze_cloud_classes: list[int] = [8, 9, 10]
    haze_cloud_buffer_m: float = 300
    wide_window_days: int = 10


class HazeMaskConfig(BaseModel):
    haze_aot_threshold: float | None = None
    scene_min_usable_frac: float = 0.2
    scene_max_pif_spread_factor: float = 3.0
    exclude_days: list[date] = []


class HarvestConfig(BaseModel):
    ndvi_green: float = 0.6
    min_green_views: int = 1
    ndvi_harvest: float = 0.3


class BurnConfig(BaseModel):
    dnbr_short_min: float = 0.10
    albedo_rel_drop_min: float = 0.15
    dnbr2_min: float = 0.02
    use_hazy_views: bool = False
    confirm_days: int = 10
    opening_radius_px: int = 1
    min_patch_ha: float = 0.1
    connectivity: int = 8
    char_nbr_max: float | None = None
    char_albedo_max: float | None = None


class RadiometryConfig(BaseModel):
    worldcover_classes: list[int] = [50, 60]
    erode_px: int = 1
    pif_ndvi_max: float = 0.3
    pif_max_rel_spread: float = 0.15
    pif_min_views: int = 8
    sample_px: int = 200000
    min_pif_px: int = 500
    seed: int = 20251001
    bands: list[str] = ["B02", "B03", "B04", "B08", "B11", "B12"]


class LabellingConfig(BaseModel):
    n_total: int = 400
    flagged_frac: float = 0.8
    n_score_bins: int = 5
    extra_weeks: dict[str, float] = {"w04": 2.0}
    unflagged_unit_px: int = 10
    unflagged_min_frac: float = 0.9
    seed: int = 20251118
    classes: list[str] = ["burnt", "tilled_not_burnt", "harvested_not_burnt", "wet", "unsure"]
    chip_pad_m: float = 250
    rgb_max_reflectance: float = 0.3


class S1SupportConfig(BaseModel):
    min_valid_frac: float = 0.5


class ProjectConfig(BaseModel):
    name: str
    district_code: str


class Window(BaseModel):
    """A compositing window; ``end`` is inclusive."""

    id: str
    start: date
    end: date

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()}/{self.end.isoformat()}"


class Config(BaseModel):
    project: ProjectConfig
    aoi: AOIConfig
    season: DateRange
    windows: WindowsConfig
    provider: str
    sentinel2: S2Config
    sentinel1: S1Config
    detection: DetectionConfig = DetectionConfig()
    quality: QualityConfig = QualityConfig()
    s1_support: S1SupportConfig = S1SupportConfig()
    harvest_detection: HarvestConfig = HarvestConfig()
    burn_detection: BurnConfig = BurnConfig()
    radiometry: RadiometryConfig | None = None
    haze_mask: HazeMaskConfig = HazeMaskConfig()
    labelling: LabellingConfig = LabellingConfig()
    paths: PathsConfig
    io: IOConfig
    root: Path = Path(".")

    def path(self, key: str, *parts: str) -> Path:
        """Absolute path under one of the configured data directories."""
        base = getattr(self.paths, key)
        p = base if base.is_absolute() else self.root / base
        return p.joinpath(*parts)

    def compositing_windows(self) -> list[Window]:
        """Pre-season window followed by the weekly post-harvest windows."""
        wins = [Window(id="pre", start=self.windows.pre.start, end=self.windows.pre.end)]
        post = self.windows.post
        cur, i = post.start, 1
        while cur <= post.end:
            end = min(cur + timedelta(days=post.step_days - 1), post.end)
            wins.append(Window(id=f"w{i:02d}", start=cur, end=end))
            cur, i = end + timedelta(days=1), i + 1
        return wins


def _find_config() -> Path:
    env = os.environ.get("STUBBLE_CONFIG")
    if env:
        return Path(env).resolve()
    here = Path.cwd()
    for d in (here, *here.parents):
        if (d / "config.yaml").exists():
            return d / "config.yaml"
    raise FileNotFoundError("config.yaml not found (set STUBBLE_CONFIG)")


@lru_cache(maxsize=4)
def load_config(path: str | None = None) -> Config:
    """Load and validate the YAML config. Also loads ``.env`` next to it."""
    cfg_path = Path(path).resolve() if path else _find_config()
    load_dotenv(cfg_path.parent / ".env")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    return Config(**raw, root=cfg_path.parent)


def secret(name: str) -> str | None:
    """Return a secret from the environment (.env); never log the value."""
    val = os.environ.get(name)
    return val or None
