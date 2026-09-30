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

PHASE 2D ADDITION -- the shared free-flow baseline. A second, OPTIONAL
graph artifact is defined at the bottom of this module: the same Phase 1
graph with ``speed_kph``/``travel_time`` re-annotated from the speeds in
the SUMO net, cached under its own hash. Phase 1 is untouched -- see the
"PHASE 2D" section header for why that direction of dependency is
load-bearing -- and `load_baseline_graph` is the selector between the two.
"""

from __future__ import annotations

import hashlib
import json
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


# ==========================================================================
# PHASE 2D: one shared free-flow baseline
# ==========================================================================
#
# The problem this closes. Before Phase 2d "free-flow" meant two different
# things at once:
#
#   * the graph's ``travel_time`` came from the deliberately conservative
#     Bengaluru ``speeds_kph`` table in configs/area_central_blr.yaml
#     (residential 20 km/h, primary 40 km/h, ...);
#   * SUMO's ``net.net.xml`` derives its own free-flow speed per edge from
#     OSM tags and netconvert's per-highway-type defaults, which are
#     FASTER.
#
# So a SUMO *congested* travel time could come out BELOW the graph's
# *free-flow* travel time on the same edge, and "SUMO 1.7x" and "BPR 1.1x"
# were ratios against two different denominators -- not comparable, and not
# reportable side by side.
#
# The fix is to re-annotate ``speed_kph``/``travel_time`` on the cached
# graph from the speeds in the generated net.net.xml, and cache THAT as a
# separate artifact with its own hash. All three providers then reference
# one graph, so every ratio shares a denominator.
#
# ARCHITECTURAL CONSTRAINT, deliberately respected: this does NOT run at
# graph-build time. ``build_graph`` and scripts/01_build_network.py are
# untouched and still need no SUMO -- otherwise you could not build the
# graph without first running netconvert, which is a backwards dependency.
# Everything below is a *Phase 2* step that consumes the Phase 1 artifact
# and produces a second, independent one.

BASELINE_CONFIG_SPEEDS = "config_speeds"
BASELINE_SUMO_SPEEDS = "sumo_speeds"

# Bumped whenever the annotation MATH changes (independent of any config
# edit), so a cached sumo-speeds graph built under an older rule is never
# silently reused. See `annotate_speeds_from_sumo` for what "length-weighted
# harmonic" means and why it is the right mean here.
SUMO_SPEEDS_ANNOTATION_VERSION = "v1-length-weighted-harmonic-speed"


def baseline_of(G: nx.MultiDiGraph) -> str:
    """Which free-flow baseline this graph carries.

    Stamped into ``G.graph`` by `annotate_speeds_from_sumo`, and absent on
    any graph produced by Phase 1 -- hence the default. Reading it off the
    graph (rather than threading a flag through every provider constructor)
    is what lets FreeFlowProvider / BPRProvider / SUMOProvider all record
    the baseline in ``TrafficSnapshot.meta`` with no change to the
    CongestionProvider contract.
    """
    return str(G.graph.get("baseline", BASELINE_CONFIG_SPEEDS))


def baseline_hash_of(G: nx.MultiDiGraph) -> str:
    """The baseline artifact's hash, or "" for the Phase 1 config-speeds
    graph (whose identity is already the area config hash)."""
    return str(G.graph.get("baseline_hash", ""))


def baseline_meta(G: nx.MultiDiGraph) -> dict[str, str]:
    """The provenance pair every snapshot's ``meta`` carries, so a result
    row records which free-flow baseline produced it."""
    return {"baseline": baseline_of(G), "baseline_hash": baseline_hash_of(G)}


def file_sha256(path: str | Path, n_chars: int = 12) -> str:
    """Short content hash of a whole file, read in chunks."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:n_chars]


# netconvert opens every net.net.xml with an XML comment holding the
# generation TIMESTAMP and the output path it was told to write to, e.g.
#   <!-- generated on 2026-09-27T14:46:42.293800+05:30 by ... netconvert 1.27.1
#   <netconvertConfiguration> ... </netconvertConfiguration>
#   -->
# Both of those change on every run even when the network produced is
# byte-identical (verified: regenerating from the same OSM extract and the
# same flags differs ONLY inside this comment). Hashing them would make the
# baseline artifact, and every BPR/SUMO snapshot keyed off it, invalidate
# on every pipeline re-run for no reason at all -- so the hash starts after
# the comment.
_NET_HEADER_MARKER = b"<!-- generated on"
_NET_HEADER_END = b"-->"


def sumo_net_content_sha256(path: str | Path, n_chars: int = 12) -> str:
    """Short content hash of a SUMO net file, ignoring netconvert's leading
    generated-on header comment (see the note above).

    Hashing the CONTENT rather than only the netconvert flags is
    deliberate: the flags plus the OSM extract determine the net in
    principle, but only a content hash actually notices that the net on
    disk was regenerated from a different extract, hand-edited, or copied
    in from another run.
    """
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        first = f.read(1 << 20)
        marker = first.find(_NET_HEADER_MARKER)
        end = first.find(_NET_HEADER_END, marker) if marker != -1 else -1
        if end != -1:
            first = first[end + len(_NET_HEADER_END):]
        h.update(first)
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:n_chars]


