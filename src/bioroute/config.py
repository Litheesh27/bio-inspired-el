"""Configuration loading: YAML -> dataclasses, and seed management.

IMPLEMENTED IN: Phase 1 (introduced alongside network acquisition, but used
by every later phase).

Contains: dataclasses mirroring configs/area_central_blr.yaml (and later
configs/algorithms.yaml, configs/experiments/*.yaml); a loader that
validates and returns typed config objects; a deterministic config-hash
function (sha256 of canonicalized JSON, first 12 hex chars) used to key
cached artifacts (graph files, SUMO outputs, results) so that changing a
config invalidates the cache; and a `set_global_seed(seed: int)` helper so
every experiment run carries an explicit, reproducible seed.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is a hard requirement elsewhere
    np = None


@dataclass(frozen=True)
class BBox:
    """Lat/lon bounding box, WGS84 degrees."""

    north: float
    south: float
    east: float
    west: float

    def as_osmnx_bbox(self) -> tuple[float, float, float, float]:
        """Return (left, bottom, right, top) as required by osmnx>=2.x
        ``graph_from_bbox``."""
        return (self.west, self.south, self.east, self.north)


@dataclass(frozen=True)
class AreaConfig:
    """Study-area / network-acquisition configuration.

    Mirrors configs/area_central_blr.yaml. Frozen so a loaded config is
    hashable-by-content and cannot be mutated after load (mutation would
    silently desync the config hash from the cached artifact it names).
    """

    name: str
    bbox: BBox
    network_type: str
    simplify: bool
    overpass_url: str
    overpass_timeout: int
    speeds_kph: dict[str, float] = field(default_factory=dict)
    default_speed_kph: float = 20.0

    def to_canonical_dict(self) -> dict[str, Any]:
        """A plain-dict, JSON-serializable, order-stable representation used
        for hashing. Keys are sorted at dump time by ``config_hash``."""
        d = asdict(self)
        return d


def load_area_config(path: str | Path) -> AreaConfig:
    """Load an AreaConfig from a YAML file such as
    configs/area_central_blr.yaml."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    bbox_raw = raw["bbox"]
    bbox = BBox(
        north=float(bbox_raw["north"]),
        south=float(bbox_raw["south"]),
        east=float(bbox_raw["east"]),
        west=float(bbox_raw["west"]),
    )

    return AreaConfig(
        name=raw["name"],
        bbox=bbox,
        network_type=raw["network_type"],
        simplify=bool(raw["simplify"]),
        overpass_url=raw.get("overpass_url", "https://overpass-api.de/api/interpreter"),
        overpass_timeout=int(raw.get("overpass_timeout", 300)),
        speeds_kph=dict(raw.get("speeds_kph", {})),
        default_speed_kph=float(raw.get("default_speed_kph", 20.0)),
    )


def config_hash(cfg: AreaConfig) -> str:
    """Stable short hash of a config, used to key cached artifacts.

    Deterministic across processes and Python versions: built from
    ``hashlib.sha256`` over a canonical JSON encoding (sorted keys, fixed
    separators), NOT Python's built-in ``hash()`` which is randomly salted
    per-process (PYTHONHASHSEED) and therefore unusable as a cache key.

    Returns the first 12 hex characters of the digest.
    """
    canonical = json.dumps(
        cfg.to_canonical_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:12]


def set_global_seed(seed: int) -> None:
    """Seed every RNG source used across the project so a run is fully
    reproducible given the same seed. Call once at the start of any script
    that depends on randomness (ACO, Physarum init, OD sampling, etc.)."""
    random.seed(seed)
    if np is not None:
        np.random.seed(seed)
