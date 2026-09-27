"""Ant Colony Optimization router (MAX-MIN Ant System).

IMPLEMENTED IN: Phase 4.

`ACORouter(Router)` implements MAX-MIN Ant System (MMAS): tau_min/tau_max
bounds on every edge's pheromone, which is what keeps this from stagnating
on a 2,856-node irregular graph the way vanilla Ant System does, and is what
makes dynamic re-adaptation after a disruption possible at all (a
completely converged vanilla-AS pheromone matrix has nowhere to go but a
full reset; MMAS's bounded matrix always retains some exploration
capacity).

Transition rule: p(u -> v via edge k) ~ tau(u,v,k)^alpha * eta(u,v,k)^beta.
`eta` has TWO configurable modes (configs/aco.yaml `use_goal_heuristic`):
  - use_goal_heuristic=False (classical MMAS as specified): plain
    eta = 1 / travel_time(u,v,k). No information about where the target
    is; only pheromone and local edge speed drive the walk.
  - use_goal_heuristic=True (default): eta(u,v,k) =
    1 / (travel_time(u,v,k) + h(v)), where h(v) is the SAME admissible
    great-circle / global-max-speed heuristic AStarRouter uses (haversine
    distance from v to the target, divided by the graph's fastest edge
    speed) -- the ACO analogue of A*'s f = g + h.
This is a config switch, not a hardcoded choice, precisely because it is
methodologically loaded: giving ACO the same target-location knowledge
AStarRouter's heuristic encodes is not "apples-to-apples" against the A*
baseline, and Physarum has no equivalent mechanism. See
docs/tuning/aco_sweep.md's goal-heuristic ablation for the measured
completion-rate and stretch difference between the two modes -- backtracking
construction (below) restores ant COMPLETION on its own; the goal heuristic
is a separate lever on CONVERGENCE QUALITY/speed, and the ablation reports
both effects honestly rather than presupposing which matters more. Which
mode becomes the paper's headline number is a decision left to the project
owner, not made here. Pheromone is keyed
per (u, v, k) edge tuple -- the same key TrafficSnapshot.travel_time uses --
rather than per node-pair, so parallel edges between the same two nodes can
carry distinct pheromone/desirability and an ant's realized edge choice is
never ambiguous. (This is a deliberate difference from the snapshot's
min-parallel-edge *path-cost* convention: that convention is for scoring an
already-built node-list path after the fact, so `best_cost()` and any
stretch computed against it always agrees with cost_of_path; it is not
about how an ant should choose among parallel edges while building a path.)

Deposit on the iteration-best path only, delta_tau = Q / L_best_iteration.
Evaporation rho applied to every tracked edge every iteration. tau_max is
recomputed dynamically each iteration as 1 / (rho * L_best_global) once any
valid path has been found (standard MMAS), tau_min = tau_max / tau_min_ratio;
both start at the config's tau_max_init / (tau_max_init / tau_min_ratio)
before that.

Robustness:
  - Per-ant tabu set (visited nodes) -- an ant can never revisit a node.
  - max_steps = max_steps_multiplier * an estimated graph diameter (hops),
    computed once per `prepare` via a double-sweep BFS from the source
    (never hardcoded).
  - Construction uses depth-first backtracking to a fork point on a local
    dead end (see `_construct_ant` for why this is necessary on a real
    street graph and the bounded operation budget that still guarantees
    termination); an ant that exhausts that budget, or backtracks all the
    way to the source with nothing left to try, is discarded, not repaired.
  - An iteration with zero successful ants is logged (via `extras`
    bookkeeping surfaced through `state()`) and does not crash; it also does
    not count toward the "iteration-best unchanged" convergence streak,
    since there is no iteration-best to compare.

Convergence fires only when BOTH: the iteration-best path has been
unchanged for k_stall consecutive (successful) iterations, AND the
pheromone branching factor is at or below branching_factor_threshold.
Neither alone is sufficient (see docs/PLAN.md Phase 4).

`apply_disruption(removed_edges, warm)`:
  - warm=False: full reinitialization -- pheromone reset to tau_max_init on
    every remaining edge, all counters cleared. Equivalent to `prepare()`
    again on the disrupted graph.
  - warm=True: the pheromone matrix is retained. Two configurable
    strategies (see configs/aco.yaml `disruption_strategy`):
      "zero"        -- only the severed edges themselves are dropped from
                       the pheromone matrix and adjacency.
      "local_reset" -- additionally, every edge within `disruption_n_hop`
                       hops of a severed edge's endpoints is reset to
                       tau_max (treating the graph as undirected for the
                       hop count), on top of dropping the severed edges.
  Either way, if the router's current best path used a severed edge it is
  invalidated (cleared) so `best_path()`/`best_cost()` never returns a path
  through a removed edge; subsequent `step()` calls resume the ant
  construction loop with the retained (adjusted) pheromone matrix.
"""

