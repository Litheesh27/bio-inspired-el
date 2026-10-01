"""Physarum polycephalum tube-reinforcement router.

IMPLEMENTED IN: Phase 5.

Implements the Tero et al. adaptive-conductivity model, plus the simpler
Bonifaci ``D <- |Q|`` variant as a config switch (``configs/physarum.yaml``).

DIRECTED vs. UNDIRECTED -- the single most consequential design decision
in this module
----------------------------------------------------------------------------
The classical Physarum/Tero model is a physical tube network: it has no
notion of one-way flow, only a scalar conductivity per *undirected* tube and
a pressure field that determines flow direction each iteration. Our graph is
a directed MultiDiGraph with parallel edges (opposite carriageways, split
ways, occasional genuine one-way streets).

Decision: **the adaptive model itself is built on an undirected projection**.
Every unordered node pair ``{u, v}`` connected by at least one directed edge
in either direction (any parallel key) becomes exactly one undirected model
edge, carrying one conductivity ``D`` and one cost ``L`` (the minimum
congested travel time over every directed parallel edge between the pair, in
either direction -- consistent with this project's existing "minimum
parallel edge" convention in ``TrafficSnapshot``). This is what makes "solve
the weighted Laplacian for pressures" well-posed at all: Kirchhoff's law and
a single scalar conductance per edge are only meaningful on an undirected
graph.

Justification: this is what Tero et al.'s model *is* -- a physical
transport medium, not a directed queueing network -- and the alternative
(maintaining a separate D_uv and D_vu as if they were independent tubes)
has no basis in the biology being modeled and would double the free
parameters for no benefit. The known cost of this choice is that the
pressure field alone could, in principle, imply a flow direction along an
edge that only exists as a one-way street in the real graph (e.g. pressure
wants to push traffic against a one-way's actual direction). We do not let
this corrupt the output: path extraction (below) never walks along a
direction that lacks a real directed edge in the original MultiDiGraph, so
the *extracted path is always a valid directed path* regardless of what the
undirected relaxation's pressure field would prefer. The undirected model
only supplies the conductivity/pressure landscape that ranks the *directed*
choices actually available at each node.

MODEL
----------------------------------------------------------------------------
Per undirected edge ``i = {u, v}``: conductivity ``D_i``, cost ``L_i``
(current congested travel time via the active TrafficSnapshot), conductance
``w_i = D_i / L_i``.

Each iteration:
 1. Solve the weighted-Laplacian system ``L p = b`` for node pressures, with
    ``+I0`` injected at the source and ``-I0`` at the sink. The Laplacian is
    singular (rows sum to zero), so it is grounded by fixing ``p_sink = 0``:
    the sink's row/column is dropped entirely before assembly (never merely
    zeroed-and-reinserted after the fact), the reduced system is solved with
    ``scipy.sparse.linalg.spsolve`` on CSR, and ``p_sink = 0`` is reinserted
    afterward. The system is assembled over the SOURCE'S CONNECTED COMPONENT
    only -- see ACTIVE COMPONENT below; grounding one node grounds one
    component, so a graph with several components needs the restriction to be
    well-posed at all.

ACTIVE COMPONENT (Phase 8a) -- why the Laplacian is not built over every node
----------------------------------------------------------------------------
``p_sink = 0`` grounds exactly ONE component. Any node the source cannot
reach through surviving tubes contributes a row that is either exactly zero
(an isolated node) or part of an ungrounded floating block whose rows sum to
zero -- in both cases the reduced matrix is EXACTLY SINGULAR, ``spsolve``
emits ``MatrixRankWarning`` and returns all-NaN pressures, and from then on
``Q``, ``D`` and every pressure in the run are NaN forever.

This is not a hypothetical. Phase 7's disruption scenarios guarantee only
that SOURCE and TARGET stay connected, never that the whole graph does, and
nodes are never deleted from the graph -- only edges. Measured over the full
420-cell Phase 8 matrix (360 disrupted cells), 82 of them (22.8%) strand at
least one node:

    S1  0/60   S2  0/60   S3  2/60   S4  2/60   S5 18/60   S6 60/60

``S6`` ("intersection closure") removes every edge incident to one path node,
so it isolates that node BY CONSTRUCTION and degenerates on 100% of its
cells, on the FIRST post-disruption iteration. ``S3``/``S4``/``S5`` instead
strand whole 2-8 node components with live internal tubes. Before the fix,
the NaN pressures silently fell through path extraction to
``full_graph_fallback`` (plain Dijkstra), so these runs were scored, were not
recorded as failed, and reported a plausible stretch computed from a path the
model had no part in choosing.

Treatment: the per-iteration system is assembled over the connected component
of the source in the undirected projection of the *surviving* graph, and only
over tubes with both endpoints inside it. This is the mathematically right
model, not a numerical patch -- Physarum is a physical transport medium and
cannot flow into a region it cannot reach, so the pressure field there is
genuinely undefined (any constant satisfies the equations, which is exactly
what the singularity was reporting). Pressures outside the component are
reported as 0 and their tubes carry ``Q = 0``, so they atrophy under the
normal adaptation rule. Extraction can never walk into them: a directed edge
out of the component would imply an undirected edge out of it. On an
undisrupted, connected graph the active component is the whole graph and the
assembled matrix is bit-for-bit what it was before, so Phase 5's numbers are
unchanged.

The alternative of flooring ``D`` at an epsilon was rejected: it would make
the matrix nonsingular by giving *severed* roads a nonzero conductance,
letting flux leak across a closed intersection -- a model change that
invents transport capacity where the scenario says there is none.

Deliberately NOT silenced: ``_solve_pressures`` turns ``MatrixRankWarning``
into a raised ``PhysarumDegenerateSolve`` and additionally rejects any
non-finite pressure vector. If a solve still degenerates for some reason the
component restriction does not cover, it propagates out of ``step()`` and
Phase 6's collector records the run as ``failed=True`` with that reason
(CLAUDE.md: failures are recorded, never silently dropped). A numerically
degenerate solve must never be scored -- and one case measured during Phase
8a returned p=0 on the stranded block WITHOUT warning, so warning detection
alone was never going to be a sufficient fix.
 2. Flux ``Q_i = w_i * (p_u - p_v)`` (sign convention: positive means flow
    from ``u`` to ``v`` in the edge's stored orientation).
 3. Adapt ``D_i <- D_i + dt * (f(|Q_i|) - D_i)``, ``f(Q) = |Q|^mu / (1 +
    |Q|^mu)`` (Tero), or ``D_i <- |Q_i|`` (Bonifaci), selected by
    ``configs/physarum.yaml: variant``.

PATH EXTRACTION
----------------------------------------------------------------------------
Primary: greedy maximum-flux walk from the source. At the current node
``u``, only *real* directed out-edges ``(u, v, k)`` of the original
MultiDiGraph are considered (this is what guarantees a valid directed path);
each candidate is scored by the undirected model's directed flux
``w_{uv} * (p_u - p_v)`` and the best-scoring, not-yet-visited neighbor is
taken. Fallback (dead end, revisit-only, or step budget exhausted):
Dijkstra on the subgraph of directed edges whose undirected-pair
conductivity exceeds ``theta``; if that subgraph is also disconnected for
this OD pair, Dijkstra on the full graph as a last resort. The method that
actually produced each solve's path is recorded every time (never silently
switched), in ``RouterResult.extras["extraction_method"]`` and tallied in
``extras["extraction_counts"]``, because a silent switch between runs would
make ``stretch`` numbers incomparable across the study.

DISRUPTION
----------------------------------------------------------------------------
``apply_disruption(removed_edges, warm)``:
 - Always: the router's live copy of the MultiDiGraph has the severed edges
   removed (so path extraction can never propose them again); any
   undirected pair left with zero remaining directed edges has its
   conductivity forced to 0 (equivalent to dropping it from the Laplacian,
   since ``w = D / L = 0`` contributes nothing to the assembly either way);
   the running best-path/best-cost tracker is reset (a pre-disruption best
   path may contain a severed edge and must never be reported as still
   current).
 - ``warm=True``: every *surviving* pair's conductivity is left completely
   untouched -- this retained adaptation state is the mechanism the
   project's whole resilience claim rests on.
 - ``warm=False``: every pair's conductivity (survivors included) is
   reinitialized to the configured ``D_init``, i.e. equivalent to calling
   ``prepare()`` again on the post-disruption graph.

CONVERGENCE
----------------------------------------------------------------------------
``converged()`` fires on ``max_i |D_i(t) - D_i(t-1)| < eps`` alone. A
path-stability criterion ("extracted path identical for `stability_k`
consecutive iterations") was tried and rejected during Phase 5 debugging: on
the real network, D decays toward its instantaneous fixed point roughly in
lockstep across every edge during the early relaxation phase (dominated by
the shared ``(1 - dt)`` decay rate), well before flux has actually
redistributed enough to separate the true shortest path from a
plausible-looking alternative -- so the greedy walk can reproduce the same
WRONG path for several consecutive iterations purely as an artifact of that
shared decay, not because the model has converged. This fired early and
wrong on a large fraction of a 30-OD-pair freeflow exit-criterion sample
even when gated behind a coarse secondary delta threshold. ``max|delta D| <
eps`` alone was empirically reliable (exact Dijkstra match once actually
fired) and, at ~2-3 ms/solve on this graph, 300 iterations is cheap enough
that there is no real incentive to risk a cheaper-but-unsound early exit.
``_path_history`` is still tracked and exposed for diagnostics/extras, just
not used to decide convergence.

One model instance is single source-sink; the Phase 8 runner parallelizes
across OD pairs with joblib rather than this module faking multi-commodity
flow.
"""

