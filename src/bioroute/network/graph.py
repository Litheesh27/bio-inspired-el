"""Graph construction, normalization, and disk caching.

IMPLEMENTED IN: Phase 1.

Extraction of the largest strongly connected component from the raw OSMnx
MultiDiGraph, ox.add_edge_speeds / ox.add_edge_travel_times to derive
free-flow travel_time per edge, normalization of the osmid edge attribute to
always be list[int] (OSMnx yields a bare int for unmerged edges and a list
for simplified edges), persistence as GraphML keyed by a config hash (see
config.py), transparent reload-if-cached behavior, and a human-readable
network report (node/edge counts, total km, degree distribution, mean edge
length) printed after every build.

GraphML caveat: GraphML only stores scalar (stringified) attribute values,
so osmid (list[int]) round-trips as a Python-repr string unless explicitly
re-parsed on load. ``load_cached_graph`` passes an explicit ``edge_dtypes``
converter to ``ox.load_graphml`` so a graph loaded from cache has exactly
the same attribute types (list[int] osmid, float length/travel_time) as one
freshly built by ``build_graph``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import networkx as nx
import osmnx as ox

from bioroute.config import AreaConfig, config_hash


def _normalize_osmid(value: Any) -> list[int]:
    """Normalize an OSMnx `osmid` edge attribute to always be list[int].

    OSMnx gives a bare int for unmerged edges and a list[int] for edges
    merged during simplification. This collapses both cases to a uniform
    list[int] so Phase 2's SUMO edge-ID join can rely on a single type.
    """
    if isinstance(value, list):
        return [int(v) for v in value]
    return [int(value)]


# NOTE on the GraphML round-trip: ox.save_graphml stringifies every edge
# attribute with str(), so a normalized osmid=[123] becomes the literal text
# "[123]". ox.load_graphml's internal _convert_edge_attr_types already
# ast.literal_eval()s any "[...]"-looking string BEFORE applying a
# user-supplied edge_dtypes converter, and then applies that converter
# PER-ITEM when the parsed value is a list (not once to the whole list). So
# the correct edge_dtypes entry for osmid is the plain per-item type `int`,
# NOT a custom function that re-wraps its input in a list -- passing a
# list-producing converter here double-nests the result (each item becomes
# its own single-element list). Since normalization (see _normalize_osmid)
# guarantees every saved osmid is a bracketed list literal, even
# single-element ones, `int` alone reconstructs list[int] correctly via
# osmnx's built-in list handling.


def _largest_scc(G: nx.MultiDiGraph) -> tuple[nx.MultiDiGraph, int, int]:
    """Return (subgraph on the largest strongly connected component,
    number of SCCs found, number of nodes dropped)."""
    sccs = list(nx.strongly_connected_components(G))
    largest = max(sccs, key=len)
    dropped = G.number_of_nodes() - len(largest)
    G_scc = G.subgraph(largest).copy()
    return G_scc, len(sccs), dropped


def _count_tagged_vs_default_speeds(G_raw: nx.MultiDiGraph) -> tuple[int, int]:
    """Count edges that carry a usable pre-existing OSM `maxspeed` tag
    (tagged) vs. those that will fall back to the config `speeds_kph` /
    `default_speed_kph` values (default). Computed on the pre-add_edge_speeds
    graph, mirroring the fill logic in ox.add_edge_speeds: an edge keeps its
    own maxspeed-derived speed if present, and only missing ones are filled
    from the config-supplied per-highway-type table."""
    tagged = 0
    default = 0
    for _, _, data in G_raw.edges(data=True):
        maxspeed = data.get("maxspeed")
        if maxspeed is None:
            default += 1
            continue
        # maxspeed may be a list (simplified edges with differing tags);
        # treat as tagged if at least one usable value is present.
        values = maxspeed if isinstance(maxspeed, list) else [maxspeed]
        if any(v not in (None, "") for v in values):
            tagged += 1
        else:
            default += 1
    return tagged, default


def build_graph(cfg: AreaConfig, G_raw: nx.MultiDiGraph) -> tuple[nx.MultiDiGraph, dict]:
    """Build the canonical, normalized graph from a freshly-fetched OSMnx
    MultiDiGraph: largest strongly connected component, free-flow speeds
    and travel times, normalized osmid. Returns (graph, report_stats) where
    report_stats feeds the network report printed by
    scripts/01_build_network.py.
    """
    n_nodes_raw = G_raw.number_of_nodes()
    n_edges_raw = G_raw.number_of_edges()

    tagged_count, default_count = _count_tagged_vs_default_speeds(G_raw)

    G_scc, n_sccs, n_dropped = _largest_scc(G_raw)

    G = ox.routing.add_edge_speeds(
        G_scc, hwy_speeds=cfg.speeds_kph, fallback=cfg.default_speed_kph
    )
    G = ox.routing.add_edge_travel_times(G)

    for u, v, k, data in G.edges(keys=True, data=True):
        data["osmid"] = _normalize_osmid(data["osmid"])

        length = data.get("length")
        if length is None or not (length == length) or length <= 0 or length == float("inf"):
            raise AssertionError(f"Edge ({u},{v},{k}) has non-finite/non-positive length: {length!r}")
        data["length"] = float(length)

        tt = data.get("travel_time")
        if tt is None or not (tt == tt) or tt <= 0 or tt == float("inf"):
            raise AssertionError(f"Edge ({u},{v},{k}) has non-finite/non-positive travel_time: {tt!r}")
        data["travel_time"] = float(tt)

    report_stats = {
        "n_nodes_raw": n_nodes_raw,
        "n_edges_raw": n_edges_raw,
        "n_sccs": n_sccs,
        "n_nodes_dropped": n_dropped,
        "n_edges_tagged_speed": tagged_count,
        "n_edges_default_speed": default_count,
    }
    return G, report_stats


def cache_path(cfg: AreaConfig, network_dir: str | Path = "data/network") -> Path:
    """Path at which the cached GraphML for this config lives."""
    h = config_hash(cfg)
    return Path(network_dir) / f"{cfg.name}_{h}.graphml"


def save_graph(G: nx.MultiDiGraph, path: str | Path) -> None:
    """Persist the graph as GraphML."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(G, filepath=path)


