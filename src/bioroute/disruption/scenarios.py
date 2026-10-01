"""Disruption scenario definitions, selected relative to the optimal path.

IMPLEMENTED IN: Phase 7.

Seven scenarios S0-S6. Every one of them selects edges *relative to the
current optimal path* for the (source, target, snapshot) being measured --
removing random far-away edges would leave the optimal path intact and
produce a null result, which is the single most important design constraint
in this module.

--------------------------------------------------------------------------
CLOSURE SEMANTICS -- the three judgement calls, made once, here
--------------------------------------------------------------------------

1. PARALLEL EDGES. The graph is a MultiDiGraph: several keyed edges can run
   between the same ordered node pair (dual carriageways, service roads
   collapsed by OSMnx simplification). "Closing a road" closes ALL parallel
   edges between that node pair in that direction. Closing only key 0 would
   leave a physically co-located lane open and the disruption would often
   cost nothing -- a null result dressed up as a closure. The atomic unit of
   closure in this module is therefore the ordered node pair (u, v), and
   `_closure_edges` expands it to every key present.

2. REVERSE DIRECTION. A real road closure (collapse, waterlogging,
   protest, metro works -- all of them the Bengaluru cases this project is
   about) blocks the carriageway in both directions. So closing (u, v) also
   closes every parallel edge of (v, u) when that reverse edge exists.
   This is `DisruptionConfig.both_directions`, DEFAULT TRUE.
   CONSEQUENCE, stated explicitly because downstream assertions depend on
   it: the returned edge list for S1-S4 contains edges that are NOT
   themselves on the optimal path -- the reverse of a path edge never is.
   The exact guaranteed relationship is:

       every removed edge (u, v, k) of S1-S4 has (u, v) or (v, u) in
       `meta["path_pairs_closed"]`, and `path_pairs_closed` is a subset of
       the optimal path's consecutive node pairs.

   Set `both_directions=False` for one-way-only closures; the relationship
   above then tightens to "(u, v) is a path pair" for every removed edge.

3. MULTI-WAY osmid IN S5. `osmid` is `list[int]` on every edge (Phase 1
   normalizes it; OSMnx simplification merged 341 of this graph's edges, so
   a single graph edge can belong to several OSM ways). S5 closes ONE
   CHOSEN WAY, not every way touching the chosen edge. Rationale: the
   scenario is "corridor closure", and one OSM way *is* the corridor. Taking
   the union of all ways on a merged edge would close 2-4 unrelated
   corridors at once, which is (a) not what a corridor closure means and
   (b) uncontrolled in severity -- S5's blast radius would depend on how
   lucky the simplifier got, not on the road network. The way is drawn from
   the chosen path edge's own `osmid` list with the scenario RNG, so it is
   reproducible, and it is recorded in `meta["way_id"]`. The corridor is
   then EVERY edge in the graph whose `osmid` list contains that way id,
   anywhere -- not just the contiguous stretch near the path.

--------------------------------------------------------------------------
HARD CONSTRAINT AND REJECTIONS
--------------------------------------------------------------------------

After removal, source and target must still be connected in the disrupted
directed graph. A candidate that disconnects them is REJECTED and the
scenario resamples. The rejection count is returned in
`Disruption.rejections` and must be recorded by Phase 8, never swallowed: a
high rejection rate is a finding about how fragile this road network is, and
belongs in the paper.

If `max_attempts` candidates are all rejected (or the path is too short for
the scenario, e.g. a 2-edge path under S4), the scenario returns
`Disruption(ok=False, removed_edges=[], reason=...)` -- an EMPTY set with a
stated reason. It never returns a disconnecting set. Pass `strict=True` to
raise `DisruptionInfeasible` instead.

--------------------------------------------------------------------------
BETWEENNESS (S2)
--------------------------------------------------------------------------

Exact edge betweenness on 2,856 nodes / 7,015 edges is far too expensive to
recompute per OD pair. Two things make it affordable:

* `k`-sampling: `networkx.edge_betweenness_centrality(..., k=k, seed=seed)`
  runs Brandes from k sampled sources instead of all n. `k` and the seed are
  recorded in `meta` so the ranking is reproducible.
* a process-level cache keyed by a content fingerprint of the graph
  structure AND of the snapshot's travel times AND (k, seed) -- so every OD
  pair in a Phase 8 sweep that shares a snapshot pays for betweenness once.
  Changing the snapshot changes the fingerprint and therefore the cache key,
  which is the CLAUDE.md cache-keying invariant applied in-memory.

Betweenness is computed on a SIMPLE DiGraph PROJECTION whose edge weight is
the minimum snapshot travel time over the parallel edges of that node pair
-- i.e. exactly the project's `TrafficSnapshot.nx_weight` cost convention.
The score belongs to the node pair (the closure unit, see semantics 1), and
every parallel edge of that pair inherits it. Scoring the multigraph
directly would hand a betweenness of 0.0 to every non-shortest parallel edge
while the road it sits on is in fact critical.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import networkx as nx

from bioroute.congestion.snapshot import TrafficSnapshot

EdgeKey = tuple[int, int, int]
NodePair = tuple[int, int]

SCENARIO_IDS: tuple[str, ...] = ("S0", "S1", "S2", "S3", "S4", "S5", "S6")

#: Human-readable one-liners, for figure captions and the results dashboard.
SCENARIO_LABELS: dict[str, str] = {
    "S0": "no disruption (control)",
    "S1": "single random path edge",
    "S2": "highest-betweenness path edge",
    "S3": "3 simultaneous path edges",
    "S4": "5 simultaneous path edges",
    "S5": "corridor closure (one OSM way)",
    "S6": "intersection closure (one path node)",
}

#: How many path node-pairs each multi-edge scenario closes.
_N_PAIRS: dict[str, int] = {"S1": 1, "S2": 1, "S3": 3, "S4": 5}


def _derive_seed(seed: int, scenario_id: str, source: Any, target: Any) -> int:
    """Deterministic per-(seed, scenario, OD pair) RNG seed.

    Uses sha256, NOT the builtin ``hash()``: Python randomizes string
    hashing per process (PYTHONHASHSEED), so ``hash(("S1", ...))`` would
    give a different disruption in every process -- a silent violation of
    the "every experiment run carries an explicit seed" invariant that would
    only show up as irreproducible Phase 8 results.
    """
    payload = f"{int(seed)}|{scenario_id}|{source}|{target}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


class DisruptionInfeasible(RuntimeError):
    """Raised (only when ``strict=True``) when a scenario cannot produce a
    connectivity-preserving edge set."""


@dataclass(frozen=True)
class DisruptionConfig:
    """Knobs that change closure *semantics*; see the module docstring for
    why each default is what it is."""

    #: Candidates to try before declaring the scenario infeasible.
    max_attempts: int = 50
    #: Close the opposing carriageway too (see semantics 2).
    both_directions: bool = True
    #: Close every parallel edge of a closed node pair (see semantics 1).
    close_parallel_edges: bool = True
    #: Brandes source samples for S2; None means exact (all sources).
    betweenness_k: int | None = 64
    #: Raise DisruptionInfeasible instead of returning ok=False.
    strict: bool = False


@dataclass(frozen=True)
class Disruption:
    """What a scenario returns.

    Attributes:
        scenario_id: "S0".."S6".
        removed_edges: the edge keys to remove, sorted and deduplicated.
            EMPTY for S0 (the control) and empty whenever ``ok`` is False.
        ok: whether a valid, connectivity-preserving disruption was found.
        rejections: how many candidate edge sets were discarded because they
            disconnected source from target. A RESULT, not a diagnostic --
            Phase 8 records it.
        attempts: candidates evaluated, including the accepted one.
        reason: "" when ok, else why the scenario could not be realized.
        meta: scenario-specific provenance (seed, chosen way/node, path
            length, betweenness k, the path pairs actually closed, ...).
    """

    scenario_id: str
    removed_edges: list[EdgeKey]
    ok: bool
    rejections: int = 0
    attempts: int = 0
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def n_removed(self) -> int:
        return len(self.removed_edges)


# ==========================================================================
# Path helpers
# ==========================================================================


def optimal_path(
    G: nx.MultiDiGraph, source: int, target: int, snapshot: TrafficSnapshot
) -> list[int] | None:
    """The Dijkstra-optimal path under ``snapshot``, or None if unreachable.

    Scenarios are defined relative to THIS path. It uses
    ``snapshot.nx_weight()`` so it is the same optimum Phase 3's
    DijkstraRouter reports and the same denominator the ``stretch``
    invariant is computed against.
    """
    try:
        return nx.dijkstra_path(G, source, target, weight=snapshot.nx_weight())
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None


def path_pairs(path: Sequence[int]) -> list[NodePair]:
    """Consecutive ordered node pairs of a path: the closure units."""
    return [(u, v) for u, v in zip(path, path[1:])]


def _closure_edges(
    G: nx.MultiDiGraph,
    pairs: Iterable[NodePair],
    cfg: DisruptionConfig,
) -> list[EdgeKey]:
    """Expand closure units (ordered node pairs) into concrete edge keys,
    applying the parallel-edge and reverse-direction rules."""
    out: set[EdgeKey] = set()
    for u, v in pairs:
        directions = [(u, v)] if not cfg.both_directions else [(u, v), (v, u)]
        for a, b in directions:
            data = G.get_edge_data(a, b)
            if not data:
                continue
            keys = sorted(data.keys())
            if not cfg.close_parallel_edges:
                keys = keys[:1]
            for k in keys:
                out.add((a, b, k))
    return sorted(out)


def _incident_edges(G: nx.MultiDiGraph, node: int) -> list[EdgeKey]:
    """Every edge with ``node`` as an endpoint, in or out, all keys."""
    out: set[EdgeKey] = set()
    for u, v, k in G.in_edges(node, keys=True):
        out.add((u, v, k))
    for u, v, k in G.out_edges(node, keys=True):
        out.add((u, v, k))
    return sorted(out)


# ==========================================================================
# S5 support: the OSM way -> edges index
# ==========================================================================


_WAY_INDEX_CACHE: dict[str, dict[int, list[EdgeKey]]] = {}


def _graph_fingerprint(G: nx.MultiDiGraph) -> str:
    """Content hash of the graph's edge structure. Stable across processes
    (unlike ``id(G)``), so a cache keyed on it can never serve one graph's
    answer for another's."""
    h = hashlib.sha256()
    for u, v, k in sorted(G.edges(keys=True)):
        h.update(f"{u},{v},{k};".encode("ascii"))
    return h.hexdigest()[:16]


