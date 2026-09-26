"""Road network acquisition from OpenStreetMap / Overpass.

IMPLEMENTED IN: Phase 1.

Fetches the study-area bbox TWICE from one logical Overpass snapshot so way
IDs are consistent between both artifacts:

  (a) raw OSM XML -> data/raw/<name>.osm.xml (all `highway` ways; this is
      what netconvert consumes in Phase 2 and does its own filtering on, so
      over-fetching here is the safe direction),
  (b) an OSMnx MultiDiGraph via ox.graph_from_bbox(bbox=..., network_type=
      "drive", simplify=True) (osmnx>=2.x signature: a single
      ``bbox=(left, bottom, right, top)`` tuple, NOT the removed 1.x
      north=/south=/east=/west= keyword form).

Fetch metadata (timestamp, Overpass endpoint, bbox, osmnx version) is
recorded into a sidecar JSON next to the raw XML.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import osmnx as ox
import requests

from bioroute.config import AreaConfig

# The Overpass API rejects requests.post()'s default python-requests
# User-Agent with a bare 406, so we identify ourselves explicitly. See
# https://overpass-api.de/ usage guidelines.
_OVERPASS_HEADERS = {
    "User-Agent": "bioroute-research/0.1 (Central Bengaluru routing benchmark)",
}

OVERPASS_QUERY_TEMPLATE = (
    "[out:xml][timeout:{timeout}];\n"
    "(way[\"highway\"]({south},{west},{north},{east}););\n"
    "(._;>;);\n"
    "out body;\n"
)


def _overpass_fetch_xml(cfg: AreaConfig, max_retries: int = 5) -> str:
    """Fetch raw OSM XML for the bbox from Overpass, with retry + backoff on
    rate-limiting (HTTP 429) and gateway timeouts (HTTP 504)."""
    query = OVERPASS_QUERY_TEMPLATE.format(
        timeout=cfg.overpass_timeout,
        south=cfg.bbox.south,
        west=cfg.bbox.west,
        north=cfg.bbox.north,
        east=cfg.bbox.east,
    )

    backoff = 5.0
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                cfg.overpass_url,
                data={"data": query},
                headers=_OVERPASS_HEADERS,
                timeout=cfg.overpass_timeout + 30,
            )
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (429, 504):
                print(
                    f"[acquire] Overpass returned {resp.status_code} "
                    f"(attempt {attempt}/{max_retries}); "
                    f"backing off {backoff:.0f}s"
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
        except requests.RequestException as exc:
            last_exc = exc
            print(
                f"[acquire] Overpass request failed (attempt "
                f"{attempt}/{max_retries}): {exc}; backing off {backoff:.0f}s"
            )
            time.sleep(backoff)
            backoff *= 2

    raise RuntimeError(
        f"Overpass fetch failed after {max_retries} attempts "
        f"(endpoint={cfg.overpass_url})"
    ) from last_exc


def fetch_raw_osm_xml(cfg: AreaConfig, out_path: str | Path, force: bool = False) -> Path:
    """Fetch raw OSM XML (all highway ways) for the config's bbox and write
    it to out_path, alongside a sidecar <out_path>.meta.json recording fetch
    metadata. Returns the path to the written XML.

    If out_path already exists and force is False, the existing file is
    reused (no network call) and its metadata sidecar is left untouched.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path = out_path.with_suffix(out_path.suffix + ".meta.json")

    if out_path.exists() and not force:
        print(f"[acquire] Reusing cached raw OSM XML at {out_path}")
        return out_path

    fetched_at = datetime.now(timezone.utc).isoformat()
    xml_text = _overpass_fetch_xml(cfg)

    out_path.write_text(xml_text, encoding="utf-8")

    meta = {
        "name": cfg.name,
        "fetched_at_utc": fetched_at,
        "overpass_url": cfg.overpass_url,
        "overpass_timeout": cfg.overpass_timeout,
        "bbox": {
            "north": cfg.bbox.north,
            "south": cfg.bbox.south,
            "east": cfg.bbox.east,
            "west": cfg.bbox.west,
        },
        "query": OVERPASS_QUERY_TEMPLATE.format(
            timeout=cfg.overpass_timeout,
            south=cfg.bbox.south,
            west=cfg.bbox.west,
            north=cfg.bbox.north,
            east=cfg.bbox.east,
        ),
        "osmnx_version": ox.__version__,
        "xml_bytes": len(xml_text.encode("utf-8")),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[acquire] Wrote raw OSM XML to {out_path} ({meta['xml_bytes']} bytes)")

    return out_path


def fetch_osmnx_graph(cfg: AreaConfig):
    """Fetch the OSMnx MultiDiGraph for the config's bbox via
    ox.graph_from_bbox, using the osmnx>=2.x bbox=(left, bottom, right, top)
    signature. This is a second, logically-simultaneous fetch against the
    same bbox as fetch_raw_osm_xml -- same study area, same Overpass
    snapshot semantics, but filtered by osmnx to network_type="drive" rather
    than the "all highways" query used for the raw XML.
    """
    bbox = cfg.bbox.as_osmnx_bbox()  # (left, bottom, right, top)
    print(
        f"[acquire] Fetching OSMnx graph for bbox={bbox} "
        f"network_type={cfg.network_type} simplify={cfg.simplify}"
    )
    G = ox.graph_from_bbox(
        bbox,
        network_type=cfg.network_type,
        simplify=cfg.simplify,
    )
    return G