from __future__ import annotations

import logging
import random
from typing import Any

import networkx as nx

from bioroute.algorithms.base import Router
from bioroute.congestion.snapshot import TrafficSnapshot

logger = logging.getLogger(__name__)

EdgeKey = tuple[int, int, int]

DEFAULT_CONFIG: dict[str, Any] = {
    "alpha": 1.0,
    "beta": 4.0,  # used when use_goal_heuristic=True
    # When True (default): eta(u,v,k) = 1/(travel_time + h(v)), the
    # goal-directed heuristic (see module docstring and the "goal
    # heuristic ablation" in docs/tuning/aco_sweep.md). When False: plain
    # eta = 1/travel_time, the classical MMAS formulation, no target
    # location information at all -- this keeps the ACO/A*/Physarum
    # three-way comparison apples-to-apples if that is the mode chosen for
    # the paper's headline result.
    "use_goal_heuristic": False,
    # Optional separate beta for the no-heuristic mode (tuning needs may
    # differ since eta's scale/shape differs); falls back to `beta` above
    # if not set.
    "beta_no_heuristic": 1.0,  # used when use_goal_heuristic=False (default)
    "rho": 0.1,
    "n_ants": 40,
    "Q": 1.0,
    "tau_max_init": 1.0,
    "tau_min_ratio": 20.0,
    "max_steps_multiplier": 3,
    "backtrack_multiplier": 25,
    "k_stall": 60,
    "lambda_branching": 0.05,
    "branching_factor_threshold": 1.02,
    "disruption_strategy": "zero",
    "disruption_n_hop": 2,
    "suggested_budget": 600,  # headline (no-heuristic) mode
    "suggested_budget_goal_heuristic": 250,
}


def load_aco_config(path: str = "configs/aco.yaml") -> dict[str, Any]:
    """Load configs/aco.yaml into a plain dict, falling back to
    DEFAULT_CONFIG for any key the file omits."""
    import yaml
    from pathlib import Path

    cfg = dict(DEFAULT_CONFIG)
    p = Path(path)
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for k, v in raw.items():
            cfg[k] = v
    return cfg