def _snapshot_fingerprint(snapshot: TrafficSnapshot) -> str:
    """Content hash of a snapshot's provenance AND its travel times. The
    values are included deliberately: two snapshots with the same
    level/source/time_bin but different numbers must not share a cached
    betweenness ranking."""
    h = hashlib.sha256()
    h.update(f"{snapshot.source}|{snapshot.level}|{snapshot.time_bin}|".encode("utf-8"))
    for edge in sorted(snapshot.travel_time):
        h.update(f"{edge}={snapshot.travel_time[edge]!r};".encode("ascii"))
    return h.hexdigest()[:16]


def way_index(G: nx.MultiDiGraph) -> dict[int, list[EdgeKey]]:
    """Map every OSM way id to all graph edges carrying it.

    A merged edge appears under each of its several way ids -- that is the
    point of the index and the reason S5 must choose ONE way (see the
    module docstring, semantics 3).
    """
    key = _graph_fingerprint(G)
    cached = _WAY_INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    index: dict[int, list[EdgeKey]] = {}
    for u, v, k, data in G.edges(keys=True, data=True):
        osmid = data.get("osmid", [])
        if not isinstance(osmid, list):
            osmid = [osmid]
        for way in osmid:
            index.setdefault(int(way), []).append((u, v, k))
    for way in index:
        index[way].sort()
    _WAY_INDEX_CACHE[key] = index
    return index