def sumo_speeds_baseline_hash(
    cfg: AreaConfig,
    net_path: str | Path,
    netconvert_flags: list[str] | None = None,
) -> str:
    """Config hash for the sumo-speeds graph artifact.

    Covers everything the artifact depends on: which area graph it
    re-annotates, the exact net.net.xml the speeds come from, the
    netconvert flags that produced that net, and the annotation-rule
    version. Changing any of them changes the hash and therefore the cache
    path -- the CLAUDE.md cache-keying invariant.
    """
    payload = {
        "area_config_hash": config_hash(cfg),
        "net_sha256": sumo_net_content_sha256(net_path),
        "netconvert_flags": list(netconvert_flags or []),
        "annotation_version": SUMO_SPEEDS_ANNOTATION_VERSION,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def sumo_speeds_cache_path(
    cfg: AreaConfig, baseline_hash: str, network_dir: str | Path = "data/network"
) -> Path:
    """Path of the cached sumo-speeds graph. Deliberately a SEPARATE file
    from `cache_path`'s Phase 1 artifact rather than an overwrite: the
    config-speeds graph must stay byte-for-byte reproducible."""
    return (
        Path(network_dir)
        / f"{cfg.name}_{config_hash(cfg)}_{baseline_hash}_sumospeeds.graphml"
    )


def annotate_speeds_from_sumo(
    G: nx.MultiDiGraph,
    sumo_edges,
    match,
    baseline_hash: str = "",
) -> tuple[nx.MultiDiGraph, dict]:
    """Return a COPY of ``G`` whose ``speed_kph``/``travel_time`` come from
    the SUMO net's free-flow speeds, plus a coverage report.

    ``sumo_edges`` is ``network.ids.load_sumo_edges``'s output (each record
    carrying ``speed_mps``); ``match`` is
    ``network.ids.match_sumo_edges_to_graph``'s MatchResult -- i.e. the same
    97.5%-coverage ID join Phase 2c already relies on, not a second,
    separately-drifting matching scheme.

    Which mean. A graph edge can match several SUMO edges, because
    netconvert splits an OSM way at every interior shape node. Averaging
    their speeds ARITHMETICALLY would not preserve travel time: 100 m at
    10 m/s followed by 100 m at 50 m/s takes 12 s, not the 200/30 = 6.7 s
    an arithmetic mean implies. So the effective speed is the
    LENGTH-WEIGHTED HARMONIC mean, ``sum(L_i) / sum(L_i / v_i)``, which is
    exactly "total length over total free-flow time" and therefore
    reproduces the traversal time of the real chain of segments.
    ``travel_time`` is then the GRAPH edge's own length divided by that
    speed, so the graph's geometry -- not SUMO's, which differs slightly
    after junction joining -- still sets the distance.

    Edges with no SUMO match, or whose matched SUMO edges all report a
    non-positive speed, KEEP their config-derived speed and travel time
    untouched, are stamped ``speed_source="config"``, and are counted in
    the report. Nothing is silently dropped or defaulted.
    """
    G2 = G.copy()
    speed_by_sid = {se.sumo_id: float(se.speed_mps) for se in sumo_edges}
    length_by_sid = {se.sumo_id: float(se.length_m) for se in sumo_edges}

    n_sumo = 0
    n_config = 0
    tt_before = 0.0
    tt_after = 0.0
    speeds_before: list[float] = []
    speeds_after: list[float] = []

    for u, v, k, data in G2.edges(keys=True, data=True):
        old_tt = float(data["travel_time"])
        old_speed = float(data.get("speed_kph", 0.0))
        tt_before += old_tt
        speeds_before.append(old_speed)

        total_len = 0.0
        total_time = 0.0
        for sid in match.edge_to_sumo.get((u, v, k), ()):
            spd = speed_by_sid.get(sid, 0.0)
            length = length_by_sid.get(sid, 0.0)
            if spd <= 0.0 or length <= 0.0:
                continue
            total_len += length
            total_time += length / spd

        new_tt = None
        if total_time > 0.0:
            effective_mps = total_len / total_time
            candidate = float(data["length"]) / effective_mps
            # Defensive: a degenerate speed must never break the graph
            # invariant (finite, positive travel_time).
            if candidate > 0.0 and candidate == candidate and candidate != float("inf"):
                new_tt = candidate

        if new_tt is None:
            data["speed_source"] = "config"
            n_config += 1
            tt_after += old_tt
            speeds_after.append(old_speed)
        else:
            data["speed_kph"] = (total_len / total_time) * 3.6
            data["travel_time"] = new_tt
            data["speed_source"] = "sumo"
            n_sumo += 1
            tt_after += new_tt
            speeds_after.append(data["speed_kph"])

    G2.graph["baseline"] = BASELINE_SUMO_SPEEDS
    G2.graph["baseline_hash"] = baseline_hash
    G2.graph["annotation_version"] = SUMO_SPEEDS_ANNOTATION_VERSION

    n_edges = G2.number_of_edges()
    report = {
        "n_graph_edges": n_edges,
        "n_edges_sumo_speed": n_sumo,
        "n_edges_kept_config_speed": n_config,
        "coverage_pct": 100.0 * n_sumo / n_edges if n_edges else 0.0,
        "mean_speed_kph_before": (
            sum(speeds_before) / len(speeds_before) if speeds_before else 0.0
        ),
        "mean_speed_kph_after": (
            sum(speeds_after) / len(speeds_after) if speeds_after else 0.0
        ),
        "total_freeflow_time_s_before": tt_before,
        "total_freeflow_time_s_after": tt_after,
        "total_freeflow_time_ratio_after_over_before": (
            tt_after / tt_before if tt_before else float("nan")
        ),
    }
    return G2, report


def build_sumo_speeds_graph(
    cfg: AreaConfig,
    G: nx.MultiDiGraph,
    net_path: str | Path = "data/sumo/net.net.xml",
    netconvert_flags: list[str] | None = None,
    network_dir: str | Path = "data/network",
    sumolib_module=None,
    force: bool = False,
) -> tuple[nx.MultiDiGraph, dict, bool]:
    """Load-or-build the sumo-speeds baseline graph for ``cfg``.

    Returns (graph, report, was_cached); ``report`` is None on a cache hit,
    since the coverage counts are not recoverable from the artifact alone
    -- exactly how `get_or_build_graph` treats its own build stats.

    This is the Phase 2 step referred to in the section header; it is never
    called from Phase 1.
    """
    # Imported lazily so network/graph.py keeps no import-time dependency on
    # the SUMO side of the codebase.
    from bioroute.network.ids import (  # noqa: PLC0415
        load_sumo_edges,
        match_sumo_edges_to_graph,
    )

    baseline_hash = sumo_speeds_baseline_hash(cfg, net_path, netconvert_flags)
    path = sumo_speeds_cache_path(cfg, baseline_hash, network_dir)
    if path.exists() and not force:
        return load_cached_graph(path), None, True

    sumo_edges = load_sumo_edges(str(net_path), sumolib_module=sumolib_module)
    match = match_sumo_edges_to_graph(G, sumo_edges)
    G2, report = annotate_speeds_from_sumo(G, sumo_edges, match, baseline_hash=baseline_hash)
    save_graph(G2, path)
    return G2, report, False


def load_baseline_graph(
    cfg: AreaConfig,
    baseline: str = BASELINE_CONFIG_SPEEDS,
    network_dir: str | Path = "data/network",
    net_path: str | Path = "data/sumo/net.net.xml",
    netconvert_flags: list[str] | None = None,
) -> tuple[nx.MultiDiGraph, dict]:
    """THE loader every phase from 2 onward should use to obtain the single
    graph all providers share.

    ``baseline`` is the explicit selector: ``"config_speeds"`` (Phase 1's
    artifact, speeds from configs/area_central_blr.yaml) or
    ``"sumo_speeds"`` (re-annotated from net.net.xml). Callers that want
    the project-wide default read it from configs/congestion.yaml's
    ``baseline_graph`` key.

    Returns (graph, info) where info records the baseline name, its hash and
    the artifact path -- all of which reach provider snapshot meta via
    `baseline_meta`, so a result row can always be traced to the denominator
    its ratios were computed against.
    """
    if baseline == BASELINE_CONFIG_SPEEDS:
        path = cache_path(cfg, network_dir)
        if not path.exists():
            raise FileNotFoundError(
                f"Cached graph not found at {path}; run scripts/01_build_network.py first."
            )
        G = load_cached_graph(path)
        # Phase 1's artifact predates the stamp; make it explicit in memory
        # so `baseline_of` is never merely falling back to its default.
        G.graph.setdefault("baseline", BASELINE_CONFIG_SPEEDS)
        return G, {
            "baseline": BASELINE_CONFIG_SPEEDS,
            "baseline_hash": "",
            "path": str(path),
        }

    if baseline == BASELINE_SUMO_SPEEDS:
        baseline_hash = sumo_speeds_baseline_hash(cfg, net_path, netconvert_flags)
        path = sumo_speeds_cache_path(cfg, baseline_hash, network_dir)
        if not path.exists():
            raise FileNotFoundError(
                f"Sumo-speeds baseline graph not found at {path}; run "
                f"scripts/02_run_sumo.py to build it."
            )
        G = load_cached_graph(path)
        return G, {
            "baseline": BASELINE_SUMO_SPEEDS,
            "baseline_hash": baseline_hash,
            "path": str(path),
        }

    raise ValueError(
        f"Unknown baseline {baseline!r}; expected "
        f"{BASELINE_CONFIG_SPEEDS!r} or {BASELINE_SUMO_SPEEDS!r}"
    )
