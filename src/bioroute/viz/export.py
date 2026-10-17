"""Web dashboard JSON bundle export.

IMPLEMENTED IN: Phase 9c, driven by `scripts/05_export_web.py`.

THE BUNDLE HAS TWO KINDS OF CONTENT AND THEY MUST STAY DISTINGUISHABLE.

  `stats`  -- MEASURED. Every number comes from
              `data/results/final_<hash>.parquet` via `viz/stats.py`, so it
              covers all 30 OD pairs x 7 scenarios x 2 traffic levels. This
              is the experimental evidence.
  `routes` -- REGENERATED. The Parquet stores `path_hops` but not the paths
              themselves, so route geometry cannot come from it and has to
              be re-run (Phase 9b's `viz/traces.py`). It is reproducible --
              Phase 9b matched 64/64 values against the Parquet -- but it is
              a re-run, and it covers only the scenarios we chose to re-run.

Every record carries `provenance`, and the page labels the two differently.
Blurring them would let a reader take an illustrative map for the measured
result, which is the same class of error Phase 8a found inside Physarum.

SIZE. The published artifact must stay well under 16 MB. The network
geometry dominates, so coordinates are rounded to 5 decimal places (~1.1 m
at this latitude, far finer than any screen pixel here) and edges with no
`geometry` attribute are emitted as their two endpoints rather than
synthesised into a polyline.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

#: Coordinate rounding. 5 dp ~ 1.1 m of longitude at Bengaluru's latitude.
COORD_DP = 5

PROVENANCE_MEASURED = (
    "MEASURED -- computed from data/results/final_<confighash>.parquet, "
    "the committed Phase 8 sweep."
)
PROVENANCE_REGENERATED = (
    "REGENERATED -- re-run under the sweep's own config, graph, snapshots "
    "and seeds because the Parquet stores no path geometry. Reproducible "
    "(Phase 9b matched 64/64 values against the Parquet) but NOT itself "
    "the measured evidence."
)


def _r(value: float) -> float:
    return round(float(value), COORD_DP)


def network_geometry(G) -> dict[str, Any]:
    """Draw-ready street geometry: one polyline per edge, plus node coords.

    Uses each edge's own `geometry` where OSMnx kept one (curved streets
    are most of this network) and falls back to the straight endpoint pair
    otherwise. Keyed `"u|v|k"` to match `traces.congestion_bundle`, so the
    page can colour an edge by congestion without a second index.
    """
    nodes = {
        str(n): [_r(d["x"]), _r(d["y"])]
        for n, d in G.nodes(data=True)
        if "x" in d and "y" in d
    }

    edges: dict[str, list[list[float]]] = {}
    for u, v, k, data in G.edges(keys=True, data=True):
        geom = data.get("geometry")
        if geom is not None and hasattr(geom, "coords"):
            pts = [[_r(x), _r(y)] for x, y in geom.coords]
        else:
            su, sv = nodes.get(str(u)), nodes.get(str(v))
            if su is None or sv is None:
                continue
            pts = [su, sv]
        # Drop consecutive duplicates introduced by rounding.
        dedup = [pts[0]]
        for p in pts[1:]:
            if p != dedup[-1]:
                dedup.append(p)
        if len(dedup) >= 2:
            edges[f"{u}|{v}|{k}"] = dedup

    xs = [c[0] for c in nodes.values()]
    ys = [c[1] for c in nodes.values()]
    return {
        "nodes": nodes,
        "edges": edges,
        "bounds": {
            "west": min(xs), "east": max(xs),
            "south": min(ys), "north": max(ys),
        },
        "n_nodes": len(nodes),
        "n_edges": len(edges),
    }


def route_payload(bundles: Sequence[Any]) -> dict[str, Any]:
    """Index regenerated `RouteBundle`s by "src|tgt|scenario|level".

    Node lists are kept as node IDs rather than expanded to coordinates:
    the page already holds the node table, and expanding here would repeat
    every coordinate once per algorithm that traverses it.
    """
    out: dict[str, Any] = {}
    for b in bundles:
        key = f"{b.od_source}|{b.od_target}|{b.scenario_id}|{b.traffic_level}"
        payload = b.to_json()
        payload["provenance"] = PROVENANCE_REGENERATED
        out[key] = payload
    return out


def build_bundle(
    *,
    stats: Mapping[str, Any],
    network: Mapping[str, Any],
    od_pairs: Sequence[Any],
    routes: Mapping[str, Any],
    congestion: Mapping[str, Any] | None,
    traces: Mapping[str, Any] | None,
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble the single JSON object the dashboard page consumes."""
    return {
        "schema_version": 1,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "meta": dict(meta),
        "provenance": {
            "stats": PROVENANCE_MEASURED,
            "routes": PROVENANCE_REGENERATED,
            "traces": PROVENANCE_REGENERATED,
            "congestion": PROVENANCE_REGENERATED,
        },
        "od_pairs": [
            {
                "source": p.source,
                "target": p.target,
                "stratum": p.stratum,
                "distance_m": round(p.distance_m, 1),
            }
            for p in od_pairs
        ],
        "stats": dict(stats),
        "network": dict(network),
        "routes": dict(routes),
        "congestion": dict(congestion) if congestion else None,
        "traces": dict(traces) if traces else None,
    }


def write_bundle(path: str | Path, bundle: Mapping[str, Any]) -> tuple[Path, int]:
    """Write the bundle compactly and return (path, size_bytes).

    Separators are tightened because at this size the default
    ", "/": " padding costs tens of kilobytes for nothing.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(bundle, separators=(",", ":"), allow_nan=False)
    p.write_text(text, encoding="utf-8")
    return p, len(text.encode("utf-8"))