def _ways_of_pair(G: nx.MultiDiGraph, pair: NodePair) -> list[int]:
    """Sorted union of the OSM way ids carried by the parallel edges of
    ``pair`` (and of its reverse, which is the same physical road)."""
    ways: set[int] = set()
    for a, b in ((pair[0], pair[1]), (pair[1], pair[0])):
        data = G.get_edge_data(a, b) or {}
        for attrs in data.values():
            osmid = attrs.get("osmid", [])
            if not isinstance(osmid, list):
                osmid = [osmid]
            ways.update(int(w) for w in osmid)
    return sorted(ways)


# ==========================================================================
# S2 support: cached, k-sampled edge betweenness
# ==========================================================================


_BETWEENNESS_CACHE: dict[tuple[str, str, int | None, int], dict[NodePair, float]] = {}


def clear_caches() -> None:
    """Drop the betweenness and way-index caches (tests; long-running
    processes that swap graphs)."""
    _BETWEENNESS_CACHE.clear()
    _WAY_INDEX_CACHE.clear()


def _simple_projection(G: nx.MultiDiGraph, snapshot: TrafficSnapshot) -> nx.DiGraph:
    """DiGraph whose edge weight is the min snapshot travel time over the
    parallel edges of that node pair -- the project's cost convention."""
    D = nx.DiGraph()
    D.add_nodes_from(G.nodes())
    tt = snapshot.travel_time
    for u, v, k in G.edges(keys=True):
        w = tt.get((u, v, k))
        if w is None:
            continue
        prev = D.get_edge_data(u, v)
        if prev is None or w < prev["weight"]:
            D.add_edge(u, v, weight=float(w))
    return D


