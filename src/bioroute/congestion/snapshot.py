"""TrafficSnapshot: the sole channel through which routing algorithms see
congestion.

IMPLEMENTED IN: Phase 2a.

`TrafficSnapshot` is a frozen, validated mapping from every graph edge
(u, v, k) to a congested travel time in seconds, plus enough provenance
(``level``, ``time_bin``, ``source``, ``meta``) that any result row can be
traced back to exactly how its weights were produced. Phase 2b's SUMO
provider and Phase 2a's BPR/free-flow providers all emit this same type, so
Phases 3-9 (Dijkstra/A*/ACO/Physarum, metrics, disruption, experiments) never
import ``congestion.bpr`` or ``congestion.sumo`` directly.

Path cost convention: a path is a plain node list, which is ambiguous on a
MultiDiGraph with parallel edges between the same node pair. This module
defines the ONE convention used everywhere in this project:
``cost_of_path`` sums the *minimum-cost parallel edge* between each
consecutive node pair. Every phase that reports a path cost must go through
this method (or the equivalent min-over-parallel-edges logic in
``nx_weight``) so numbers are comparable across algorithms.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import networkx as nx

EdgeKey = tuple[int, int, int]


@dataclass(frozen=True)
class TrafficSnapshot:
    """A congestion snapshot: one congested travel time per graph edge.

    Attributes:
        level: coarse congestion label, e.g. "freeflow", "light", "heavy".
        time_bin: integer time-of-day bin index (0 for time-invariant
            providers such as free-flow).
        source: provenance of the numbers -- "freeflow" / "bpr" / "sumo".
            Every result row downstream can carry this so it's always
            possible to tell which congestion source produced a given
            measurement.
        travel_time: mapping (u, v, k) -> seconds. Must cover every edge an
            algorithm will query; values must be finite and > 0.
        meta: free-form provenance dict (config hash, seed, demand scale,
            capacity table version, etc.) -- enough to reproduce this exact
            snapshot from scratch.
    """

    level: str
    time_bin: int
    source: str
    travel_time: Mapping[EdgeKey, float]
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.travel_time:
            raise ValueError("TrafficSnapshot.travel_time must not be empty")
        for edge, value in self.travel_time.items():
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(
                    f"TrafficSnapshot: non-finite travel_time for edge {edge!r}: {value!r}"
                )
            if value <= 0:
                raise ValueError(
                    f"TrafficSnapshot: non-positive travel_time for edge {edge!r}: {value!r}"
                )

    def weight(self, u: int, v: int, k: int) -> float:
        """Congested travel time (seconds) of the single edge (u, v, k)."""
        return self.travel_time[(u, v, k)]

    def nx_weight(self) -> Callable[[int, int, dict], float | None]:
        """Return a callable usable directly as ``weight=`` for networkx
        shortest-path functions (``nx.shortest_path``, ``nx.dijkstra_path``,
        ``nx.astar_path``, etc.) on a MultiDiGraph.

        networkx's weight-function convention differs by graph type. For a
        callable ``weight``, networkx's internal ``_weight_function`` uses
        it AS-IS (it does not wrap it), and on a MultiDiGraph it is called
        as ``weight(u, v, d)`` where ``d`` is the dict of *all* parallel
        edges between u and v, keyed by edge key: ``{k: attr_dict, ...}``.
        This is the same convention networkx's own built-in multigraph
        weight function uses (``min(attr.get(weight, 1) for attr in
        d.values())``). We mirror that: take the minimum congested travel
        time over the parallel edges actually present in ``d``, using our
        own ``travel_time`` mapping rather than the graph's edge attributes,
        so the returned function is the single source of truth for cost
        regardless of which attribute values live on the graph.
        """
        travel_time = self.travel_time

        def _weight(u: int, v: int, d: Mapping[int, dict]) -> float | None:
            best: float | None = None
            for k in d.keys():
                val = travel_time.get((u, v, k))
                if val is None:
                    continue
                if best is None or val < best:
                    best = val
            return best

        return _weight

    def cost_of_path(self, G: nx.MultiDiGraph, path: list[int]) -> float:
        """Sum of minimum-cost parallel edge between each consecutive node
        pair in ``path``. This is the ONE path-cost convention used across
        the whole project (see module docstring) -- every algorithm's
        reported cost for a path must agree with this function given the
        same snapshot and graph.
        """
        total = 0.0
        for u, v in zip(path, path[1:]):
            edge_dict = G.get_edge_data(u, v)
            if not edge_dict:
                raise ValueError(f"No edge from {u} to {v} in graph")
            best: float | None = None
            for k in edge_dict.keys():
                val = self.travel_time.get((u, v, k))
                if val is None:
                    continue
                if best is None or val < best:
                    best = val
            if best is None:
                raise ValueError(
                    f"No travel_time entry in snapshot for any parallel edge "
                    f"({u}, {v}, *)"
                )
            total += best
        return total