def _haversine_m(y1: float, x1: float, y2: float, x2: float) -> float:
    """Great-circle distance in meters between two (lat, lon) points.
    Duplicated from algorithms/classical.py (a shared module we must not
    edit per the Phase 4 coordination constraints) rather than imported,
    to keep aco.py self-contained within its own touched-files list."""
    import math

    R = 6371000.0
    phi1, phi2 = math.radians(y1), math.radians(y2)
    dphi = math.radians(y2 - y1)
    dlambda = math.radians(x2 - x1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _estimate_diameter_hops(G: nx.MultiDiGraph, seed_node: int) -> int:
    """Double-sweep BFS (hop-count, directed) estimate of the graph's
    diameter, starting from `seed_node`. Not exact (a true diameter needs
    all-pairs BFS), but a cheap, honest approximation that scales the
    per-ant step budget to the graph at hand instead of a hardcoded
    constant."""
    lengths1 = dict(nx.single_source_shortest_path_length(G, seed_node))
    if not lengths1:
        return 1
    far_node = max(lengths1, key=lengths1.get)
    lengths2 = dict(nx.single_source_shortest_path_length(G, far_node))
    d1 = max(lengths1.values()) if lengths1 else 1
    d2 = max(lengths2.values()) if lengths2 else 1
    return max(1, d1, d2)


class ACORouter(Router):
    """MAX-MIN Ant System router."""

    name = "aco"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        config_path: str = "configs/aco.yaml",
        disruption_strategy: str | None = None,
        use_goal_heuristic: bool | None = None,
    ) -> None:
        """
        Args:
            config: explicit hyperparameter dict; if given, `config_path`
                is ignored. Missing keys fall back to DEFAULT_CONFIG.
            config_path: path to a YAML file (configs/aco.yaml by default)
                used when `config` is not given.
            disruption_strategy: overrides the config's
                `disruption_strategy` ("zero" or "local_reset") for this
                instance -- lets tests/tuning compare both strategies
                without editing the config file.
            use_goal_heuristic: overrides the config's
                `use_goal_heuristic` for this instance -- lets tests/tuning
                compare the goal-directed and plain-MMAS heuristic modes
                without editing the config file.
        """
        if config is not None:
            merged = dict(DEFAULT_CONFIG)
            merged.update(config)
            self.config = merged
        else:
            self.config = load_aco_config(config_path)
        if disruption_strategy is not None:
            self.config["disruption_strategy"] = disruption_strategy
        if use_goal_heuristic is not None:
            self.config["use_goal_heuristic"] = use_goal_heuristic
        if self.config["disruption_strategy"] not in ("zero", "local_reset"):
            raise ValueError(
                f"Unknown disruption_strategy: {self.config['disruption_strategy']!r}"
            )

    # ------------------------------------------------------------------
    # Router contract
    # ------------------------------------------------------------------

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
        self._rng = random.Random(seed)

        self._alpha = float(self.config["alpha"])
        self._use_goal_heuristic = bool(self.config["use_goal_heuristic"])
        if not self._use_goal_heuristic and self.config.get("beta_no_heuristic") is not None:
            self._beta = float(self.config["beta_no_heuristic"])
        else:
            self._beta = float(self.config["beta"])
        self._rho = float(self.config["rho"])
        self._n_ants = int(self.config["n_ants"])
        self._Q = float(self.config["Q"])
        self._tau_max_init = float(self.config["tau_max_init"])
        self._tau_min_ratio = float(self.config["tau_min_ratio"])
        self._k_stall = int(self.config["k_stall"])
        self._lambda_branching = float(self.config["lambda_branching"])
        self._branching_threshold = float(self.config["branching_factor_threshold"])
        self._disruption_n_hop = int(self.config["disruption_n_hop"])

        diameter = _estimate_diameter_hops(G, source)
        self._max_steps = max(4, int(self.config["max_steps_multiplier"]) * diameter)
        self._diameter_estimate = diameter
        self._backtrack_multiplier = int(self.config["backtrack_multiplier"])

        self._setup_heuristic()
        self._build_adjacency()
        self._update_reachability()

        self._tau_max = self._tau_max_init
        self._tau_min = self._tau_max_init / self._tau_min_ratio
        self._pheromone: dict[EdgeKey, float] = {e: self._tau_max for e in self._all_edges}

        self._best_path: list[int] | None = None
        self._best_cost: float | None = None
        self._best_edges: list[EdgeKey] | None = None
        self._prev_iteration_best_path: tuple[int, ...] | None = None
        self._stall_count = 0
        self._iterations = 0
        self._converged = False
        self._empty_iterations = 0
        self._done_steps = 0

    def _setup_heuristic(self) -> None:
        """Admissible remaining-distance term h(v) added to travel_time
        inside eta (see module docstring): great-circle distance from v to
        the target, divided by the graph's global max edge speed. Cached
        per-node on first use since only visited nodes ever need it."""
        max_speed_kph = 0.0
        for _, _, data in self._G.edges(data=True):
            sp = data.get("speed_kph")
            if sp is not None and sp > max_speed_kph:
                max_speed_kph = float(sp)
        if max_speed_kph <= 0:
            max_speed_kph = 20.0  # graceful fallback for edge-attribute-less toy graphs
        self._max_speed_mps = max_speed_kph * 1000.0 / 3600.0
        self._h_target_y = self._G.nodes[self._target]["y"]
        self._h_target_x = self._G.nodes[self._target]["x"]
        self._h_cache: dict[int, float] = {}

    def _h(self, v: int) -> float:
        cached = self._h_cache.get(v)
        if cached is not None:
            return cached
        vy = self._G.nodes[v]["y"]
        vx = self._G.nodes[v]["x"]
        dist_m = _haversine_m(vy, vx, self._h_target_y, self._h_target_x)
        val = dist_m / self._max_speed_mps
        self._h_cache[v] = val
        return val

    def _build_adjacency(self) -> None:
        """out_edges[u] = list of (v, k); all_edges = every (u, v, k) with
        a snapshot travel_time entry."""
        out_edges: dict[int, list[tuple[int, int]]] = {}
        all_edges: list[EdgeKey] = []
        for u, v, k in self._G.edges(keys=True):
            if (u, v, k) not in self._snapshot.travel_time:
                continue
            out_edges.setdefault(u, []).append((v, k))
            all_edges.append((u, v, k))
        self._out_edges = out_edges
        self._all_edges = all_edges

    def _update_reachability(self) -> None:
        """Precompute the set of nodes from which the target is reachable
        at all (reverse BFS on the current graph). This is NOT ant-level
        repair: it is a fixed, once-per-graph constraint on the candidate
        set every ant draws from, applied because on a real street network
        (mean out-degree ~2.5, ~19% of nodes with out-degree 1) an
        unguided tabu random-walk with eta = 1/travel_time alone dead-ends
        on essentially every ant over paths tens of hops long -- pruning
        provably-dead moves before they're taken is a legitimate
        restriction of the action space, whereas "discard, don't repair"
        (see module docstring / CLAUDE.md) is about ants that DO dead-end
        anyway (tabu has locked them out of every remaining reachable
        neighbour) or exceed max_steps; both still apply on top of this."""
        try:
            rev = self._G.reverse(copy=False)
            self._can_reach_target = set(
                nx.single_source_shortest_path_length(rev, self._target).keys()
            )
        except nx.NodeNotFound:
            self._can_reach_target = set()

    # -- construction -----------------------------------------------------

    def _weighted_candidates(
        self, node: int, visited: set[int]
    ) -> list[list]:
        """[v, k, weight] for every out-edge of `node` whose target is
        neither already on the current path nor provably unable to reach
        the destination. A mutable list (not a tuple) because
        `_construct_ant` pops entries out of it in place as they're tried,
        so a backtrack into this node resumes from the untried remainder
        rather than re-trying an already-failed branch."""
        out = []
        for v, k in self._out_edges.get(node, []):
            if v in visited or v not in self._can_reach_target:
                continue
            tau = self._pheromone.get((node, v, k), self._tau_min)
            tt = self._snapshot.travel_time.get((node, v, k))
            if tt is None:
                continue
            h = self._h(v) if self._use_goal_heuristic else 0.0
            eta = 1.0 / (tt + h)
            out.append([v, k, (tau ** self._alpha) * (eta ** self._beta)])
        return out

    def _pick_and_remove(self, candidates: list[list]) -> tuple[int, int]:
        """Weighted-random pick of one (v, k) from `candidates`, removing
        it from the list in place (sampling without replacement -- a
        backtrack into this same frame will not retry it)."""
        total = sum(w for _, _, w in candidates)
        if total <= 0.0:
            idx = self._rng.randrange(len(candidates))
        else:
            r = self._rng.random() * total
            acc = 0.0
            idx = len(candidates) - 1
            for i, (_v, _k, w) in enumerate(candidates):
                acc += w
                if r <= acc:
                    idx = i
                    break
        v, k, _w = candidates.pop(idx)
        return v, k

    def _construct_ant(self) -> tuple[list[int], float, list[EdgeKey]] | None:
        """Build one ant's path from source to target via depth-first
        construction WITH backtracking: on a dead end (no untried,
        not-yet-visited, still-can-reach-target out-edge), pop back to the
        last node that still has an untried alternative and try that
        instead, rather than discarding the ant immediately.

        This is a deliberate strengthening of pure feed-forward tabu
        construction (see module docstring): verified empirically, plain
        forward-only construction on this graph has essentially a 0%
        completion rate, because real intersections routinely lead into
        short residential loops/cul-de-sacs with only two out-edges, both
        already on the path, which is an unrecoverable-by-tabu-choice-alone
        dead end that has nothing to do with beta/rho tuning. Backtracking
        is bounded by a total operation budget
        (max_steps * backtrack_multiplier); an ant that exhausts that
        budget, or backtracks all the way to the source with no untried
        branch left anywhere, is discarded -- not repaired. `max_steps`
        itself still bounds the FORWARD path length only (an ant that would
        need to go forward past max_steps hops is forced to backtrack
        instead of continuing).

        Returns (node_path, cost, edge_path), or None if discarded.
        """
        target = self._target
        path = [self._source]
        edge_path: list[EdgeKey] = []
        visited = {self._source}
        frames: list[list] = []
        op_budget = self._max_steps * self._backtrack_multiplier
        ops = 0

        candidates = self._weighted_candidates(self._source, visited)

        while True:
            ops += 1
            if ops > op_budget:
                return None

            if path[-1] == target:
                break

            if len(path) - 1 >= self._max_steps:
                candidates = []  # forward budget exhausted -> force backtrack

            if not candidates:
                if len(path) == 1:
                    return None  # exhausted every branch back to the source
                popped = path.pop()
                edge_path.pop()
                visited.discard(popped)
                candidates = frames.pop()
                continue

            v, k = self._pick_and_remove(candidates)
            frames.append(candidates)  # resume point if we backtrack later
            edge_path.append((path[-1], v, k))
            path.append(v)
            visited.add(v)
            candidates = self._weighted_candidates(v, visited)

        cost = self._snapshot.cost_of_path(self._G, path)
        return path, cost, edge_path

    def _branching_factor(self) -> float:
        threshold = self._tau_min + self._lambda_branching * (self._tau_max - self._tau_min)
        counts = []
        for u, neighbours in self._out_edges.items():
            if not neighbours:
                continue
            n_sig = sum(
                1
                for (v, k) in neighbours
                if self._pheromone.get((u, v, k), self._tau_min) > threshold
            )
            counts.append(n_sig)
        if not counts:
            return 0.0
        return sum(counts) / len(counts)

    def step(self) -> float | None:
        self._iterations += 1

        ants_results = []
        for _ in range(self._n_ants):
            result = self._construct_ant()
            if result is not None:
                ants_results.append(result)

        # Evaporation applies every iteration regardless of ant success.
        for e in list(self._pheromone.keys()):
            self._pheromone[e] = max(self._tau_min, self._pheromone[e] * (1.0 - self._rho))

        if not ants_results:
            self._empty_iterations += 1
            logger.info(
                "ACORouter: iteration %d produced no valid ant (all dead-ended "
                "or exceeded max_steps=%d); stall counter reset, continuing.",
                self._iterations,
                self._max_steps,
            )
            self._stall_count = 0
            self._prev_iteration_best_path = None
            return self._best_cost

        iter_best_path, iter_best_cost, iter_best_edges = min(
            ants_results, key=lambda r: r[1]
        )

        # Deposit on the iteration-best path only.
        delta = self._Q / iter_best_cost
        for e in iter_best_edges:
            if e not in self._pheromone:
                self._pheromone[e] = self._tau_min
            self._pheromone[e] = min(self._tau_max, self._pheromone[e] + delta)

        # Update global best.
        if self._best_cost is None or iter_best_cost < self._best_cost:
            self._best_path = iter_best_path
            self._best_cost = iter_best_cost
            self._best_edges = iter_best_edges

        # Dynamic tau bounds (standard MMAS), keyed off the global best.
        self._tau_max = 1.0 / (self._rho * self._best_cost)
        self._tau_min = self._tau_max / self._tau_min_ratio
        for e in list(self._pheromone.keys()):
            v = self._pheromone[e]
            if v > self._tau_max:
                self._pheromone[e] = self._tau_max
            elif v < self._tau_min:
                self._pheromone[e] = self._tau_min

        # Stall counter: compare THIS iteration's best path to the
        # previous iteration's best path (not to the global best).
        cur_tuple = tuple(iter_best_path)
        if self._prev_iteration_best_path == cur_tuple:
            self._stall_count += 1
        else:
            self._stall_count = 1
        self._prev_iteration_best_path = cur_tuple

        branching = self._branching_factor()
        self._converged = (
            self._stall_count >= self._k_stall and branching <= self._branching_threshold
        )

        return self._best_cost

    def best_path(self) -> list[int] | None:
        return self._best_path

    def best_cost(self) -> float | None:
        return self._best_cost

    def converged(self) -> bool:
        return self._converged

    def apply_disruption(self, removed_edges: list[tuple[int, int, int]], warm: bool) -> None:
        removed = set(removed_edges)

        if not warm:
            G2 = self._G.copy()
            G2.remove_edges_from(list(removed))
            self._G = G2
            self._build_adjacency()
            self._update_reachability()
            self._pheromone = {e: self._tau_max_init for e in self._all_edges}
            self._tau_max = self._tau_max_init
            self._tau_min = self._tau_max_init / self._tau_min_ratio
            self._best_path = None
            self._best_cost = None
            self._best_edges = None
            self._prev_iteration_best_path = None
            self._stall_count = 0
            self._iterations = 0
            self._converged = False
            self._empty_iterations = 0
            return

        # warm=True: retain pheromone, only invalidate the break.
        G2 = self._G.copy()
        G2.remove_edges_from(list(removed))
        self._G = G2
        self._build_adjacency()
        self._update_reachability()

        for e in removed:
            self._pheromone.pop(e, None)

        if self.config["disruption_strategy"] == "local_reset":
            broken_nodes: set[int] = set()
            for u, v, _k in removed:
                broken_nodes.add(u)
                broken_nodes.add(v)
            undirected = self._G.to_undirected(as_view=True)
            near_nodes: set[int] = set()
            for n in broken_nodes:
                if n not in undirected:
                    continue
                lengths = nx.single_source_shortest_path_length(
                    undirected, n, cutoff=self._disruption_n_hop
                )
                near_nodes.update(lengths.keys())
            for u, v, k in self._all_edges:
                if u in near_nodes or v in near_nodes:
                    self._pheromone[(u, v, k)] = self._tau_max

        # Invalidate the current best if it used a severed edge.
        if self._best_edges is not None and any(e in removed for e in self._best_edges):
            self._best_path = None
            self._best_cost = None
            self._best_edges = None

        # The old iteration-best-path streak no longer means anything
        # against the disrupted graph.
        self._prev_iteration_best_path = None
        self._stall_count = 0
        self._converged = False

    def state(self) -> dict[str, Any]:
        return {
            "pheromone": dict(self._pheromone),
            "tau_max": self._tau_max,
            "tau_min": self._tau_min,
            "best_path": list(self._best_path) if self._best_path is not None else None,
            "best_cost": self._best_cost,
            "best_edges": list(self._best_edges) if self._best_edges is not None else None,
            "prev_iteration_best_path": self._prev_iteration_best_path,
            "stall_count": self._stall_count,
            "iterations": self._iterations,
            "converged": self._converged,
            "empty_iterations": self._empty_iterations,
            "max_steps": self._max_steps,
            "diameter_estimate": self._diameter_estimate,
            "rng_state": self._rng.getstate(),
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self._pheromone = dict(state["pheromone"])
        self._tau_max = state["tau_max"]
        self._tau_min = state["tau_min"]
        self._best_path = list(state["best_path"]) if state["best_path"] is not None else None
        self._best_cost = state["best_cost"]
        self._best_edges = (
            list(state["best_edges"]) if state["best_edges"] is not None else None
        )
        self._prev_iteration_best_path = state["prev_iteration_best_path"]
        self._stall_count = state["stall_count"]
        self._iterations = state["iterations"]
        self._converged = state["converged"]
        self._empty_iterations = state["empty_iterations"]
        self._max_steps = state["max_steps"]
        self._diameter_estimate = state["diameter_estimate"]
        self._rng.setstate(state["rng_state"])
