"""Spectral indices and per-observation quality tests (pure numpy, no I/O)."""

from __future__ import annotations

import numpy as np
from scipy.ndimage import maximum_filter

from stubble.data.composites import NODATA


def nbr(b8: np.ndarray, b12: np.ndarray) -> np.ndarray:
    """Normalized Burn Ratio ``(B8 - B12) / (B8 + B12)`` as float32.

    Inputs are int16 reflectance*10000 (``NODATA`` = missing). NaN where either band is
    missing or the denominator is not positive (possible after the BOA offset).
    """
    a = b8.astype(np.float32)
    b = b12.astype(np.float32)
    den = a + b
    ok = (b8 != NODATA) & (b12 != NODATA) & (den > 0)
    out = np.full(a.shape, np.nan, dtype=np.float32)
    np.divide(a - b, den, out=out, where=ok)
    return out


def dnbr(nbr_pre: np.ndarray, nbr_post: np.ndarray) -> np.ndarray:
    """``dNBR = NBR_pre - NBR_post``; positive values mean a drop in NBR (burn-like)."""
    return (nbr_pre - nbr_post).astype(np.float32)


def near_class(scl: np.ndarray, classes: list[int], radius_px: int) -> np.ndarray:
    """True within ``radius_px`` (square neighbourhood) of any pixel in ``classes``.

    Works on 2-D arrays or stacks ``(..., rows, cols)``; filtering is per 2-D slice.
    """
    hit = np.isin(scl, classes).astype(np.uint8)
    if radius_px <= 0:
        return hit.astype(bool)
    size = [1] * (hit.ndim - 2) + [2 * radius_px + 1, 2 * radius_px + 1]
    return maximum_filter(hit, size=size, mode="nearest").astype(bool)


def haze_flag(b02: np.ndarray, scl: np.ndarray, *, blue_threshold: float, scale: int,
              cloud_classes: list[int], radius_px: int) -> np.ndarray:
    """Suspected haze/smoke: bright blue reflectance, or close to SCL cloud classes.

    This flags observations; it does not remove them. Fresh burn scars are dark in B02, so
    the blue test does not fire on the scars themselves.
    """
    bright = (b02 != NODATA) & (b02 > blue_threshold * scale)
    return bright | near_class(scl, cloud_classes, radius_px)
