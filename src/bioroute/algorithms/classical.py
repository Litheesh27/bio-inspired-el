"""Classical shortest-path routers: Dijkstra and A*.

IMPLEMENTED IN: Phase 3.

`DijkstraRouter` is ground truth for the project's `stretch` invariant
(stretch = cost_alg / cost_dijkstra), so it must be exactly optimal: this
uses plain `networkx.dijkstra_path` against the snapshot's weight function,
no bidirectional shortcuts or approximations.

`AStarRouter` uses an admissible heuristic: great-circle (haversine)
distance from a node to the target, divided by the *global maximum* edge
speed anywhere in the graph (computed once in `prepare`, not a local or
typical speed -- a local speed would make the heuristic inadmissible and
silently non-optimal whenever the true path passes through a faster
segment than the one near the current node).

ADMISSIBILITY IS CONDITIONAL — it depends on the baseline graph, not just
on the snapshot. The heuristic is a valid lower bound only while every
snapshot travel time is >= the free-flow value this graph's `speed_kph`
implies. That holds unconditionally for `FreeFlowProvider` and for
`BPRProvider` (BPR multiplies free-flow by a factor >= 1), and it holds on
the `sumo_speeds` baseline graph, where the free-flow denominator is derived
from the same SUMO network the congested times come from.

It does NOT hold for a SUMO snapshot on the `config_speeds` graph: SUMO's
own speed limits are faster than the conservative Bengaluru table in
`configs/area_central_blr.yaml`, so 242 edges carry congested times BELOW
that graph's free-flow value and A* can return a non-optimal path. This was
a live bug found in Phase 2d, masked by A*-vs-Dijkstra tests that happened to
sample unaffected OD pairs. Pair SUMO snapshots with the `sumo_speeds`
baseline (the default in `configs/congestion.yaml`); see
`docs/tuning/sumo_calibration.md`.

Both routers are one-shot: `step()` computes the full solution on its first
call and is a no-op on every subsequent call, and disruption recovery is
their only mode -- a plain recompute on the graph with severed edges
removed. `warm` is accepted (for Router interface compatibility) but has no
effect: neither algorithm carries adaptable internal state to retain.
"""

from __future__ import annotations

import math
from typing import Any

import networkx as nx

from bioroute.algorithms.base import Router
from bioroute.congestion.snapshot import TrafficSnapshot


def _haversine_m(y1: float, x1: float, y2: float, x2: float) -> float:
    """Great-circle distance in meters between two (lat, lon) points."""
    R = 6371000.0
    phi1, phi2 = math.radians(y1), math.radians(y2)
    dphi = math.radians(y2 - y1)
    dlambda = math.radians(x2 - x1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


class _OneShotRouter(Router):
    """Shared one-shot machinery for Dijkstra and A*: compute once on the
    first `step()`, no-op after, plain-recompute disruption recovery."""

    name = "one_shot"

    def prepare(
        self,
        G: nx.MultiDiGraph,
        source: int,
        target: int,
        snapshot: TrafficSnapshot,
        seed: int,
    ) -> None:
        self._G = G
        self._source = source
        self._target = target
        self._snapshot = snapshot
        self._seed = seed
        self._path: list[int] | None = None
        self._cost: float | None = None
        self._iterations = 0
        self._done = False
        self._on_prepare(G, source, target, snapshot, seed)

    def _on_prepare(self, G, source, target, snapshot, seed) -> None:
        """Hook for subclass-specific one-time setup (e.g. A*'s max-speed
        heuristic constant). No-op by default."""
        return None

    def _compute(self) -> tuple[list[int] | None, float | None]:
        raise NotImplementedError

    def step(self) -> float | None:
        if self._done:
            return self._cost
        self._iterations = 1
        self._path, self._cost = self._compute()
        self._done = True
        return self._cost

    def best_path(self) -> list[int] | None:
        return self._path

    def best_cost(self) -> float | None:
        return self._cost

    def converged(self) -> bool:
        return self._done

    def apply_disruption(self, removed_edges: list[tuple[int, int, int]], warm: bool) -> None:
        # No warm-start concept applies here: Dijkstra/A* have no internal
        # adaptation state to retain, so warm=True and warm=False behave
        # identically -- a plain recompute on the graph with the severed
        # edges removed. This IS the honest classical baseline.
        G2 = self._G.copy()
        G2.remove_edges_from(list(removed_edges))
        self._G = G2
        self._path = None
        self._cost = None
        self._iterations = 0
        self._done = False

    def state(self) -> dict[str, Any]:
        return {}

    def load_state(self, state: dict[str, Any]) -> None:
        return None


class DijkstraRouter(_OneShotRouter):
    """Exact shortest path via networkx Dijkstra. Ground truth for the
    `stretch` invariant."""

    name = "dijkstra"

    def _compute(self) -> tuple[list[int] | None, float | None]:
        w = self._snapshot.nx_weight()
        try:
            path = nx.dijkstra_path(self._G, self._source, self._target, weight=w)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None, None
        cost = self._snapshot.cost_of_path(self._G, path)
        return path, cost


class AStarRouter(_OneShotRouter):
    """A* with an admissible great-circle / global-max-speed heuristic."""

    name = "astar"

    def _on_prepare(self, G, source, target, snapshot, seed) -> None:
        max_speed_kph = 0.0
        for _, _, data in G.edges(data=True):
            sp = data.get("speed_kph")
            if sp is not None and sp > max_speed_kph:
                max_speed_kph = float(sp)
        if max_speed_kph <= 0:
            raise ValueError(
                "Graph has no positive speed_kph edge attribute; A* heuristic "
                "is undefined without a global max speed."
            )
        self._max_speed_mps = max_speed_kph * 1000.0 / 3600.0
        self._target_y = G.nodes[target]["y"]
        self._target_x = G.nodes[target]["x"]

    def _heuristic(self, u: int, _v: int) -> float:
        # networkx always calls heuristic(node, target) with the same fixed
        # target passed to astar_path, so we ignore _v and use the cached
        # target coordinates directly.
        uy = self._G.nodes[u]["y"]
        ux = self._G.nodes[u]["x"]
        dist_m = _haversine_m(uy, ux, self._target_y, self._target_x)
        return dist_m / self._max_speed_mps

    def _compute(self) -> tuple[list[int] | None, float | None]:
        w = self._snapshot.nx_weight()
        try:
            path = nx.astar_path(
                self._G, self._source, self._target, heuristic=self._heuristic, weight=w
            )
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None, None
        cost = self._snapshot.cost_of_path(self._G, path)
        return path, cost