from __future__ import annotations

import time
import warnings
from pathlib import Path
from typing import Any

import networkx as nx
import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
import yaml

from bioroute.algorithms.base import Router
from bioroute.congestion.snapshot import EdgeKey, TrafficSnapshot

DEFAULT_CONFIG_PATH = "configs/physarum.yaml"


def load_physarum_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load configs/physarum.yaml into a plain dict."""
    with Path(path).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class PhysarumDegenerateSolve(RuntimeError):
    """The per-iteration Laplacian solve was numerically degenerate.

    Raised (never swallowed, never warned-and-continued) when ``spsolve``
    reports a singular matrix or returns a non-finite pressure, so that the
    run surfaces as ``failed=True`` with a stated reason instead of being
    absorbed by the path-extraction fallback and silently scored. See the
    module docstring's ACTIVE COMPONENT section for the degeneracy Phase 8a
    diagnosed and removed; this exception is the backstop for anything it
    does not cover.
    """


class PhysarumUnreachableTarget(PhysarumDegenerateSolve):
    """The target is not in the source's connected component of the surviving
    undirected projection, so there is no pressure field that can describe
    flow between them. Phase 7 guarantees this cannot happen in the sweep
    (and Phase 6 checks the post-disruption Dijkstra reference before the
    recovery solve), so it is a recorded failure rather than a silent one."""


def _pair_key(u: int, v: int) -> tuple[int, int]:
    """Canonical (unordered) key for an undirected model edge. Node ids in
    this project are ints (OSMnx node ids); using min/max keeps a stable,
    hashable, order-independent key without needing frozenset overhead on
    the hot path."""
    return (u, v) if u <= v else (v, u)


class PhysarumRouter(Router):
    """Tero et al. adaptive-conductivity router with a Bonifaci-variant
    switch. See module docstring for the full model, the directed/undirected
    design decision, path extraction, disruption semantics, and convergence
    criteria."""

    name = "physarum"

    def __init__(self, config_path: str | Path = DEFAULT_CONFIG_PATH):
        self._cfg = load_physarum_config(config_path)
        self._variant = str(self._cfg["variant"])
        if self._variant not in ("tero", "bonifaci"):
            raise ValueError(f"Unknown physarum variant {self._variant!r}")
        self._I0 = float(self._cfg["I0"])
        self._D_init = float(self._cfg["D_init"])
        self._dt = float(self._cfg["dt"])
        self._mu = float(self._cfg["mu"])
        self._eps = float(self._cfg["eps"])
        self._stability_k = int(self._cfg["stability_k"])
        self._stability_max_delta = float(self._cfg["stability_max_delta"])
        self._theta = float(self._cfg["theta"])
        self._walk_factor = float(self._cfg["walk_max_steps_factor"])

    # -- Router contract ---------------------------------------------------

    def prepare(
        self,
        G: nx.MultiDiGraph,
        source: int,
        target: int,
        snapshot: TrafficSnapshot,
        seed: int,
    ) -> None:
        if source == target:
            raise ValueError("PhysarumRouter requires source != target")
        self._seed = seed
        self._source = source
        self._target = target
        self._snapshot = snapshot
        self._G = G.copy()
        self._build_model(self._G)
        self._reset_run_state()

    def step(self) -> float | None:
        self._iteration += 1
        w = self._D / self._L

        p = self._solve_pressures(w)
        Q = w * (p[self._edge_u] - p[self._edge_v])
        # Tubes outside the source's active component carry no flow. Their
        # endpoints are both at p = 0 so this is already arithmetically true;
        # it is stated explicitly so `last_flux_by_node`'s Kirchhoff check
        # stays exact and so no future change to the pressure reporting can
        # quietly leak flux into an unreachable region.
        if not self._all_active:
            Q = np.where(self._active_edge, Q, 0.0)

        if self._variant == "tero":
            absQ = np.abs(Q)
            f = np.power(absQ, self._mu) / (1.0 + np.power(absQ, self._mu))
            D_new = self._D + self._dt * (f - self._D)
        else:  # bonifaci
            D_new = np.abs(Q)

        max_delta = float(np.max(np.abs(D_new - self._D))) if D_new.size else 0.0
        self._D = D_new
        self._last_delta = max_delta
        self._last_p = p
        self._last_Q = Q

        path, cost, method = self._extract_path(p, w)
        self._extraction_counts[method] = self._extraction_counts.get(method, 0) + 1
        self._last_method = method

        self._path_history.append(tuple(path) if path is not None else None)
        if len(self._path_history) > self._stability_k:
            self._path_history.pop(0)

        if path is not None and (self._best_cost is None or cost < self._best_cost):
            self._best_path = path
            self._best_cost = cost

        # `_path_history` is retained purely for diagnostics/extras -- NOT
        # used to fire convergence. Empirically (30-OD-pair freeflow
        # debugging run), gating early-stop on "path unchanged for k
        # iterations" alone, or even gated behind a coarse delta-D
        # threshold, locks onto a WRONG path in a large fraction of real-
        # network OD pairs: during the early relaxation phase, D decays
        # geometrically toward its instantaneous fixed point on EVERY edge
        # roughly in lockstep (dominated by (1-dt) per iteration) well
        # before flux has actually redistributed enough to differentiate
        # the true shortest path from a plausible-looking alternative, so
        # the walk can reproduce the same (wrong) path for several
        # consecutive iterations purely as an artifact of that shared decay
        # rate -- not because the model has converged. The single reliable
        # criterion on this network is `max|delta D| < eps`; see the
        # module docstring's CONVERGENCE section.
        self._converged_flag = max_delta < self._eps

        return self._best_cost

    def best_path(self) -> list[int] | None:
        return self._best_path

    def best_cost(self) -> float | None:
        return self._best_cost

    def converged(self) -> bool:
        return self._converged_flag

    def apply_disruption(self, removed_edges: list[tuple[int, int, int]], warm: bool) -> None:
        G2 = self._G.copy()
        G2.remove_edges_from(list(removed_edges))
        self._G = G2

        if warm:
            # Retain every surviving pair's conductivity untouched; only
            # force D=0 on pairs with zero remaining directed edges.
            affected_pairs = {_pair_key(u, v) for (u, v, _k) in removed_edges}
            for pair in affected_pairs:
                i = self._pair_index.get(pair)
                if i is None:
                    continue
                u, v = pair
                if not (self._G.has_edge(u, v) or self._G.has_edge(v, u)):
                    self._D[i] = 0.0
            # Warm does NOT rebuild the model -- that is the whole point --
            # but the closure can still have stranded a component (Phase 8a:
            # 82 of the sweep's 360 disrupted cells do), and an ungrounded
            # component makes the reduced Laplacian exactly singular. The
            # active-component structure must therefore be recomputed here.
            self._refresh_active_component()
        else:
            # Cold start: rebuild the model from scratch on the
            # post-disruption graph (equivalent to a fresh prepare()).
            # `_build_model` refreshes the active component itself.
            self._build_model(self._G)

        self._reset_run_state()

    def state(self) -> dict[str, Any]:
        return {
            "variant": self._variant,
            "D_by_pair": {
                f"{u}_{v}": float(d) for (u, v), d in zip(self._pairs, self._D)
            },
            "iteration": self._iteration,
            "best_path": list(self._best_path) if self._best_path is not None else None,
            "best_cost": self._best_cost,
        }

    def load_state(self, state: dict[str, Any]) -> None:
        for key, val in state.get("D_by_pair", {}).items():
            u_s, v_s = key.split("_")
            pair = _pair_key(int(u_s), int(v_s))
            i = self._pair_index.get(pair)
            if i is not None:
                self._D[i] = float(val)
        self._iteration = int(state.get("iteration", self._iteration))
        bp = state.get("best_path")
        self._best_path = list(bp) if bp is not None else None
        self._best_cost = state.get("best_cost")

    # -- Reporting helpers ---------------------------------------------------

    def extraction_counts(self) -> dict[str, int]:
        """Tally of which path-extraction method fired on every step() call
        so far, e.g. {"max_flux_walk": 27, "theta_subgraph_fallback": 3}."""
        return dict(self._extraction_counts)

    def mean_solve_time_s(self) -> float:
        return float(np.mean(self._solve_times)) if self._solve_times else 0.0

    def n_solves(self) -> int:
        return len(self._solve_times)

    def last_extraction_method(self) -> str | None:
        return self._last_method

    def active_component(self) -> tuple[int, int]:
        """``(active_nodes, total_nodes)`` for the component the Laplacian is
        currently assembled over. Equal on an undisrupted connected graph;
        after a closure that strands a region they differ, and the difference
        is exactly the degeneracy Phase 8a removed."""
        return int(self._active_node.sum()), len(self._nodes)

    def last_flux_by_node(self) -> dict[int, float]:
        """Net flux at every node after the most recent step() call: sum of
        flux flowing IN minus flux flowing OUT along every undirected model
        edge. Used by tests to check Kirchhoff conservation (should be ~0 at
        every node except source, which nets to +I0, and sink, which nets to
        -I0) -- the sharpest available test that the grounded Laplacian
        solve is actually correct."""
        net: dict[int, float] = {n: 0.0 for n in self._nodes}
        for i, (u, v) in enumerate(self._pairs):
            q = self._last_Q[i]  # positive => flow u -> v
            net[u] -= q
            net[v] += q
        return net

    # -- Model construction ---------------------------------------------------

    def _build_model(self, G: nx.MultiDiGraph) -> None:
        nodes = list(G.nodes())
        self._nodes = nodes
        self._node_idx = {n: i for i, n in enumerate(nodes)}
        n = len(nodes)

        pair_L: dict[tuple[int, int], float] = {}
        for u, v, k, _data in G.edges(keys=True, data=True):
            if u == v:
                continue
            val = self._snapshot.travel_time.get((u, v, k))
            if val is None:
                continue
            pair = _pair_key(u, v)
            if pair not in pair_L or val < pair_L[pair]:
                pair_L[pair] = val

        pairs = sorted(pair_L.keys())
        self._pairs = pairs
        self._pair_index = {pair: i for i, pair in enumerate(pairs)}
        m = len(pairs)

        edge_u = np.empty(m, dtype=np.int64)
        edge_v = np.empty(m, dtype=np.int64)
        L = np.empty(m, dtype=np.float64)
        for i, (u, v) in enumerate(pairs):
            edge_u[i] = self._node_idx[u]
            edge_v[i] = self._node_idx[v]
            L[i] = pair_L[(u, v)]

        self._edge_u = edge_u
        self._edge_v = edge_v
        self._L = L
        self._D = np.full(m, self._D_init, dtype=np.float64)

        self._sink_idx = self._node_idx[self._target]
        self._refresh_active_component()

        max_steps = max(4, int(self._walk_factor * max(n, 1)))
        self._walk_max_steps = max_steps

    def _refresh_active_component(self) -> None:
        """Recompute the sparsity structure of the reduced Laplacian over the
        source's connected component of the SURVIVING undirected projection.

        Called from `_build_model` and again after every `apply_disruption`
        (warm included -- warm does not rebuild the model, but a closure can
        still strand a component, which is exactly the Phase 8a bug). The
        component is derived from the GRAPH's surviving edges, never from the
        magnitude of `D`: deciding membership from conductivities would make
        the matrix's structure drift as tubes atrophy, and `D` under the Tero
        rule decays towards zero without ever reaching it.

        On a connected, undisrupted graph every node is active and the
        assembled matrix (including the row ordering of the reduced index) is
        identical to the pre-Phase-8a one, so Phase 5's measurements stand.
        """
        G = self._G
        pairs = self._pairs
        n = len(self._nodes)
        edge_u, edge_v = self._edge_u, self._edge_v

        # A tube is LIVE iff at least one directed edge still joins its
        # endpoints. In cold mode `_build_model` already rebuilt `pairs` from
        # the disrupted graph so every pair is live; in warm mode `pairs` is
        # the pre-disruption set and this is what drops the severed tubes.
        live = np.fromiter(
            (G.has_edge(u, v) or G.has_edge(v, u) for (u, v) in pairs),
            dtype=bool,
            count=len(pairs),
        )

        adjacency: list[list[int]] = [[] for _ in range(n)]
        for i in np.nonzero(live)[0]:
            a = int(edge_u[i])
            b = int(edge_v[i])
            adjacency[a].append(b)
            adjacency[b].append(a)

        source_idx = self._node_idx[self._source]
        active = np.zeros(n, dtype=bool)
        active[source_idx] = True
        stack = [source_idx]
        while stack:
            x = stack.pop()
            for y in adjacency[x]:
                if not active[y]:
                    active[y] = True
                    stack.append(y)

        sink_idx = self._sink_idx
        if not active[sink_idx]:
            raise PhysarumUnreachableTarget(
                f"target {self._target} is not in source {self._source}'s "
                "connected component of the surviving undirected projection "
                f"({int(active.sum())} of {n} nodes reachable)"
            )

        self._active_node = active
        self._all_active = bool(active.all())
        # Both endpoints of a live tube are in the same component by
        # definition, so testing one endpoint would suffice; both are tested
        # so the mask stays correct if the derivation of `live` ever changes.
        self._active_edge = live & active[edge_u] & active[edge_v]

        reduced_index = np.full(n, -1, dtype=np.int64)
        keep = [idx for idx in range(n) if active[idx] and idx != sink_idx]
        reduced_index[keep] = np.arange(len(keep))
        self._reduced_index = reduced_index
        self._reduced_n = len(keep)
        self._orig_of_reduced = np.array(keep, dtype=np.int64)
        self._source_reduced = int(reduced_index[source_idx])

        # Precompute the fixed sparsity structure used to assemble the
        # reduced Laplacian every iteration (only the `data` array changes
        # as conductivities adapt): diagonal contributions from every active
        # tube touching a kept (active, non-sink) node, off-diagonal
        # contributions only from active tubes where BOTH endpoints are kept.
        ra = reduced_index[edge_u]
        rb = reduced_index[edge_v]
        self._ra = ra
        self._rb = rb
        self._mask_a = self._active_edge & (ra != -1)
        self._mask_b = self._active_edge & (rb != -1)
        self._mask_both = self._mask_a & self._mask_b

    def _reset_run_state(self) -> None:
        self._iteration = 0
        self._last_delta = float("inf")
        self._path_history: list[tuple[int, ...] | None] = []
        self._converged_flag = False
        self._best_path: list[int] | None = None
        self._best_cost: float | None = None
        self._solve_times: list[float] = []
        self._extraction_counts: dict[str, int] = {}
        self._last_method: str | None = None

    # -- Per-iteration Laplacian solve ---------------------------------------

    def _solve_pressures(self, w: np.ndarray) -> np.ndarray:
        ra, rb = self._ra, self._rb
        mask_a, mask_b, mask_both = self._mask_a, self._mask_b, self._mask_both

        rows = np.concatenate([ra[mask_a], rb[mask_b], ra[mask_both], rb[mask_both]])
        cols = np.concatenate([ra[mask_a], rb[mask_b], rb[mask_both], ra[mask_both]])
        data = np.concatenate(
            [w[mask_a], w[mask_b], -w[mask_both], -w[mask_both]]
        )

        n_r = self._reduced_n
        L_reduced = sp.coo_matrix((data, (rows, cols)), shape=(n_r, n_r)).tocsr()

        b = np.zeros(n_r, dtype=np.float64)
        b[self._source_reduced] = self._I0

        t0 = time.perf_counter()
        # `spsolve` reports an exactly-singular matrix as a WARNING and hands
        # back all-NaN pressures. Warning-and-continuing is what let the
        # Phase 8a degeneracy be absorbed by the extraction fallback and
        # scored as a healthy run, so the warning is promoted to an exception
        # here and the result is additionally checked for finiteness (one
        # measured degenerate case returned zeros without warning at all).
        with warnings.catch_warnings():
            warnings.simplefilter("error", spla.MatrixRankWarning)
            try:
                p_reduced = spla.spsolve(L_reduced, b)
            except spla.MatrixRankWarning as exc:
                raise PhysarumDegenerateSolve(
                    f"singular Laplacian at iteration {self._iteration} "
                    f"(reduced system {n_r}x{n_r}, "
                    f"{int(self._active_node.sum())} active nodes, "
                    f"{int(self._active_edge.sum())} active tubes): {exc}"
                ) from exc
        self._solve_times.append(time.perf_counter() - t0)

        if not np.all(np.isfinite(p_reduced)):
            n_bad = int((~np.isfinite(p_reduced)).sum())
            raise PhysarumDegenerateSolve(
                f"non-finite pressure at iteration {self._iteration}: "
                f"{n_bad} of {n_r} reduced nodes are NaN or inf"
            )

        p_full = np.zeros(len(self._nodes), dtype=np.float64)
        p_full[self._orig_of_reduced] = p_reduced
        # p_full[self._sink_idx] is already 0 (grounded).
        return p_full

    # -- Path extraction -------------------------------------------------------

    def _extract_path(self, p: np.ndarray, w: np.ndarray):
        source, target = self._source, self._target
        node_idx = self._node_idx
        pair_index = self._pair_index

        u = source
        visited = {source}
        path = [source]
        for _ in range(self._walk_max_steps):
            if u == target:
                break
            best_flux = None
            best_v = None
            for _u2, v, _k in self._G.out_edges(u, keys=True):
                if v in visited:
                    continue
                pair = _pair_key(u, v)
                i = pair_index.get(pair)
                if i is None:
                    continue
                flux = w[i] * (p[node_idx[u]] - p[node_idx[v]])
                if best_flux is None or flux > best_flux:
                    best_flux = flux
                    best_v = v
            if best_v is None:
                break
            path.append(best_v)
            visited.add(best_v)
            u = best_v

        if u == target:
            cost = self._snapshot.cost_of_path(self._G, path)
            return path, cost, "max_flux_walk"

        return self._fallback_path(w)

    def _fallback_path(self, w: np.ndarray):
        source, target = self._source, self._target
        H = nx.MultiDiGraph()
        H.add_nodes_from(self._G.nodes())
        for u, v, k in self._G.edges(keys=True):
            pair = _pair_key(u, v)
            i = self._pair_index.get(pair)
            if i is not None and self._D[i] > self._theta:
                H.add_edge(u, v, key=k)

        weight_fn = self._snapshot.nx_weight()
        try:
            path = nx.dijkstra_path(H, source, target, weight=weight_fn)
            cost = self._snapshot.cost_of_path(self._G, path)
            return path, cost, "theta_subgraph_fallback"
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            pass

        try:
            path = nx.dijkstra_path(self._G, source, target, weight=weight_fn)
            cost = self._snapshot.cost_of_path(self._G, path)
            return path, cost, "full_graph_fallback"
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None, None, "no_path_found"