def pair_betweenness(
    G: nx.MultiDiGraph,
    snapshot: TrafficSnapshot,
    k: int | None = 64,
    seed: int = 0,
) -> dict[NodePair, float]:
    """Edge betweenness per node pair, k-sampled and cached.

    See the module docstring for why this is per node pair rather than per
    keyed edge, and what the cache key covers.
    """
    n = G.number_of_nodes()
    effective_k = None if (k is None or k >= n) else int(k)
    cache_key = (_graph_fingerprint(G), _snapshot_fingerprint(snapshot), effective_k, int(seed))
    cached = _BETWEENNESS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    D = _simple_projection(G, snapshot)
    scores = nx.edge_betweenness_centrality(
        D, k=effective_k, normalized=True, weight="weight", seed=int(seed)
    )
    result = {(u, v): float(s) for (u, v), s in scores.items()}
    _BETWEENNESS_CACHE[cache_key] = result
    return result


# ==========================================================================
# The scenario generator
# ==========================================================================


def _fail(scenario_id: str, reason: str, meta: dict, cfg: DisruptionConfig) -> Disruption:
    if cfg.strict:
        raise DisruptionInfeasible(f"{scenario_id}: {reason}")
    return Disruption(
        scenario_id=scenario_id,
        removed_edges=[],
        ok=False,
        rejections=int(meta.get("rejections", 0)),
        attempts=int(meta.get("attempts", 0)),
        reason=reason,
        meta=meta,
    )


def _still_connected(
    G: nx.MultiDiGraph, removed: Iterable[EdgeKey], source: int, target: int
) -> bool:
    # Imported here rather than at module import time to keep the
    # scenarios <- apply dependency one-directional and obvious.
    from bioroute.disruption.apply import (  # noqa: PLC0415
        apply_disruption,
        connectivity_preserved,
    )

    G2 = apply_disruption(G, list(removed))
    return connectivity_preserved(G2, source, target)


