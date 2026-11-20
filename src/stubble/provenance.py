"""Provenance sidecars: every output file gets ``<file>.provenance.json``.

The sidecar records inputs (scene IDs, dates), parameters, code version and a
parameter hash used for cache validation.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SIDE_SUFFIX = ".provenance.json"


def sidecar_path(output: Path) -> Path:
    """Path of the ``.provenance.json`` sidecar next to ``output``."""
    return output.with_name(output.name + SIDE_SUFFIX)


def params_hash(params: dict[str, Any]) -> str:
    """Stable short hash of a JSON-serialisable parameter dict."""
    blob = json.dumps(params, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


CODE_PATHS = ["src", "config.yaml", "pyproject.toml", "requirements.txt"]


def code_version() -> str:
    """Git commit, with ``-dirty`` if code or config differ from it; ``nogit`` otherwise.

    Only code and config paths are checked: regenerated reports (tracked files written by
    the pipeline itself) must not mark later outputs as dirty.
    """
    try:
        root = Path(__file__).resolve().parents[2]
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True,
            text=True, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", *CODE_PATHS], cwd=root,
            capture_output=True, text=True,
        ).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except Exception:
        return "nogit"


def write_provenance(
    output: Path, *, step: str, params: dict[str, Any], inputs: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write the provenance sidecar for ``output``."""
    rec = {
        "output": output.name,
        "step": step,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "code_version": code_version(),
        "params_hash": params_hash(params),
        "params": params,
        "inputs": inputs or [],
        **(extra or {}),
    }
    p = sidecar_path(output)
    p.write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    return p


def read_provenance(output: Path) -> dict[str, Any] | None:
    """Read the provenance sidecar of ``output``; None if it does not exist."""
    p = sidecar_path(output)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def is_cached(output: Path, params: dict[str, Any]) -> bool:
    """True if ``output`` exists and was produced with identical parameters."""
    if not output.exists():
        return False
    prov = read_provenance(output)
    return bool(prov) and prov.get("params_hash") == params_hash(params)
