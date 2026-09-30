"""OSM-way-ID <-> SUMO-edge-ID <-> graph-edge (u,v,k) matching.

IMPLEMENTED IN: Phase 2c. Flagged in docs/PLAN.md as the single highest-risk
piece of the whole project.

SUMO derives edge IDs from OSM way IDs when netconvert is run WITHOUT
``--geometry.remove`` (see congestion/sumo.py): a plain way ID ``123456``
for the forward direction, ``-123456`` for the reverse direction of a
bidirectional way, ``123456#2`` for a split segment (netconvert splits a
way at every interior shape node when geometry is not removed), and
combinations of the two, e.g. ``-123456#0``. netconvert's ``--ramps.guess``
also introduces synthetic split edges suffixed ``-AddedOnRampEdge`` /
``-AddedOffRampEdge`` on top of an otherwise-normal ID. Internal
(junction) edges start with ``:`` and are never real OSM ways -- excluded
unconditionally.

Direction handling
------------------
The graph is a MultiDiGraph with (at most) one directed edge per direction
for a given way. A SUMO edge's leading ``-`` marks the *reverse* of the
way's OSM digitization direction, but that convention only tells us
*relative* direction (this segment vs. the other sign of the same way) --
not which of the graph's two directed edges (u, v, k) / (v, u, k') is
"positive". Two independent signals are used, in priority order:

1. **Node-ID match**: ``netconvert`` preserves original OSM node IDs as
   SUMO junction IDs *except* where ``--junctions.join`` merged a cluster of
   junctions into a synthetic ID (observed on ~38% of edges here, so this
   signal alone is not sufficient). Where a SUMO edge's from/to junction
   IDs both parse as plain ints, and a graph edge (u, v, k) with u=from,
   v=to exists whose osmid list contains this way ID, that is a direct,
   unambiguous match.
2. **Sign-convention inference**: for a way ID that has both a "forward"
   and a "reverse" graph edge (i.e. a bidirectional street), once *any*
   segment of that way is matched via (1), the sign of that segment fixes
   which graph edge direction is "positive" for the *entire* way (a way's
   digitization direction is constant along its whole length, so this
   generalizes across split segments). Remaining segments of the same way
   are then assigned by sign alone.
   If a way never gets a single node-ID-confirmed segment, its sign
   convention cannot be determined from data at hand; both directions are
   assigned to the same segments as a matching-coverage-preserving choice
   (not a directional guess), and every edge in the graph is flagged in
   ``coverage_report()["ambiguous_direction_edges"]`` so this cannot be
   read as high-confidence. This is deliberately conservative: undercounting
   coverage is safe, silently mis-assigning direction is not.
   A way with only one direction represented in the graph (a genuine
   one-way street) has no ambiguity at all: any segment of that way ID,
   whichever sign, belongs to the single graph edge.

Travel time aggregation
------------------------
A graph edge's congested travel time is its own length times the
**length-weighted mean PACE** (seconds per metre) of its matched SUMO
edges. Graph edges with no matched SUMO edge fall back to the graph's own
free-flow ``travel_time`` and are counted in the coverage report -- never
silently dropped and never counted as "matched".

PHASE 2D CORRECTION -- this used to be the length-weighted mean of the
matched segments' ``traveltime`` values, which is dimensionally wrong.
That quantity is "how long it takes to cross ONE of my matched segments",
not "how long it takes to cross this graph edge". netconvert splits a way
at every interior shape node, and OSMnx's ``simplify=True`` merges
consecutive ways, so a single graph edge routinely matches many short SUMO
segments: measured on this network, the ratio of total matched SUMO length
to graph-edge length has median 1.44 and 90th percentile 8.9. Wherever
that ratio exceeded 1, the old rule UNDERSTATED the edge's travel time by
roughly that factor.

The damage was measurable and severe. Under the old rule, 1,794 of 7,015
graph edges (26%) came out with SUMO travel times implying speeds above
the FASTEST speed limit anywhere in the network -- the worst by a factor
of 266. That silently broke A*'s optimality guarantee, whose heuristic is
great-circle distance over the global maximum edge speed and is only
admissible if no edge is traversed faster than that.

Working in PACE fixes it, because pace is scale-free: averaging
seconds-per-metre over the matched road and then multiplying by the GRAPH
edge's length gives a time for the graph edge's actual distance, however
many segments happened to match it. It is also the exact analogue of the
free-flow rule in ``network/graph.py::annotate_speeds_from_sumo`` (total
length over total time), so a congestion ratio on an edge is now a clean
ratio of two speeds derived by the same rule -- which is the whole point
of Phase 2d's shared baseline.

Geometric fallback
-------------------
If ID-based matching alone lands the exit criterion (>=85% of graph edges
matched) below threshold, ``geometric_fallback_match`` adds a secondary
pass: for each still-unmatched graph edge, nearest-neighbour match on
edge-midpoint distance among still-unmatched SUMO edges, accepted only if
the compass bearing of the two edges agrees within 30 degrees.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import networkx as nx

EdgeKey = tuple[int, int, int]

# Matches: optional leading '-' (reverse direction), digits (OSM way ID),
# optional '#<n>' (split-segment index), optional netconvert
# ramps.guess synthetic suffix. Verified against the actual generated
# net.net.xml for this project (see Phase 2b/2c report) rather than
# assumed from the SUMO docs alone.
SUMO_EDGE_ID_RE = re.compile(
    r"^(?P<neg>-)?(?P<way>\d+)(?:#(?P<split>\d+))?(?P<ramp>-Added(?:On|Off)RampEdge)?$"
)


@dataclass
class ParsedSumoEdge:
    sumo_id: str
    way_id: int
    sign: int  # +1 or -1
    from_node: str
    to_node: str
    length_m: float
    # PHASE 2D: netconvert's free-flow speed limit for this edge, m/s.
    # Carried here (rather than re-opened from the net object at every call
    # site) because Phase 2d's shared-baseline step re-annotates the graph's
    # `speed_kph`/`travel_time` from exactly these numbers -- see
    # network/graph.py's `annotate_speeds_from_sumo`. Defaults to 0.0 so
    # records constructed positionally by older code still build; a
    # non-positive speed is treated as "no usable SUMO speed" downstream.
    speed_mps: float = 0.0


def parse_sumo_edge_id(sumo_id: str) -> tuple[int, int] | None:
    """Return (way_id, sign) for a SUMO edge ID, or None if it is an
    internal junction edge (leading ':') or otherwise unparseable."""
    if sumo_id.startswith(":"):
        return None
    m = SUMO_EDGE_ID_RE.match(sumo_id)
    if not m:
        return None
    return int(m.group("way")), (-1 if m.group("neg") else 1)


def _node_int(node_id: str) -> int | None:
    try:
        return int(node_id)
    except (TypeError, ValueError):
        return None


def load_sumo_edges(net_path: str, sumolib_module=None) -> list[ParsedSumoEdge]:
    """Parse every non-internal edge of a SUMO net.net.xml into
    ParsedSumoEdge records. Requires sumolib (pass the already-imported
    module, or it is imported here assuming $SUMO_HOME/tools is already on
    sys.path -- see congestion/sumo.py's resolve_sumo_home)."""
    if sumolib_module is None:
        import sumolib as sumolib_module

    net = sumolib_module.net.readNet(str(net_path))
    parsed: list[ParsedSumoEdge] = []
    for e in net.getEdges(withInternal=False):
        sid = e.getID()
        result = parse_sumo_edge_id(sid)
        if result is None:
            continue
        way_id, sign = result
        parsed.append(
            ParsedSumoEdge(
                sumo_id=sid,
                way_id=way_id,
                sign=sign,
                from_node=e.getFromNode().getID(),
                to_node=e.getToNode().getID(),
                length_m=float(e.getLength()),
                speed_mps=float(e.getSpeed()),
            )
        )
    return parsed


def build_way_to_graph_edges(G: nx.MultiDiGraph) -> dict[int, list[EdgeKey]]:
    """way_id -> [(u,v,k), ...] using the normalized osmid lists (Phase 1
    guarantees list[int]). One graph edge can appear under multiple way
    IDs (the 341 edges OSMnx's simplify=True merged from consecutive
    ways)."""
    way_to_edges: dict[int, list[EdgeKey]] = {}
    for u, v, k, data in G.edges(keys=True, data=True):
        for way_id in data["osmid"]:
            way_to_edges.setdefault(way_id, []).append((u, v, k))
    return way_to_edges


@dataclass
class MatchResult:
    edge_to_sumo: dict[EdgeKey, list[str]] = field(default_factory=dict)
    unmatched_edges: set[EdgeKey] = field(default_factory=set)
    ambiguous_direction_edges: set[EdgeKey] = field(default_factory=set)
    n_matched_via_node_id: int = 0
    n_matched_via_sign_confirmed: int = 0
    n_matched_via_sign_unpaired: int = 0
    n_matched_via_sign_ambiguous: int = 0
    n_sumo_edges_unassigned: int = 0
    n_sumo_edges_total: int = 0


def match_sumo_edges_to_graph(
    G: nx.MultiDiGraph, sumo_edges: list[ParsedSumoEdge]
) -> MatchResult:
    """The ID-based join described in the module docstring. Returns a
    MatchResult mapping every graph edge to zero or more matched SUMO edge
    IDs, plus bookkeeping for the coverage report."""
    way_to_edges = build_way_to_graph_edges(G)

    # Index graph edges by (u, v) node pair (dropping k) so we can look up
    # "is there a graph edge from this SUMO edge's from-node to its
    # to-node".
    by_node_pair: dict[tuple[int, int], list[EdgeKey]] = {}
    for u, v, k in G.edges(keys=True):
        by_node_pair.setdefault((u, v), []).append((u, v, k))

    # Pre-classify each way as "paired" (has graph edges in both directions
    # for at least one node-pair pairing) or "unpaired" (one-way).
    def way_direction_groups(way_id: int) -> tuple[set[EdgeKey], set[EdgeKey]] | None:
        """Return (group_a, group_b) partitioning this way's graph edges
        into the two direction classes if the way is bidirectional in the
        graph (i.e. at least one (u,v) has a reverse (v,u) counterpart also
        tagged with this way ID), else None (one-way / no ambiguity)."""
        edges = way_to_edges.get(way_id, [])
        node_pairs = {(u, v) for (u, v, k) in edges}
        has_reverse = any((v, u) in node_pairs for (u, v) in node_pairs)
        if not has_reverse:
            return None
        # Arbitrary but deterministic split: smaller (u,v) tuple lexicographically
        # anchors "group_a"; its exact reverse anchors "group_b". Edges whose
        # node pair isn't purely one side (shouldn't normally happen) fall
        # into group_a to keep the partition total.
        group_a: set[EdgeKey] = set()
        group_b: set[EdgeKey] = set()
        seen_pairs: set[tuple[int, int]] = set()
        for (u, v, k) in edges:
            if (v, u) in seen_pairs:
                group_b.add((u, v, k))
            else:
                group_a.add((u, v, k))
                seen_pairs.add((u, v))
        return group_a, group_b

    way_sign_convention: dict[int, int] = {}  # way_id -> sign that maps to group_a
    way_groups: dict[int, tuple[set[EdgeKey], set[EdgeKey]] | None] = {}

    result = MatchResult()
    result.n_sumo_edges_total = len(sumo_edges)
    edge_to_sumo: dict[EdgeKey, list[str]] = {e: [] for e in G.edges(keys=True)}

    # Pass 1: node-ID-confirmed direct matches, and establish sign
    # conventions for paired ways as a side effect.
    deferred: list[ParsedSumoEdge] = []
    for se in sumo_edges:
        f, t = _node_int(se.from_node), _node_int(se.to_node)
        matched = False
        if f is not None and t is not None:
            candidates = by_node_pair.get((f, t), [])
            candidates = [
                (u, v, k) for (u, v, k) in candidates if se.way_id in G[u][v][k]["osmid"]
            ]
            if candidates:
                # Prefer the parallel edge with the closest length if more
                # than one key exists between the same node pair for this
                # way.
                best = min(
                    candidates,
                    key=lambda e: abs(G.edges[e]["length"] - se.length_m),
                )
                edge_to_sumo[best].append(se.sumo_id)
                result.n_matched_via_node_id += 1
                matched = True

                groups = way_groups.setdefault(se.way_id, way_direction_groups(se.way_id))
                if groups is not None:
                    group_a, _group_b = groups
                    convention = 1 if best in group_a else -1
                    # sign * convention_multiplier = "maps to group_a";
                    # store the sign that maps to group_a directly.
                    way_sign_convention.setdefault(
                        se.way_id, se.sign if best in group_a else -se.sign
                    )
        if not matched:
            deferred.append(se)

    # Pass 2: sign-based assignment for everything not directly matched.
    for se in deferred:
        groups = way_groups.setdefault(se.way_id, way_direction_groups(se.way_id))
        if groups is None:
            # One-way (or no graph representation at all): any segment of
            # this way ID belongs to whichever graph edges this way maps
            # to, regardless of sign.
            targets = way_to_edges.get(se.way_id, [])
            if not targets:
                result.n_sumo_edges_unassigned += 1
                continue
            for e in targets:
                edge_to_sumo[e].append(se.sumo_id)
            result.n_matched_via_sign_unpaired += len(targets)
            continue

        group_a, group_b = groups
        convention = way_sign_convention.get(se.way_id)
        if convention is not None:
            target_group = group_a if se.sign == convention else group_b
            for e in target_group:
                edge_to_sumo[e].append(se.sumo_id)
            result.n_matched_via_sign_confirmed += len(target_group)
        else:
            # No node-ID evidence anywhere for this way: cannot determine
            # which physical direction is which. Assign to BOTH groups
            # (conservative: preserves coverage measurement of "this OSM
            # geometry has traffic data" without asserting an unverified
            # direction) and flag every edge touched as ambiguous.
            for e in group_a | group_b:
                edge_to_sumo[e].append(se.sumo_id)
                result.ambiguous_direction_edges.add(e)
            result.n_matched_via_sign_ambiguous += len(group_a) + len(group_b)

    for e, sumo_ids in edge_to_sumo.items():
        if not sumo_ids:
            result.unmatched_edges.add(e)
    result.edge_to_sumo = edge_to_sumo
    return result


def _edge_midpoint_and_bearing(G: nx.MultiDiGraph, u: int, v: int) -> tuple[float, float, float]:
    """Return (mid_lat, mid_lon, bearing_degrees) for graph edge u->v using
    node coordinates (`x`=lon, `y`=lat, the osmnx convention)."""
    y1, x1 = G.nodes[u]["y"], G.nodes[u]["x"]
    y2, x2 = G.nodes[v]["y"], G.nodes[v]["x"]
    mid_lat = (y1 + y2) / 2.0
    mid_lon = (x1 + x2) / 2.0
    phi1, phi2 = math.radians(y1), math.radians(y2)
    dlambda = math.radians(x2 - x1)
    x_ = math.sin(dlambda) * math.cos(phi2)
    y_ = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    bearing = (math.degrees(math.atan2(x_, y_)) + 360.0) % 360.0
    return mid_lat, mid_lon, bearing


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def geometric_fallback_match(
    G: nx.MultiDiGraph,
    sumo_edges: list[ParsedSumoEdge],
    net,
    match: MatchResult,
    max_distance_m: float = 60.0,
    max_bearing_diff_deg: float = 30.0,
) -> int:
    """Secondary pass for edges still unmatched after the ID-based join:
    nearest-neighbour on edge-midpoint distance among SUMO edges not yet
    used by ANY graph edge, accepted only if compass bearing agrees within
    `max_bearing_diff_deg`. Mutates `match` in place. Returns the number of
    previously-unmatched graph edges newly matched.

    `net` is the sumolib net object (needed for SUMO edge shapes/midpoints).
    """
    already_used = {sid for ids in match.edge_to_sumo.values() for sid in ids}
    candidates = []
    for se in sumo_edges:
        if se.sumo_id in already_used:
            continue
        try:
            sumo_edge_obj = net.getEdge(se.sumo_id)
        except KeyError:
            continue
        shape = sumo_edge_obj.getShape()
        if len(shape) < 2:
            continue
        (x1, y1), (x2, y2) = shape[0], shape[-1]
        lon1, lat1 = net.convertXY2LonLat(x1, y1)
        lon2, lat2 = net.convertXY2LonLat(x2, y2)
        mid_lat = (lat1 + lat2) / 2.0
        mid_lon = (lon1 + lon2) / 2.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dlambda = math.radians(lon2 - lon1)
        xb = math.sin(dlambda) * math.cos(phi2)
        yb = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
        bearing = (math.degrees(math.atan2(xb, yb)) + 360.0) % 360.0
        candidates.append((se.sumo_id, mid_lat, mid_lon, bearing))

    n_newly_matched = 0
    for e in list(match.unmatched_edges):
        u, v, k = e
        mid_lat, mid_lon, bearing = _edge_midpoint_and_bearing(G, u, v)
        best_sid = None
        best_dist = max_distance_m
        for sid, clat, clon, cbearing in candidates:
            dist = _haversine_m(mid_lat, mid_lon, clat, clon)
            if dist > best_dist:
                continue
            diff = abs(cbearing - bearing) % 360.0
            diff = min(diff, 360.0 - diff)
            if diff > max_bearing_diff_deg:
                continue
            best_dist = dist
            best_sid = sid
        if best_sid is not None:
            match.edge_to_sumo[e].append(best_sid)
            match.unmatched_edges.discard(e)
            n_newly_matched += 1

    return n_newly_matched


def compute_edge_travel_times(
    G: nx.MultiDiGraph,
    match: MatchResult,
    sumo_traveltime: dict[str, float],
    sumo_length: dict[str, float],
) -> tuple[dict[EdgeKey, float], int]:
    """Graph-edge congested travel time = the graph edge's own length times
    the length-weighted mean PACE (s/m) of its matched SUMO edges; free-flow
    fallback (and count) for edges with no matched SUMO edge (or whose
    matched SUMO edges all lack a traveltime observation, e.g. zero traffic
    all simulation long).

    See the module docstring's "Travel time aggregation" section for why
    this works in pace rather than averaging the matched segments' travel
    times directly -- the short version is that a graph edge matches an
    arbitrary amount of SUMO road (median 1.44x its own length here, p90
    8.9x), so a mean of segment TIMES answers the wrong question and
    understated 26% of this network's edges into implying speeds above the
    fastest limit in the network.

    Returns (travel_time_map, n_fallback).
    """
    travel_time: dict[EdgeKey, float] = {}
    n_fallback = 0
    for e, sumo_ids in match.edge_to_sumo.items():
        # sum(tt_i) / sum(L_i) is the length-weighted mean of the per-segment
        # paces tt_i / L_i -- the same weighting as before, applied to the
        # quantity that actually composes across a chain of segments.
        total_time = 0.0
        total_length = 0.0
        for sid in sumo_ids:
            tt = sumo_traveltime.get(sid)
            if tt is None:
                continue
            length = sumo_length.get(sid)
            if length is None or length <= 0:
                continue
            total_time += tt
            total_length += length
        if total_length > 0 and total_time > 0:
            travel_time[e] = float(G.edges[e]["length"]) * total_time / total_length
        else:
            travel_time[e] = float(G.edges[e]["travel_time"])
            n_fallback += 1
    return travel_time, n_fallback


def coverage_report(G: nx.MultiDiGraph, match: MatchResult, n_fallback: int) -> dict:
    n_edges = G.number_of_edges()
    n_matched = n_edges - len(match.unmatched_edges)
    return {
        "n_graph_edges": n_edges,
        "n_matched_edges": n_matched,
        "n_unmatched_edges": len(match.unmatched_edges),
        "n_fallback_no_traveltime_observation": n_fallback,
        "coverage_pct": 100.0 * n_matched / n_edges,
        "n_ambiguous_direction_edges": len(match.ambiguous_direction_edges),
        "n_matched_via_node_id": match.n_matched_via_node_id,
        "n_matched_via_sign_confirmed": match.n_matched_via_sign_confirmed,
        "n_matched_via_sign_unpaired": match.n_matched_via_sign_unpaired,
        "n_matched_via_sign_ambiguous": match.n_matched_via_sign_ambiguous,
        "n_sumo_edges_total": match.n_sumo_edges_total,
        "n_sumo_edges_unassigned": match.n_sumo_edges_unassigned,
    }