def generate_disruption(
    G: nx.MultiDiGraph,
    source: int,
    target: int,
    snapshot: TrafficSnapshot,
    scenario_id: str,
    seed: int,
    path: Sequence[int] | None = None,
    config: DisruptionConfig | None = None,
) -> Disruption:
    """Generate scenario ``scenario_id``'s edge removals for one routing
    problem. Deterministic given ``seed`` (CLAUDE.md invariant).

    ``path`` is the optimal path under ``snapshot``; it is computed with
    `optimal_path` when not supplied. Passing it in lets Phase 8 reuse the
    Dijkstra solve it already did.

    Returns a `Disruption`. Check ``.ok`` -- on failure ``removed_edges`` is
    empty and ``reason`` says why. Never returns a disconnecting set.
    """
    cfg = config or DisruptionConfig()
    if scenario_id not in SCENARIO_IDS:
        raise ValueError(f"Unknown scenario {scenario_id!r}; expected one of {SCENARIO_IDS}")

    base_meta: dict[str, Any] = {
        "seed": int(seed),
        "source": source,
        "target": target,
        "both_directions": cfg.both_directions,
        "close_parallel_edges": cfg.close_parallel_edges,
        "max_attempts": cfg.max_attempts,
        "rejections": 0,
        "attempts": 0,
    }

    if scenario_id == "S0":
        base_meta["label"] = SCENARIO_LABELS["S0"]
        return Disruption("S0", [], ok=True, rejections=0, attempts=0, meta=base_meta)

    if path is None:
        path = optimal_path(G, source, target, snapshot)
    if path is None or len(path) < 2:
        return _fail(scenario_id, "no optimal path between source and target", base_meta, cfg)

    pairs = path_pairs(path)
    base_meta["label"] = SCENARIO_LABELS[scenario_id]
    base_meta["path_len_nodes"] = len(path)
    base_meta["path_len_edges"] = len(pairs)

    rng = random.Random(_derive_seed(seed, scenario_id, source, target))
    # Deriving the RNG from (seed, scenario, source, target) rather than the
    # bare seed keeps two different scenarios on the same seed from drawing
    # the same index sequence, which would correlate S1's edge with S3's
    # first edge and make the scenarios look more alike than they are.

    if scenario_id in ("S1", "S3", "S4"):
        return _random_path_edges(G, source, target, pairs, scenario_id, rng, cfg, base_meta)
    if scenario_id == "S2":
        return _highest_betweenness(G, source, target, snapshot, pairs, rng, cfg, base_meta)
    if scenario_id == "S5":
        return _corridor(G, source, target, pairs, rng, cfg, base_meta)
    return _intersection(G, source, target, path, rng, cfg, base_meta)


def _accept(
    scenario_id: str,
    removed: list[EdgeKey],
    rejections: int,
    attempts: int,
    meta: dict,
) -> Disruption:
    meta["rejections"] = rejections
    meta["attempts"] = attempts
    return Disruption(
        scenario_id=scenario_id,
        removed_edges=removed,
        ok=True,
        rejections=rejections,
        attempts=attempts,
        meta=meta,
    )


def _random_path_edges(G, source, target, pairs, scenario_id, rng, cfg, meta) -> Disruption:
    """S1 / S3 / S4: n distinct closure units drawn uniformly from the
    optimal path's node pairs."""
    n = _N_PAIRS[scenario_id]
    meta["n_pairs_requested"] = n
    if len(pairs) < n:
        return _fail(
            scenario_id,
            f"optimal path has {len(pairs)} edges, scenario needs {n}",
            meta,
            cfg,
        )

    rejections = 0
    attempts = 0
    tried: set[tuple[NodePair, ...]] = set()
    for _ in range(cfg.max_attempts):
        if n == 1:
            # For a single edge, enumerate distinct candidates in a seeded
            # order rather than resampling with replacement: every rejection
            # is then a genuinely new candidate and the rejection count means
            # "this fraction of the path's edges is a cut edge for this OD
            # pair" -- a directly interpretable fragility number.
            untried = [p for p in pairs if (p,) not in tried]
            if not untried:
                break
            choice = (rng.choice(untried),)
        else:
            choice = tuple(sorted(rng.sample(pairs, n)))
            if choice in tried:
                continue
        tried.add(choice)
        attempts += 1
        removed = _closure_edges(G, choice, cfg)
        if not removed:
            rejections += 1
            continue
        if _still_connected(G, removed, source, target):
            meta["path_pairs_closed"] = [list(p) for p in choice]
            return _accept(scenario_id, removed, rejections, attempts, meta)
        rejections += 1

    meta["rejections"] = rejections
    meta["attempts"] = attempts
    return _fail(
        scenario_id,
        f"no connectivity-preserving choice of {n} path edge(s) in {attempts} attempts",
        meta,
        cfg,
    )


