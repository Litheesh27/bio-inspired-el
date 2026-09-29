"""Ant Colony Optimization router (MAX-MIN Ant System).

IMPLEMENTED IN: Phase 4. VECTORIZED IN: Phase 4b (performance only -- see
"Phase 4b: vectorization" below; the algorithm itself is unchanged from
Phase 4).

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

------------------------------------------------------------------------
Phase 4b: performance (NOT a behaviour change -- bit-for-bit identical)
------------------------------------------------------------------------
The Phase 4 implementation cost ~0.6-0.7 s/iteration on the real graph
(2,856 nodes / 7,015 edges, n_ants=40), which put the Phase 8 ACO sweep in
the hundreds of serial hours. The cost was never the MMAS model: it was
per-candidate bookkeeping. An ant performs ~5,000 construction operations
(forward steps + backtracks) and the colony performs ~200,000 of them per
iteration, so everything on that path is multiplied by 200,000 while
everything outside it is essentially free.

Two things were fixed:

1. DENSE INDEXING, BUILT ONCE. Node ids and (u, v, k) edge keys are mapped
   to dense integers in `_rebuild_arrays`, with a CSR-style
   node -> out-edge structure, per-edge eta^beta precomputed, and edges
   whose head cannot reach the target filtered out up front. The
   construction loop then does list indexing on ints instead of dict
   lookups on tuples, and `visited` is a `bytearray` indexed by node
   index instead of a set of node ids.

2. NUMPY ONLY WHERE THE ARRAY IS BIG. Evaporation, deposit, the tau
   clip and the branching factor run over all ~7,000 edges at once and
   are genuinely vectorized (`_pheromone_arr` is the source of truth).
   The per-candidate path is the opposite case: mean out-degree here is
   2.46 and 19% of nodes have out-degree 1, so a candidate set is a 1-3
   element sequence, and an earlier attempt that used numpy there
   (fancy-indexing + cumsum + searchsorted + an `np.ones` mask per
   candidate set) was measured 5.3x SLOWER than plain Python scalars --
   numpy's per-call overhead dwarfs arithmetic at that size. That path is
   therefore plain Python lists/floats, fed from `_cand_base`, which is
   re-derived from the numpy array once per iteration (~7,000 conversions against ~110,000 candidate evaluations).

Nothing about the search changed. The RNG is still `random.Random(seed)`,
drawn once per pick in the same order, from candidate sets built in the
same order with bit-for-bit identical weights -- so this implementation
reproduces the Phase 4 implementation EXACTLY: same paths, same costs,
same iteration counts, same ant completion rates, same operations per ant,
at every seed tested. (That is stronger than the Phase 4b spec required --
it permitted an RNG-stream change and therefore only statistical
equivalence -- and it is what makes the tuning record and the
stretch-1.157 plateau finding in docs/tuning/aco_sweep.md still apply
verbatim to this code.) Measured speedups, the operations-per-ant
accounting, and the honest note about what was NOT achieved are in that
file's "Phase 4b performance" section.
"""

from __future__ import annotations

import logging
import random
from typing import Any

import networkx as nx
import numpy as np

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


def _haversine_m(y1, x1, y2, x2):
    """Great-circle distance in meters, vectorized (y1/x1 may be numpy
    arrays; y2/x2 are the scalar target coordinates). Duplicated from
    algorithms/classical.py (a shared module we must not edit per the
    Phase 4 coordination constraints) rather than imported, to keep aco.py
    self-contained within its own touched-files list."""
    R = 6371000.0
    phi1 = np.radians(y1)
    phi2 = np.radians(y2)
    dphi = np.radians(y2 - y1)
    dlambda = np.radians(x2 - x1)
    a = np.sin(dphi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2) ** 2
    return 2 * R * np.arcsin(np.sqrt(a))


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