def load_cached_graph(path: str | Path) -> nx.MultiDiGraph:
    """Load a GraphML-cached graph, restoring osmid to list[int] and
    length/travel_time/speed_kph to float, so the reloaded graph's edge
    attribute types are identical to a freshly built graph's."""
    G = ox.load_graphml(
        filepath=path,
        edge_dtypes={
            "osmid": int,
            "length": float,
            "travel_time": float,
            "speed_kph": float,
        },
    )
    # Defensive: guarantee list[int] even if some cache predates
    # normalization or a value round-tripped as a bare (unbracketed) osmid.
    for _, _, data in G.edges(data=True):
        data["osmid"] = _normalize_osmid(data["osmid"])
    return G


def get_or_build_graph(
    cfg: AreaConfig,
    G_raw_factory,
    network_dir: str | Path = "data/network",
    force: bool = False,
) -> tuple[nx.MultiDiGraph, dict, bool]:
    """Load the cached graph for cfg if present (unless force), else build
    it via G_raw_factory() (expected to return a freshly-fetched OSMnx
    MultiDiGraph) and cache it. Returns (graph, report_stats, was_cached).

    report_stats is None when loaded from cache (the raw-fetch statistics
    are not recoverable from the cached artifact alone).
    """
    path = cache_path(cfg, network_dir)
    if path.exists() and not force:
        G = load_cached_graph(path)
        return G, None, True

    G_raw = G_raw_factory()
    G, report_stats = build_graph(cfg, G_raw)
    save_graph(G, path)
    return G, report_stats, False