def _highest_betweenness(G, source, target, snapshot, pairs, rng, cfg, meta) -> Disruption:
    """S2: the path's most structurally critical edge -- the worst case.

    Deterministic by construction (no sampling of candidates): candidates
    are the path's node pairs sorted by betweenness, descending. A rejection
    falls through to the next-highest, so S2 stays "the worst edge that is
    actually survivable", and the rejection count says how many strictly
    worse edges were genuine cut edges.
    """
    scores = pair_betweenness(G, snapshot, k=cfg.betweenness_k, seed=meta["seed"])
    n = G.number_of_nodes()
    meta["betweenness_k"] = None if (cfg.betweenness_k is None or cfg.betweenness_k >= n) else int(cfg.betweenness_k)
    meta["betweenness_seed"] = meta["seed"]
    meta["betweenness_n_nodes"] = n

    ranked = sorted(pairs, key=lambda p: (-scores.get(p, 0.0), p))
    rejections = 0
    attempts = 0
    for pair in ranked[: cfg.max_attempts]:
        attempts += 1
        removed = _closure_edges(G, [pair], cfg)
        if not removed:
            rejections += 1
            continue
        if _still_connected(G, removed, source, target):
            meta["path_pairs_closed"] = [list(pair)]
            meta["betweenness"] = scores.get(pair, 0.0)
            meta["betweenness_rank"] = attempts - 1
            return _accept("S2", removed, rejections, attempts, meta)
        rejections += 1

    meta["rejections"] = rejections
    meta["attempts"] = attempts
    return _fail(
        "S2", f"every path edge by betweenness rank disconnected s-t ({attempts} tried)", meta, cfg
    )


def _corridor(G, source, target, pairs, rng, cfg, meta) -> Disruption:
    """S5: close one whole OSM way that a path edge belongs to.

    Candidates are (path pair, one of its way ids) in a seeded order. See
    the module docstring, semantics 3, for why one way rather than all.
    """
    index = way_index(G)
    order = list(pairs)
    rng.shuffle(order)

    candidates: list[tuple[NodePair, int]] = []
    for pair in order:
        ways = _ways_of_pair(G, pair)
        rng.shuffle(ways)
        candidates.extend((pair, w) for w in ways)

    rejections = 0
    attempts = 0
    seen_ways: set[int] = set()
    for pair, way in candidates:
        if way in seen_ways:
            continue
        seen_ways.add(way)
        if attempts >= cfg.max_attempts:
            break
        attempts += 1
        corridor = index.get(way, [])
        if not corridor:
            rejections += 1
            continue
        # Reverse-direction rule applies to the whole corridor, not just to
        # the seed edge: closing a way closes both carriageways of every
        # node pair it runs through.
        corridor_pairs = {(u, v) for u, v, _ in corridor}
        removed = _closure_edges(G, corridor_pairs, cfg)
        if not removed:
            rejections += 1
            continue
        if _still_connected(G, removed, source, target):
            meta["way_id"] = int(way)
            meta["seed_pair"] = list(pair)
            meta["n_corridor_edges"] = len(corridor)
            meta["path_pairs_closed"] = [list(pair)]
            return _accept("S5", removed, rejections, attempts, meta)
        rejections += 1

    meta["rejections"] = rejections
    meta["attempts"] = attempts
    return _fail(
        "S5", f"no OSM way on the path could be closed without disconnecting s-t ({attempts} tried)", meta, cfg
    )


def _intersection(G, source, target, path, rng, cfg, meta) -> Disruption:
    """S6: close one node on the path and every edge incident to it.

    Source and target are excluded as candidates: closing every edge
    incident to an endpoint disconnects s-t by construction, so including
    them would inflate the rejection count with a trivially impossible
    candidate rather than measuring anything about the network.
    """
    interior = [n for n in path[1:-1] if n not in (source, target)]
    meta["n_interior_nodes"] = len(interior)
    if not interior:
        return _fail("S6", "optimal path has no interior node to close", meta, cfg)

    order = list(interior)
    rng.shuffle(order)

    rejections = 0
    attempts = 0
    for node in order[: cfg.max_attempts]:
        attempts += 1
        removed = _incident_edges(G, node)
        if not removed:
            rejections += 1
            continue
        if _still_connected(G, removed, source, target):
            meta["node"] = node
            meta["n_incident_edges"] = len(removed)
            return _accept("S6", removed, rejections, attempts, meta)
        rejections += 1

    meta["rejections"] = rejections
    meta["attempts"] = attempts
    return _fail(
        "S6", f"every interior path node disconnected s-t when closed ({attempts} tried)", meta, cfg
    )