# Empty-candidate sentinel reused everywhere a node has no viable out-edge,
# so `_weighted_candidates` never has to allocate fresh empty arrays.
_EMPTY_I = np.empty(0, dtype=np.int64)
_EMPTY_F = np.empty(0, dtype=np.float64)


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

        self._rebuild_arrays()

        self._tau_max = self._tau_max_init
        self._tau_min = self._tau_max_init / self._tau_min_ratio
        self._pheromone_arr = np.full(self._n_edges, self._tau_max, dtype=np.float64)
        self._refresh_weight_cache()

        # Construction-work counters (diagnostics only; two integer
        # increments per ant). `ops` is the same quantity `_construct_ant`
        # bounds with op_budget: forward steps + backtracks.
        self._ops_total = 0
        self._ants_total = 0
        self._ants_completed = 0

        self._best_path: list[int] | None = None
        self._best_cost: float | None = None
        self._best_edges: list[EdgeKey] | None = None
        self._prev_iteration_best_path: tuple[int, ...] | None = None
        self._stall_count = 0
        self._iterations = 0
        self._converged = False
        self._empty_iterations = 0
        self._done_steps = 0

    # -- dense array bookkeeping ------------------------------------------

    def _rebuild_arrays(self) -> None:
        """(Re)builds every dense array derived from `self._G` +
        `self._snapshot`: node index <-> node id, edge index <-> (u,v,k),
        the CSR node->out-edge structure, per-edge travel_time/eta, and the
        reachability mask. Called from `prepare` and from
        `apply_disruption` (both warm and cold) -- never touches
        `self._pheromone_arr`, which callers manage themselves since its
        treatment differs between prepare (fresh) and warm disruption
        (carried over by edge key).
        """
        G = self._G

        node_ids = list(G.nodes())
        node_index: dict[int, int] = {nid: i for i, nid in enumerate(node_ids)}
        n_nodes = len(node_ids)
        node_y = np.empty(n_nodes, dtype=np.float64)
        node_x = np.empty(n_nodes, dtype=np.float64)
        for i, nid in enumerate(node_ids):
            data = G.nodes[nid]
            node_y[i] = data["y"]
            node_x[i] = data["x"]

        self._node_ids = node_ids
        self._node_index = node_index
        self._n_nodes = n_nodes
        self._source_idx = node_index[self._source]
        self._target_idx = node_index[self._target]

        # Group edges by source node (dict preserves insertion order, same
        # traversal order as the old `_out_edges` dict) so the CSR layout
        # matches the order edges were previously offered to an ant.
        per_node_edges: dict[int, list[int]] = {}
        edge_keys: list[EdgeKey] = []
        edge_v = []
        edge_tt = []
        travel_time = self._snapshot.travel_time
        for u, v, k in G.edges(keys=True):
            key = (u, v, k)
            tt = travel_time.get(key)
            if tt is None:
                continue
            eidx = len(edge_keys)
            edge_keys.append(key)
            edge_v.append(node_index[v])
            edge_tt.append(tt)
            per_node_edges.setdefault(node_index[u], []).append(eidx)

        n_edges = len(edge_keys)
        self._edge_keys = edge_keys
        self._edge_index = {key: i for i, key in enumerate(edge_keys)}
        self._n_edges = n_edges
        self._edge_v = np.array(edge_v, dtype=np.int64) if n_edges else _EMPTY_I
        edge_tt_arr = np.array(edge_tt, dtype=np.float64) if n_edges else _EMPTY_F

        indptr = np.zeros(n_nodes + 1, dtype=np.int64)
        indices = np.empty(n_edges, dtype=np.int64)
        pos = 0
        for node_idx in range(n_nodes):
            eids = per_node_edges.get(node_idx)
            indptr[node_idx] = pos
            if eids:
                n = len(eids)
                indices[pos : pos + n] = eids
                pos += n
        indptr[n_nodes] = pos
        self._csr_indptr = indptr
        self._csr_indices = indices

        # Reachability: nodes from which the target is reachable at all
        # (reverse BFS). Fixed, once-per-graph pruning of the candidate
        # set -- see module docstring / Phase 4 rationale; not ant-level
        # repair.
        try:
            rev = G.reverse(copy=False)
            can_reach_ids = set(
                nx.single_source_shortest_path_length(rev, self._target).keys()
            )
        except nx.NodeNotFound:
            can_reach_ids = set()
        can_reach_mask = np.zeros(n_nodes, dtype=bool)
        for nid in can_reach_ids:
            can_reach_mask[node_index[nid]] = True
        self._can_reach_target = can_reach_ids  # kept for compatibility/introspection
        self._edge_valid_reach = (
            can_reach_mask[self._edge_v] if n_edges else _EMPTY_I.astype(bool)
        )

        # eta (and eta^beta, precomputed once): depends only on the
        # snapshot's travel_time and, in goal-heuristic mode, on h(v) --
        # neither varies step to step, so this is computed once per
        # prepare/disruption rather than per ant per hop.
        if self._use_goal_heuristic and n_edges:
            target_y = self._G.nodes[self._target]["y"]
            target_x = self._G.nodes[self._target]["x"]
            max_speed_kph = 0.0
            for _, _, data in G.edges(data=True):
                sp = data.get("speed_kph")
                if sp is not None and sp > max_speed_kph:
                    max_speed_kph = float(sp)
            if max_speed_kph <= 0:
                max_speed_kph = 20.0  # graceful fallback for toy graphs
            max_speed_mps = max_speed_kph * 1000.0 / 3600.0
            node_h = _haversine_m(node_y, node_x, target_y, target_x) / max_speed_mps
            edge_h = node_h[self._edge_v]
            eta = 1.0 / (edge_tt_arr + edge_h)
        elif n_edges:
            eta = 1.0 / edge_tt_arr
        else:
            eta = _EMPTY_F

        self._etapow = eta**self._beta if n_edges else _EMPTY_F
        self._edge_tt = edge_tt_arr

        # Plain-Python mirrors of every array the per-candidate hot path
        # touches. Mean out-degree on this graph is 2.46 (19% of nodes have
        # out-degree 1), so a candidate set is a 1-3 element sequence:
        # numpy's per-call dispatch overhead on arrays that small is an
        # order of magnitude larger than the arithmetic it performs, and
        # profiling showed fancy-indexing/cumsum/searchsorted/np.ones over
        # 2-3 element arrays dominating the whole solve. Scalar Python
        # arithmetic over list slices is strictly faster at this size.
        # numpy is retained where the array really is large -- the
        # per-iteration evaporation/deposit/clip over all ~7,000 edges (see
        # `step`), which is one vectorized pass instead of 7,000 Python
        # loop iterations. `_pheromone_arr` stays the source of truth and
        # the Python-side weight cache is re-derived from it once per
        # iteration (`_refresh_weight_cache`).
        indptr_l = indptr.tolist()
        indices_l = indices.tolist()
        edge_v_l = self._edge_v.tolist() if n_edges else []
        valid_l = self._edge_valid_reach.tolist() if n_edges else []
        self._etapow_l = self._etapow.tolist() if n_edges else []
        # Edges whose head is provably unable to reach the target are
        # filtered here once, so the per-candidate loop never sees them:
        # `_csr_reachable[node] = [(edge_id, head_node_idx), ...]`.
        self._csr_reachable = [
            [
                (eid, edge_v_l[eid])
                for eid in indices_l[indptr_l[i] : indptr_l[i + 1]]
                if valid_l[eid]
            ]
            for i in range(n_nodes)
        ]
        self._cand_base: list[list[tuple[int, int, float]]] = []

    def _refresh_weight_cache(self) -> None:
        """Re-derive the per-node candidate-weight cache from the numpy
        pheromone array (the source of truth).

        The transition weight tau(e)^alpha * eta(e)^beta depends only on
        the pheromone, which changes exactly once per iteration (in
        `step`), so within one iteration every ant computes the SAME weight
        for the same edge. Computing all ~7,000 of them once per iteration
        costs ~2 ms; computing them lazily cost ~100,000 multiplications
        and index lookups per iteration. The cached value is bit-for-bit
        the same product as the lazy one, so the ants' choices are
        unchanged.

        `_cand_base[node] = [(edge_id, head_node_idx, weight), ...]`,
        already filtered to edges whose head can reach the target; the
        per-ant `visited` filter is all that remains to apply."""
        tau = self._pheromone_arr.tolist()
        etapow = self._etapow_l
        if self._alpha == 1.0:
            self._cand_base = [
                [(eid, v, tau[eid] * etapow[eid]) for eid, v in per_node]
                for per_node in self._csr_reachable
            ]
        else:
            alpha = self._alpha
            self._cand_base = [
                [(eid, v, (tau[eid] ** alpha) * etapow[eid]) for eid, v in per_node]
                for per_node in self._csr_reachable
            ]

    def _pheromone_dict(self) -> dict[EdgeKey, float]:
        return {key: float(v) for key, v in zip(self._edge_keys, self._pheromone_arr.tolist())}

    # -- construction -----------------------------------------------------

    def _weighted_candidates(self, node_idx: int, visited: bytearray) -> list:
        """`[(edge_id, head_node_idx, weight), ...]` for every out-edge of
        `node_idx` whose head is neither already on the ant's path nor
        provably unable to reach the destination.

        A mutable list of immutable entries, because `_pick_and_remove`
        pops entries out of it
        in place, so a backtrack into this node resumes from the untried
        remainder rather than re-trying an already-failed branch (sampling
        without replacement).

        Pure Python scalars by design -- see `_rebuild_arrays` for why
        numpy is the wrong tool on a 1-3 element candidate set, and
        `_refresh_weight_cache` for why the weights themselves are
        cached per iteration rather than recomputed per visit.

        NOTE: `_construct_ant` inlines this body rather than calling it
        (two Python calls per hop, ~110,000 hops per iteration, was
        measurably more expensive than the work itself). This method is
        the readable reference form of that inlined code and is what
        `test_aco_hot_loop_matches_reference_helpers` checks the inlined
        loop against.
        """
        return [c for c in self._cand_base[node_idx] if not visited[c[1]]]

    def _pick_and_remove(self, candidates: list) -> tuple[int, int]:
        """Weighted-random pick of one (edge_id, head_node_idx) from
        `candidates`, removing it from the list in place (sampling without
        replacement -- a backtrack into this same frame will not retry
        it). Roulette selection: one running accumulation, one RNG draw.

        NOTE: inlined into `_construct_ant` for the same reason as
        `_weighted_candidates`; kept as the reference form, and tested
        against the inlined loop.
        """
        n = len(candidates)
        total = 0.0
        for c in candidates:
            total += c[2]
        if total <= 0.0:
            idx = self._rng.randrange(n)
        else:
            r = self._rng.random() * total
            acc = 0.0
            idx = n - 1
            for i, c in enumerate(candidates):
                acc += c[2]
                if r <= acc:
                    idx = i
                    break
        eid, v, _w = candidates.pop(idx)
        return eid, v

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

        Node/edge bookkeeping is by dense integer index internally (list
        indexing, no dict/tuple hashing); only the returned path/edge_path
        are converted back to node ids / (u,v,k) edge keys.

        `_weighted_candidates` and `_pick_and_remove` are INLINED below
        rather than called. This loop runs ~200,000 times per iteration
        (~110,000 of those doing a full candidate build + weighted pick),
        and at that call count the Python call overhead alone was a
        double-digit percentage of the whole solve. The two methods remain
        as the readable reference form of the inlined code, and
        tests/test_aco.py checks that the inlined loop and the reference
        helpers make identical choices from identical state.

        Returns (node_path, cost, edge_path), or None if discarded.
        """
        source_idx = self._source_idx
        target_idx = self._target_idx
        path_idx = [source_idx]
        edge_path_ids: list[int] = []
        visited = bytearray(self._n_nodes)
        visited[source_idx] = 1
        frames: list[list] = []
        max_steps = self._max_steps
        op_budget = max_steps * self._backtrack_multiplier
        ops = 0
        depth = 0  # == len(path_idx) - 1, maintained rather than recomputed
        last_idx = source_idx
        cand_base = self._cand_base
        rand = self._rng.random
        randrange = self._rng.randrange

        candidates = [c for c in cand_base[source_idx] if not visited[c[1]]]

        while True:
            ops += 1
            if ops > op_budget:
                self._ops_total += ops
                self._ants_total += 1
                return None

            if last_idx == target_idx:
                break

            if depth >= max_steps:
                candidates = []  # forward budget exhausted -> force backtrack

            if not candidates:
                if depth == 0:
                    self._ops_total += ops
                    self._ants_total += 1
                    return None  # exhausted every branch back to the source
                visited[path_idx.pop()] = 0
                edge_path_ids.pop()
                depth -= 1
                last_idx = path_idx[-1]
                candidates = frames.pop()
                continue

            # -- inlined _pick_and_remove ------------------------------
            n = len(candidates)
            if n == 1:
                # 61% of picks on this graph (mean out-degree 2.46, minus
                # the visited/reachability filters). The RNG draw is still
                # consumed, because the general branch below consumes
                # exactly one draw for any candidate set with positive
                # total weight and always selects index 0 when there is
                # only one -- skipping the draw would silently shift the
                # random stream.
                if candidates[0][2] > 0.0:
                    rand()
                    idx = 0
                else:
                    idx = randrange(1)
            else:
                total = 0.0
                for c in candidates:
                    total += c[2]
                if total <= 0.0:
                    idx = randrange(n)
                else:
                    r = rand() * total
                    acc = 0.0
                    idx = n - 1
                    for i, c in enumerate(candidates):
                        acc += c[2]
                        if r <= acc:
                            idx = i
                            break
            eid, v_idx, _w = candidates.pop(idx)
            # ----------------------------------------------------------

            frames.append(candidates)  # resume point if we backtrack later
            edge_path_ids.append(eid)
            path_idx.append(v_idx)
            visited[v_idx] = 1
            depth += 1
            last_idx = v_idx
            # -- inlined _weighted_candidates --------------------------
            candidates = [c for c in cand_base[v_idx] if not visited[c[1]]]

        self._ops_total += ops
        self._ants_total += 1
        self._ants_completed += 1
        node_ids = self._node_ids
        path = [node_ids[i] for i in path_idx]
        edge_path = [self._edge_keys[e] for e in edge_path_ids]
        cost = self._snapshot.cost_of_path(self._G, path)
        return path, cost, edge_path

    def _branching_factor(self) -> float:
        if self._n_edges == 0:
            return 0.0
        threshold = self._tau_min + self._lambda_branching * (self._tau_max - self._tau_min)
        outdeg = np.diff(self._csr_indptr)
        has_out = outdeg > 0
        if not has_out.any():
            return 0.0
        significant = self._pheromone_arr > threshold
        # edge -> source-node index, derived from the CSR structure.
        edge_u = np.repeat(np.arange(self._n_nodes), outdeg)
        counts = np.bincount(edge_u[significant], minlength=self._n_nodes)
        return float(counts[has_out].mean())

    def step(self) -> float | None:
        self._iterations += 1

        ants_results = []
        for _ in range(self._n_ants):
            result = self._construct_ant()
            if result is not None:
                ants_results.append(result)

        # Evaporation applies every iteration regardless of ant success.
        if self._n_edges:
            self._pheromone_arr *= 1.0 - self._rho
            np.maximum(self._pheromone_arr, self._tau_min, out=self._pheromone_arr)

        if not ants_results:
            self._refresh_weight_cache()
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
        iter_best_eids = np.array(
            [self._edge_index[e] for e in iter_best_edges], dtype=np.int64
        )
        if iter_best_eids.size:
            np.add.at(self._pheromone_arr, iter_best_eids, delta)
            np.minimum(self._pheromone_arr, self._tau_max, out=self._pheromone_arr)

        # Update global best.
        if self._best_cost is None or iter_best_cost < self._best_cost:
            self._best_path = iter_best_path
            self._best_cost = iter_best_cost
            self._best_edges = iter_best_edges

        # Dynamic tau bounds (standard MMAS), keyed off the global best.
        self._tau_max = 1.0 / (self._rho * self._best_cost)
        self._tau_min = self._tau_max / self._tau_min_ratio
        if self._n_edges:
            np.clip(self._pheromone_arr, self._tau_min, self._tau_max, out=self._pheromone_arr)
        self._refresh_weight_cache()

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
            self._rebuild_arrays()
            self._pheromone_arr = np.full(self._n_edges, self._tau_max_init, dtype=np.float64)
            self._refresh_weight_cache()
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
        old_pheromone = self._pheromone_dict()
        for e in removed:
            old_pheromone.pop(e, None)

        G2 = self._G.copy()
        G2.remove_edges_from(list(removed))
        self._G = G2
        self._rebuild_arrays()

        new_pheromone = np.array(
            [old_pheromone.get(key, self._tau_min) for key in self._edge_keys],
            dtype=np.float64,
        ) if self._n_edges else np.empty(0, dtype=np.float64)

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
            if near_nodes:
                for i, (u, v, k) in enumerate(self._edge_keys):
                    if u in near_nodes or v in near_nodes:
                        new_pheromone[i] = self._tau_max

        self._pheromone_arr = new_pheromone
        self._refresh_weight_cache()

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
            "pheromone": self._pheromone_dict(),
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
        pheromone_dict = dict(state["pheromone"])
        self._pheromone_arr = np.array(
            [pheromone_dict.get(key, self._tau_min) for key in self._edge_keys],
            dtype=np.float64,
        ) if self._n_edges else np.empty(0, dtype=np.float64)
        self._refresh_weight_cache()
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
