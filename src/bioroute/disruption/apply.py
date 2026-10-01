"""Applying a disruption scenario's edge removal to a graph.

IMPLEMENTED IN: Phase 7.

The removal mechanics, shared by classical / ACO / Physarum so that one set
of closure semantics applies to all three, plus the two verifications the
project's correctness rests on: source-target connectivity survives, and no
post-disruption path traverses a severed road.

--------------------------------------------------------------------------
WHY THE DEFAULT IS A VIEW, NOT A COPY
--------------------------------------------------------------------------

`apply_disruption` MUST NOT mutate the caller's graph. Phase 8 reuses one
base graph across thousands of runs (7 scenarios x N OD pairs x seeds x
algorithms); a single in-place `G.remove_edge` would leak into every
subsequent run and silently corrupt the whole sweep -- and it would corrupt
it *plausibly*, producing believable numbers for a network that no longer
matches the one on disk. That failure mode is unrecoverable after the fact,
so this is enforced by construction rather than by discipline.

The default is `networkx.restricted_view`, which returns a read-only
MultiDiGraph FILTERED VIEW over the same underlying data: O(1) to build,
no node/edge dicts copied, and structurally incapable of mutating the base
graph. On this network a `G.copy()` costs tens of milliseconds and tens of
megabytes; across a full sweep that is the difference between minutes and
hours of pure copying.

Pass `copy=True` when the caller genuinely needs a mutable, independent
graph (e.g. to write scratch attributes onto edges). The result is then a
detached copy and mutating it still cannot touch the base graph.

CAVEAT worth knowing: a view's edge ATTRIBUTE dicts are the base graph's
dicts, not copies. Reading them is free; writing to them writes through to
the base graph. Every algorithm in this project reads its weights from a
`TrafficSnapshot`, not from edge attributes, so nothing writes -- but a
future caller that wants to annotate edges must pass `copy=True`.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import networkx as nx

EdgeKey = tuple[int, int, int]


def apply_disruption(
    G: nx.MultiDiGraph,
    removed_edges: Iterable[EdgeKey],
    copy: bool = False,
) -> nx.MultiDiGraph:
    """Return ``G`` with ``removed_edges`` closed, WITHOUT mutating ``G``.

    Args:
        G: the base graph. Never modified. Guaranteed by construction, not
           by convention -- see the module docstring.
        removed_edges: (u, v, key) triples. Edges not present in ``G`` are
           ignored rather than raising, so a disruption computed against one
           graph can be replayed against an already-disrupted one (Phase 8
           applies a scenario on top of a base graph that may itself carry
           an earlier closure).
        copy: True for an independent mutable copy; default False for a
           zero-copy read-only filtered view.

    Returns:
        A MultiDiGraph over the surviving edges. Same nodes as ``G`` --
        closing every edge of an intersection (S6) isolates that node, it
        does not delete it, which is what a physically barricaded junction
        actually is.
    """
    removed = list(removed_edges)
    if not removed:
        return G.copy() if copy else nx.restricted_view(G, [], [])

    if copy:
        G2 = G.copy()
        for u, v, k in removed:
            if G2.has_edge(u, v, k):
                G2.remove_edge(u, v, k)
        return G2

    # restricted_view tolerates absent edges itself (it filters on
    # membership), so no pre-filtering is needed.
    return nx.restricted_view(G, [], removed)


def connectivity_preserved(G: nx.MultiDiGraph, source: int, target: int) -> bool:
    """Whether a DIRECTED path from ``source`` to ``target`` still exists.

    Directed, not weakly connected: the base graph is the largest strongly
    connected component precisely so that every OD pair is routable, and a
    closure that leaves only a wrong-way link has in fact disconnected the
    pair for routing purposes.
    """
    if source not in G or target not in G:
        return False
    if source == target:
        return True
    return nx.has_path(G, source, target)


def severed_pairs(G: nx.MultiDiGraph, removed_edges: Iterable[EdgeKey]) -> set[tuple[int, int]]:
    """Ordered node pairs made completely untraversable by the removal.

    A pair survives while ANY of its parallel edges survives, so a removal
    set that takes only some keys of a pair does not sever it. The closure
    semantics in `scenarios.py` always take all parallel edges of a closed
    pair, making this set equal to the set of closed pairs -- but the check
    is written generally so it stays honest if that ever changes.
    """
    removed = set(removed_edges)
    pairs = {(u, v) for u, v, _ in removed}
    out = set()
    for u, v in pairs:
        data = G.get_edge_data(u, v) or {}
        if all((u, v, k) in removed for k in data.keys()):
            out.add((u, v))
    return out


def path_violations(
    G: nx.MultiDiGraph,
    path: Sequence[int] | None,
    removed_edges: Iterable[EdgeKey],
) -> list[tuple[int, int]]:
    """Consecutive node pairs of ``path`` that traverse a severed road.

    The project-wide path representation is a bare node list, so "does this
    path use a closed edge?" is only well-posed at node-pair granularity:
    the path legitimately traverses (u, v) as long as one parallel edge
    survives. Returns the offending pairs; an empty list is the pass
    condition Phase 8 asserts on every post-disruption result.
    """
    if not path:
        return []
    severed = severed_pairs(G, removed_edges)
    return [(u, v) for u, v in zip(path, path[1:]) if (u, v) in severed]
