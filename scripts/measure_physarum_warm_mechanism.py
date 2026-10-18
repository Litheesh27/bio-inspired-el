"""Phase 8e follow-up: WHY does warm start make Physarum's max-flux walk
less likely to complete a path during recovery?

Run:
    SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe \
        scripts/measure_physarum_warm_mechanism.py
    .venv/Scripts/python.exe scripts/measure_physarum_warm_mechanism.py --report-only

THE QUESTION
--------------------------------------------------------------------------
Phase 8e RESULT 4 measured the FACT: the warm max-flux walk completes a path
on only 36.3% of recoveries vs cold's 59.5%, and on the 126 cells where both
complete cold's walk is marginally better. CLAUDE.md then offers a MECHANISM,
explicitly labelled "plausible, not yet tested":

    the retained conductivity field is tuned to the OLD optimum, so after a
    closure it keeps driving flux down now-severed corridors and the walk
    dead-ends; a cold field starts uniform and explores freely.

This script tests that mechanism against two rivals. The predictions each
explanation makes were WRITTEN DOWN FIRST, in
`docs/tuning/physarum_warm_start_mechanism.md`, before any datum was
collected; that file is the pre-registration and this script is its
instrument. Read it before reading the numbers.

  M  "misdirected retained field" -- the CLAUDE.md mechanism. Spatial and
     closure-specific: the damage depends on WHERE the closure is relative to
     the retained corridor.
  R  "global decay" -- the warm field has simply decayed, so there is less
     usable conductivity everywhere and the dead-ends have nothing to do with
     the closure's location.
  C  "field concentration" -- the warm field is CONCENTRATED on a thin
     corridor, so off-corridor pressures are nearly flat and the walk's
     ranking there is decided by ~0 flux differences. Spatially related to the
     CORRIDOR, not the closure. Level of D is irrelevant; concentration is not.

BEHAVIOURAL IDENTITY (hard constraint)
--------------------------------------------------------------------------
`src/bioroute/algorithms/physarum.py` is NOT TOUCHED. The quantity the whole
experiment turns on -- the node at which a FAILING walk dead-ends -- is not
exposed by the router, so `replay_walk()` below re-implements the walk loop
OUTSIDE the router from read-only state (`_last_p`, `_L`, `_G`,
`_pair_index`, `_node_idx`, plus the pre-step `D`).

Two details make that replay exact rather than approximate, and both are
asserted on EVERY iteration rather than assumed:

  1. `step()` scores the walk with `w = D_OLD / L` (the `w` it solved the
     pressures with) even though `self._D` has already been advanced to
     `D_new` by the time `_extract_path` is called. The replay therefore
     snapshots `D` BEFORE each `step()`.
  2. The replay must reach the target exactly when the router reports
     `last_extraction_method() == "max_flux_walk"`. Any disagreement raises.

`--check` additionally verifies that the two run-construction shortcuts this
script uses are behaviourally identical to the sweep's own sequence (see
`verify_equivalence`).

SAMPLING
--------------------------------------------------------------------------
Declared in the pre-registration before running: 6 OD pairs (2 per distance
stratum, in the sweep's own draw order) x 6 disrupted scenarios x SUMO heavy
x {warm, cold}, plus one placebo cell per OD pair. Heavy only and one traffic
level, because a dead-end's spatial locality is topological and the Parquet
shows the warm deficit is uniform across scenarios -- so OD x scenario buys
more power on the quantity under test than a second traffic level would.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import networkx as nx  # noqa: E402
import numpy as np  # noqa: E402

from bioroute.algorithms.base import run_to_convergence  # noqa: E402
from bioroute.algorithms.physarum import (  # noqa: E402
    PhysarumDegenerateSolve,
    PhysarumRouter,
    _pair_key,
)
from bioroute.disruption.apply import apply_disruption  # noqa: E402
from bioroute.disruption.scenarios import (  # noqa: E402
    _closure_edges,
    generate_disruption,
)
from bioroute.experiments.runner import _disruption_config, make_router  # noqa: E402
from bioroute.metrics.collectors import dijkstra_reference  # noqa: E402
from bioroute.viz.traces import load_context  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "results" / "physarum_warm_mechanism.jsonl"

SCENARIOS = ("S1", "S2", "S3", "S4", "S5", "S6")
TRAFFIC = "heavy"
#: 2 per stratum, in the sweep's own draw order (no fresh draw: a cell that is
#: not a sweep cell cannot be cross-checked against the Parquet).
N_PER_STRATUM = 2
#: Fixed node sample for the `d_random` null, drawn once so every cell's null
#: is computed over the same nodes.
N_RANDOM_NODES = 200
NULL_SEED = 991
#: Placebo closure: >= this many hops from EVERY node of the pre-disruption
#: optimal path, so the retained corridor is untouched.
PLACEBO_MIN_HOPS = 8
#: Hop radii for the "where does the retained conductivity mass sit" shares.
NEAR_RADII = (1, 2, 3)
#: Top-quantile of D that counts as "the retained corridor" for explanation C.
CORRIDOR_Q = 0.95


# ==========================================================================
# Read-only replay of the router's max-flux walk
# ==========================================================================


def replay_walk(router: PhysarumRouter, D_pre: np.ndarray) -> tuple[list[int], bool]:
    """Re-run `PhysarumRouter._extract_path`'s greedy max-flux walk from the
    outside, returning `(path_walked, reached_target)`.

    `D_pre` must be the conductivity vector as it stood BEFORE the `step()`
    whose pressures are now in `router._last_p` -- `step()` scores the walk
    with the same `w` it solved the pressures with, but has already advanced
    `self._D` by the time it extracts. Passing `router._D` here would be a
    subtly different walk on most iterations.

    Read-only: nothing on the router is mutated, so running this cannot move
    a number the model reports.
    """
    G = router._G
    p = router._last_p
    w = D_pre / router._L
    node_idx = router._node_idx
    pair_index = router._pair_index
    source, target = router._source, router._target

    u = source
    visited = {source}
    path = [source]
    for _ in range(router._walk_max_steps):
        if u == target:
            break
        best_flux = None
        best_v = None
        for _u2, v, _k in G.out_edges(u, keys=True):
            if v in visited:
                continue
            i = pair_index.get(_pair_key(u, v))
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
    return path, u == target


def run_with_walk_replay(
    router: PhysarumRouter, budget: int
) -> tuple[Any, list[dict[str, Any]]]:
    """`run_to_convergence`, but recording the replayed walk every iteration.

    Reimplements the driver loop (rather than wrapping it) only because the
    replay needs `D` from before each `step()`. The loop is otherwise
    line-for-line `algorithms.base.run_to_convergence`, including the
    converged-break placement, and `verify_equivalence` checks the traces it
    produces against the real driver's.
    """
    import time

    trace: list[float] = []
    per_iter: list[dict[str, Any]] = []
    first_valid_iter: int | None = None
    start = time.perf_counter()
    iterations = 0

    for i in range(1, budget + 1):
        D_pre = router._D.copy()
        cost = router.step()
        iterations = i
        if cost is None:
            trace.append(float("inf"))
        else:
            trace.append(cost)
            if first_valid_iter is None:
                first_valid_iter = i

        walk_path, reached = replay_walk(router, D_pre)
        method = router.last_extraction_method()
        # The replay is EXACT or the measurement is worthless. This assertion
        # is the whole reason the router did not need instrumenting.
        if reached != (method == "max_flux_walk"):
            raise AssertionError(
                f"walk replay disagrees with the router at iteration {i}: "
                f"replay reached={reached} but router method={method!r}"
            )
        per_iter.append(
            {
                "iter": i,
                "walk_ok": bool(reached),
                "terminal": int(walk_path[-1]),
                "walk_nodes": [int(n) for n in walk_path],
                "method": method,
            }
        )

        if router.converged():
            break

    from bioroute.algorithms.base import RouterResult

    return (
        RouterResult(
            path=router.best_path(),
            cost=router.best_cost(),
            iterations=iterations,
            converged=router.converged(),
            trace=trace,
            time_to_first_valid_path_iters=first_valid_iter,
            wallclock_s=time.perf_counter() - start,
            extras={},
        ),
        per_iter,
    )


# ==========================================================================
# Graph geometry helpers
# ==========================================================================


def undirected_projection(G: nx.MultiDiGraph) -> nx.Graph:
    """Hop-distance geometry: the UNDISRUPTED undirected projection.

    Deliberately undisrupted. S6 isolates its closed node in the surviving
    graph, so a BFS on the survivor would report an infinite distance from
    every dead-end to exactly the scenario whose closure is most central --
    which is a statement about the closure, not about where the walk stopped.
    "How far across the city is the dead-end from the closure" is a question
    about the street network, so it is measured on the street network.
    """
    H = nx.Graph()
    H.add_nodes_from(G.nodes())
    H.add_edges_from((u, v) for u, v in G.edges() if u != v)
    return H


def hop_field(H: nx.Graph, sources: Iterable[int]) -> dict[int, int]:
    """Multi-source BFS hop distances from `sources`."""
    sources = [s for s in sources if s in H]
    if not sources:
        return {}
    seed = "__multi_source__"
    H.add_node(seed)
    H.add_edges_from((seed, s) for s in sources)
    try:
        d = nx.single_source_shortest_path_length(H, seed)
    finally:
        H.remove_node(seed)
    return {n: v - 1 for n, v in d.items() if n != seed}


def _q(values: Sequence[float], q: float) -> float | None:
    s = sorted(float(v) for v in values)
    if not s:
        return None
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _med(values: Sequence[float]) -> float | None:
    return None if not values else float(statistics.median(values))


def _iqr(values: Sequence[float]) -> tuple[float | None, float | None]:
    return _q(values, 0.25), _q(values, 0.75)


def gini(values: Sequence[float]) -> float | None:
    v = sorted(float(x) for x in values if x >= 0)
    n = len(v)
    tot = sum(v)
    if n == 0 or tot <= 0:
        return None
    cum = 0.0
    for i, x in enumerate(v, start=1):
        cum += i * x
    return float((2.0 * cum) / (n * tot) - (n + 1.0) / n)


# ==========================================================================
# Conductivity-field descriptors
# ==========================================================================


def live_tube_mask(router: PhysarumRouter) -> np.ndarray:
    """Tubes with at least one surviving directed edge in `router._G`."""
    G = router._G
    return np.fromiter(
        (G.has_edge(u, v) or G.has_edge(v, u) for (u, v) in router._pairs),
        dtype=bool,
        count=len(router._pairs),
    )


def field_descriptors(
    router: PhysarumRouter,
    dist_closure: dict[int, int],
) -> dict[str, Any]:
    """Everything measurements 2 and 3 need about the conductivity field at
    the moment of disruption (after `apply_disruption`, before any recovery
    `step()`)."""
    D = router._D
    live = live_tube_mask(router)
    pairs = router._pairs

    D_live = D[live]
    tube_d = np.array(
        [
            min(
                dist_closure.get(u, 10**6),
                dist_closure.get(v, 10**6),
            )
            for (u, v) in pairs
        ],
        dtype=np.int64,
    )

    out: dict[str, Any] = {
        "n_tubes_total": int(len(pairs)),
        "n_tubes_live": int(live.sum()),
        "D_total": float(D_live.sum()),
        "D_mean": float(D_live.mean()) if D_live.size else None,
        "D_median": float(np.median(D_live)) if D_live.size else None,
        "D_p25": float(np.percentile(D_live, 25)) if D_live.size else None,
        "D_p75": float(np.percentile(D_live, 75)) if D_live.size else None,
        "D_max": float(D_live.max()) if D_live.size else None,
        "D_share_below_theta": float((D_live < router._theta).mean())
        if D_live.size
        else None,
        "D_gini": gini(D_live.tolist()),
    }
    # Measurement 3's concentration summary: how much of the whole field's
    # mass sits on its top 1% / 5% of tubes.
    if D_live.size:
        srt = np.sort(D_live)[::-1]
        tot = float(srt.sum()) or 1.0
        for frac in (0.01, 0.05):
            k = max(1, int(round(frac * srt.size)))
            out[f"D_top{int(frac * 100)}pct_share"] = float(srt[:k].sum() / tot)

    # Measurement 2: share of surviving mass within h hops of the closure,
    # against the structural (uniform-D) baseline cold gets for free.
    tot_live = float(D_live.sum()) or 1.0
    for h in NEAR_RADII:
        near = live & (tube_d <= h)
        out[f"near{h}_D_share"] = float(D[near].sum() / tot_live)
        out[f"near{h}_count_share"] = (
            float(near.sum() / live.sum()) if live.sum() else None
        )
        out[f"near{h}_excess"] = (
            out[f"near{h}_D_share"] / out[f"near{h}_count_share"]
            if out[f"near{h}_count_share"]
            else None
        )

    # Explanation C's "retained corridor": the top-quantile tubes of the
    # surviving field. Nodes incident to them define the corridor geometry.
    if D_live.size:
        thr = float(np.quantile(D_live, CORRIDOR_Q))
        corridor_nodes = {
            n
            for i, (u, v) in enumerate(pairs)
            if live[i] and D[i] >= thr
            for n in (u, v)
        }
    else:
        thr = 0.0
        corridor_nodes = set()
    out["corridor_threshold_D"] = thr
    out["n_corridor_nodes"] = len(corridor_nodes)
    return out, corridor_nodes


# ==========================================================================
# One cell
# ==========================================================================


def summarise_walk(
    per_iter: Sequence[dict[str, Any]],
    dist_closure: dict[int, int],
    dist_corridor: dict[int, int],
    dist_path: dict[int, int],
) -> dict[str, Any]:
    """Spatial summary (measurement 1) + per-iteration success (measurement 4)."""
    fails = [r for r in per_iter if not r["walk_ok"]]
    oks = [r for r in per_iter if r["walk_ok"]]

    def dclosure(n: int) -> float | None:
        v = dist_closure.get(n)
        return None if v is None else float(v)

    first = per_iter[0] if per_iter else None
    first_term = None if first is None else int(first["terminal"])

    # Per-cell terminal-distance summary over FAILING iterations only.
    term_d = [d for r in fails if (d := dclosure(r["terminal"])) is not None]
    # Null: the failing walks' own non-terminal nodes.
    walkpath_d: list[float] = []
    for r in fails:
        for n in r["walk_nodes"][:-1]:
            d = dclosure(n)
            if d is not None:
                walkpath_d.append(d)

    term_corr = [
        float(dist_corridor[r["terminal"]])
        for r in fails
        if r["terminal"] in dist_corridor
    ]
    term_path = [
        float(dist_path[r["terminal"]]) for r in fails if r["terminal"] in dist_path
    ]

    return {
        "n_iters": len(per_iter),
        "n_walk_ok": len(oks),
        "n_walk_fail": len(fails),
        "walk_ok_any": bool(oks),
        "walk_ok_first_iter": bool(first["walk_ok"]) if first else None,
        "walk_ok_last_iter": bool(per_iter[-1]["walk_ok"]) if per_iter else None,
        "walk_ok_rate": len(oks) / len(per_iter) if per_iter else None,
        "first_iter_terminal": first_term,
        "first_iter_walk_ok": bool(first["walk_ok"]) if first else None,
        "first_iter_d_closure": None if first_term is None else dclosure(first_term),
        "first_iter_hops_walked": None
        if first is None
        else len(first["walk_nodes"]) - 1,
        "median_terminal_d_closure": _med(term_d),
        "median_walkpath_d_closure": _med(walkpath_d),
        "median_terminal_d_corridor": _med(term_corr),
        "median_terminal_d_refpath": _med(term_path),
        "n_distinct_terminals": len({r["terminal"] for r in fails}),
        # Measurement 4: the success series, thinned to the first 60
        # iterations plus a tail summary. Full series is not needed and would
        # bloat the JSONL.
        "walk_ok_series_head": [int(r["walk_ok"]) for r in per_iter[:60]],
        "walk_ok_rate_first10": (
            sum(r["walk_ok"] for r in per_iter[:10]) / min(10, len(per_iter))
            if per_iter
            else None
        ),
        "walk_ok_rate_last10": (
            sum(r["walk_ok"] for r in per_iter[-10:]) / min(10, len(per_iter))
            if per_iter
            else None
        ),
        "first_walk_ok_iter": next((r["iter"] for r in oks), None),
    }


def placebo_closure(
    G: nx.MultiDiGraph,
    H: nx.Graph,
    ref_path: Sequence[int],
    source: int,
    target: int,
    rng: random.Random,
    cfg,
) -> tuple[list[tuple[int, int, int]], dict[str, Any]]:
    """A connectivity-preserving 1-pair closure >= PLACEBO_MIN_HOPS from EVERY
    node of the pre-disruption optimal path.

    Phase 7's closure semantics are reused verbatim via `_closure_edges`
    (ordered node pair, every parallel edge, reverse direction too), so the
    placebo differs from S1 in exactly one respect: WHERE it is. That is the
    point -- the retained field is just as decayed and just as concentrated,
    but nothing is driving flux into a severed corridor.
    """
    dist_path = hop_field(H, ref_path)
    cands = [
        (u, v, k)
        for u, v, k in G.edges(keys=True)
        if u != v
        and dist_path.get(u, 0) >= PLACEBO_MIN_HOPS
        and dist_path.get(v, 0) >= PLACEBO_MIN_HOPS
    ]
    rng.shuffle(cands)
    for u, v, _k in cands[:200]:
        removed = _closure_edges(G, [(u, v)], cfg)
        if not removed:
            continue
        view = apply_disruption(G, removed, copy=False)
        if nx.has_path(view, source, target):
            return removed, {"pair": [int(u), int(v)], "n_candidates": len(cands)}
    return [], {"pair": None, "n_candidates": len(cands)}


def measure_cell(
    ctx,
    od,
    scenario_id: str,
    H: nx.Graph,
    random_nodes: Sequence[int],
    budget: int,
    options,
    snapshot,
    level: str = TRAFFIC,
    placebo: bool = False,
) -> list[dict[str, Any]]:
    """Measure one (od, scenario) cell warm AND cold, paired by construction.

    Mirrors `runner.execute_cell` / `collectors._run_into`: the closure is
    generated ONCE and both modes face the identical removed-edge set, and the
    pre/post Dijkstra references are computed once and shared.
    """
    G = ctx.graph
    cfg = _disruption_config(ctx.cfg.disruption)

    ref_path, ref_cost = dijkstra_reference(G, od.source, od.target, snapshot)
    if ref_path is None or ref_cost is None:
        return []

    if placebo:
        rng = random.Random(f"placebo-{ctx.cfg.scenario_seed}-{od.source}-{od.target}")
        removed, pmeta = placebo_closure(
            G, H, ref_path, od.source, od.target, rng, cfg
        )
        if not removed:
            return []
        dis_meta = {"placebo": pmeta}
    else:
        dis = generate_disruption(
            G,
            od.source,
            od.target,
            snapshot,
            scenario_id,
            seed=ctx.cfg.scenario_seed,
            path=ref_path,
            config=cfg,
        )
        # MUST be checked before use (CLAUDE.md Phase 8d GOTCHA).
        if not dis.ok:
            return [
                {
                    "od_source": od.source,
                    "od_target": od.target,
                    "scenario_id": scenario_id,
                    "skipped": dis.reason,
                }
            ]
        removed = list(dis.removed_edges)
        dis_meta = {"rejections": dis.rejections, "attempts": dis.attempts}

    G_dis = apply_disruption(G, removed, copy=False)
    dis_ref_path, dis_ref_cost = dijkstra_reference(
        G_dis, od.source, od.target, snapshot
    )
    if dis_ref_cost is None:
        return []

    closure_nodes = sorted({n for (u, v, _k) in removed for n in (u, v)})
    dist_closure = hop_field(H, closure_nodes)
    dist_refpath = hop_field(H, ref_path)

    null = {
        "d_random_median": _med(
            [float(dist_closure[n]) for n in random_nodes if n in dist_closure]
        ),
        "d_random_p25": _q(
            [float(dist_closure[n]) for n in random_nodes if n in dist_closure], 0.25
        ),
        "d_random_p75": _q(
            [float(dist_closure[n]) for n in random_nodes if n in dist_closure], 0.75
        ),
        "d_source": dist_closure.get(od.source),
        "d_target": dist_closure.get(od.target),
        "d_refpath_median": _med(
            [float(dist_closure[n]) for n in ref_path if n in dist_closure]
        ),
    }

    rows: list[dict[str, Any]] = []
    for warm in (True, False):
        router: PhysarumRouter = make_router("physarum", options)  # type: ignore[assignment]
        try:
            router.prepare(G, od.source, od.target, snapshot, 0)
            init_res, init_iters = run_with_walk_replay(router, budget)

            # Pre-disruption field (measurement 2's `sev_mass_pre`), read
            # BEFORE apply_disruption forces severed pairs to D = 0.
            D_pre_dis = router._D.copy()
            sev_idx = [
                router._pair_index[p]
                for p in {_pair_key(u, v) for (u, v, _k) in removed}
                if p in router._pair_index
            ]
            tot_pre = float(D_pre_dis.sum()) or 1.0
            sev_mass_pre = float(D_pre_dis[sev_idx].sum() / tot_pre) if sev_idx else 0.0

            router.apply_disruption(list(removed), warm=warm)
            desc, corridor_nodes = field_descriptors(router, dist_closure)
            dist_corridor = hop_field(H, corridor_nodes)

            rec_res, rec_iters = run_with_walk_replay(router, budget)
            failed = ""
        except PhysarumDegenerateSolve as exc:
            rows.append(
                {
                    "od_source": od.source,
                    "od_target": od.target,
                    "scenario_id": scenario_id,
                    "traffic_level": level,
                    "warm_start": warm,
                    "failed": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        init_sum = summarise_walk(init_iters, dist_closure, {}, dist_refpath)
        rec_sum = summarise_walk(rec_iters, dist_closure, dist_corridor, dist_refpath)

        rows.append(
            {
                "od_source": int(od.source),
                "od_target": int(od.target),
                "stratum": od.stratum,
                "scenario_id": scenario_id,
                "traffic_level": level,
                "warm_start": bool(warm),
                "placebo": bool(placebo),
                "failed": failed,
                "n_removed_edges": len(removed),
                "closure_nodes": [int(n) for n in closure_nodes],
                "disruption": dis_meta,
                "reference_cost": float(ref_cost),
                "reference_hops": len(ref_path) - 1,
                "disrupted_reference_cost": float(dis_ref_cost),
                # Parquet cross-check columns.
                "path_cost": None if init_res.cost is None else float(init_res.cost),
                "stretch": None
                if init_res.cost is None
                else float(init_res.cost) / float(ref_cost),
                "iterations": int(init_res.iterations),
                "recovery_iterations": int(rec_res.iterations),
                "post_disruption_cost": None
                if rec_res.cost is None
                else float(rec_res.cost),
                "post_disruption_stretch": None
                if rec_res.cost is None
                else float(rec_res.cost) / float(dis_ref_cost),
                "best_path_extraction_method": router.best_path_extraction_method(),
                "walk_success_count_recovery": router.walk_success_count(),
                "sev_mass_pre": sev_mass_pre,
                "n_severed_tubes": len(sev_idx),
                "field": desc,
                "null": null,
                "initial": init_sum,
                "recovery": rec_sum,
            }
        )
    return rows


# ==========================================================================
# Equivalence checks
# ==========================================================================


def verify_equivalence(ctx, od, scenario_id: str, options, snapshot, budget: int) -> dict:
    """Prove the two shortcuts this script takes change nothing.

    1. `run_with_walk_replay` vs `algorithms.base.run_to_convergence`: same
       iterations, same converged flag, same best cost, same trace.
    2. The replayed walk vs the router's own verdict -- already asserted every
       iteration inside the driver, so reaching here at all is a pass.
    """
    G = ctx.graph
    cfg = _disruption_config(ctx.cfg.disruption)
    ref_path, _ = dijkstra_reference(G, od.source, od.target, snapshot)
    dis = generate_disruption(
        G, od.source, od.target, snapshot, scenario_id,
        seed=ctx.cfg.scenario_seed, path=ref_path, config=cfg,
    )
    removed = list(dis.removed_edges)

    out: dict[str, Any] = {}
    for warm in (True, False):
        a: PhysarumRouter = make_router("physarum", options)  # type: ignore[assignment]
        a.prepare(G, od.source, od.target, snapshot, 0)
        ra = run_to_convergence(a, budget)
        a.apply_disruption(list(removed), warm=warm)
        rra = run_to_convergence(a, budget)

        b: PhysarumRouter = make_router("physarum", options)  # type: ignore[assignment]
        b.prepare(G, od.source, od.target, snapshot, 0)
        rb, _ = run_with_walk_replay(b, budget)
        b.apply_disruption(list(removed), warm=warm)
        rrb, _ = run_with_walk_replay(b, budget)

        key = "warm" if warm else "cold"
        out[key] = {
            "initial_iters_equal": ra.iterations == rb.iterations,
            "initial_cost_equal": ra.cost == rb.cost,
            "initial_trace_equal": ra.trace == rb.trace,
            "initial_path_equal": ra.path == rb.path,
            "recovery_iters_equal": rra.iterations == rrb.iterations,
            "recovery_cost_equal": rra.cost == rrb.cost,
            "recovery_trace_equal": rra.trace == rrb.trace,
            "recovery_path_equal": rra.path == rrb.path,
            "walk_best_equal": a.walk_best_cost() == b.walk_best_cost(),
        }
    return out


def scale_invariance_probe(
    ctx, od_sel, options, snapshot, budget: int, factors=(1e-3, 1e3)
) -> list[dict]:
    """Is the walk's dead-end caused by the LEVEL of D, or by its SHAPE?

    Explanation R says the warm field is simply too small. That claim is
    testable analytically and the model answers it outright: scaling every
    conductivity by a constant `c` scales every `w = D/L` by `c`, hence the
    reduced Laplacian by `c`, hence (with `b` fixed) the pressures by `1/c` --
    so the flux `Q = w * dp` the walk ranks candidates by is EXACTLY
    unchanged. A uniform shortage of conductivity therefore cannot make the
    walk dead-end anywhere it would not have dead-ended with a thousand times
    more of it.

    This probe checks that numerically rather than taking the algebra on
    trust: it rescales the retained field by 1/1000 and 1000 at the moment of
    disruption and compares the first post-disruption walk. Scaling a throwaway
    router instance's `_D` is a NUMERICAL PROBE, not a model change -- nothing
    in `physarum.py` moves, and no measured result in this document comes from
    a rescaled run.
    """
    out: list[dict] = []
    cfg = _disruption_config(ctx.cfg.disruption)
    G = ctx.graph
    for od in od_sel:
        ref_path, _ = dijkstra_reference(G, od.source, od.target, snapshot)
        dis = generate_disruption(
            G, od.source, od.target, snapshot, "S1",
            seed=ctx.cfg.scenario_seed, path=ref_path, config=cfg,
        )
        if not dis.ok:
            continue
        removed = list(dis.removed_edges)
        base = None
        row = {"od": [od.source, od.target]}
        for factor in (1.0,) + tuple(factors):
            r: PhysarumRouter = make_router("physarum", options)  # type: ignore[assignment]
            r.prepare(G, od.source, od.target, snapshot, 0)
            run_to_convergence(r, budget)
            r.apply_disruption(list(removed), warm=True)
            r._D = r._D * factor  # numerical probe only -- see docstring
            D_pre = r._D.copy()
            r.step()
            walk, ok = replay_walk(r, D_pre)
            if factor == 1.0:
                base = (tuple(walk), ok)
                row["baseline_walk_ok"] = bool(ok)
                row["baseline_hops"] = len(walk) - 1
            else:
                row[f"x{factor:g}_identical"] = bool(
                    base == (tuple(walk), ok)
                )
                row[f"x{factor:g}_walk_ok"] = bool(ok)
        out.append(row)
    return out


def crosscheck_parquet(rows: Sequence[dict], parquet_path: Path) -> dict[str, Any]:
    """Compare every measured cell to its Phase 8e Parquet row."""
    import pandas as pd

    df = pd.read_parquet(parquet_path)
    df = df[(df.algorithm == "physarum")]
    checked = 0
    mismatches: list[dict[str, Any]] = []
    for r in rows:
        if r.get("placebo") or r.get("failed") or r.get("skipped"):
            continue
        sel = df[
            (df.od_source == r["od_source"])
            & (df.od_target == r["od_target"])
            & (df.scenario_id == r["scenario_id"])
            & (df.traffic_level == r["traffic_level"])
            & (df.warm_start == r["warm_start"])
        ]
        if sel.empty:
            mismatches.append({"cell": r["scenario_id"], "why": "no parquet row"})
            continue
        row = sel.iloc[0]
        for col in ("stretch", "post_disruption_stretch", "recovery_iterations"):
            mine = r[col]
            theirs = row[col]
            theirs = None if theirs is None or pd.isna(theirs) else float(theirs)
            checked += 1
            ok = (mine is None and theirs is None) or (
                mine is not None
                and theirs is not None
                and abs(float(mine) - theirs) <= 1e-6 * max(1.0, abs(theirs))
            )
            if not ok:
                mismatches.append(
                    {
                        "cell": f"{r['od_source']}->{r['od_target']} "
                        f"{r['scenario_id']} {'warm' if r['warm_start'] else 'cold'}",
                        "field": col,
                        "mine": mine,
                        "parquet": theirs,
                    }
                )
    return {"checked": checked, "mismatches": mismatches}


# ==========================================================================
# Main
# ==========================================================================


def _paired(rows: Sequence[dict], placebo: bool = False):
    """Index measured rows by `(od, scenario, traffic)` and return the cells
    where BOTH modes are present. Pairing is not optional in this project
    (CLAUDE.md standing rule, which this very result cost us)."""
    by: dict[tuple, dict[bool, dict]] = {}
    for r in rows:
        if r.get("failed") or r.get("skipped") or bool(r.get("placebo")) != placebo:
            continue
        key = (r["od_source"], r["od_target"], r["scenario_id"], r["traffic_level"])
        by.setdefault(key, {})[bool(r["warm_start"])] = r
    return {k: v for k, v in by.items() if True in v and False in v}


def _fmt(values: Sequence[float | None], label: str) -> str:
    v = [float(x) for x in values if x is not None]
    if not v:
        return f"{label}: n=0"
    lo, hi = _iqr(v)
    return f"{label}: n={len(v)} median={statistics.median(v):.3f} IQR=[{lo:.3f},{hi:.3f}]"


def _wilcoxon(a: Sequence[float], b: Sequence[float]) -> str:
    """Paired Wilcoxon with the sign breakdown, or an honest refusal."""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < 6:
        return f"n={len(pairs)} -- UNDERPOWERED, no test"
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    lt = sum(1 for x, y in pairs if x < y)
    gt = sum(1 for x, y in pairs if x > y)
    eq = sum(1 for x, y in pairs if x == y)
    try:
        from scipy.stats import wilcoxon as _w

        stat = _w(xs, ys, zero_method="wilcox")
        p = f"p={stat.pvalue:.3g}"
    except Exception:  # all-zero differences
        p = "p=n/a (all differences zero)"
    return (
        f"n={len(pairs)} median_a={statistics.median(xs):.3f} "
        f"median_b={statistics.median(ys):.3f} a<b:{lt} a>b:{gt} ties:{eq} {p}"
    )


def _mcnemar(a_only: int, b_only: int) -> str:
    from scipy.stats import binomtest

    n = a_only + b_only
    if n == 0:
        return "no discordant pairs"
    return f"discordant {a_only}/{b_only}, exact p={binomtest(a_only, n, 0.5).pvalue:.3g}"


def report(rows: Sequence[dict]) -> None:
    """Print every pre-registered measurement. No measurement is omitted
    because it came out null -- that is the point of the pre-registration."""
    cells = _paired(rows)
    pla = _paired(rows, placebo=True)
    print(f"\n=== measured cells: {len(cells)} paired, {len(pla)} placebo paired ===")

    # ---- Measurement 6 (control): the walk on the converged, intact field
    last_ok = [c[True]["initial"]["walk_ok_last_iter"] for c in cells.values()]
    any_ok = [c[True]["initial"]["walk_ok_any"] for c in cells.values()]
    print("\n-- M6 CONTROL: walk on the fully decayed field, graph INTACT --")
    print(f"  walk completes on the FINAL pre-disruption iteration: "
          f"{sum(bool(x) for x in last_ok)}/{len(last_ok)} "
          f"({sum(bool(x) for x in last_ok)/max(len(last_ok),1):.3f})")
    print(f"  walk completes on ANY pre-disruption iteration:       "
          f"{sum(bool(x) for x in any_ok)}/{len(any_ok)} "
          f"({sum(bool(x) for x in any_ok)/max(len(any_ok),1):.3f})")

    # ---- The effect, in this sample
    w_ok = [bool(c[True]["recovery"]["walk_ok_any"]) for c in cells.values()]
    c_ok = [bool(c[False]["recovery"]["walk_ok_any"]) for c in cells.values()]
    print("\n-- the effect in this sample (paired) --")
    print(f"  warm {sum(w_ok)}/{len(w_ok)} = {sum(w_ok)/len(w_ok):.3f}   "
          f"cold {sum(c_ok)}/{len(c_ok)} = {sum(c_ok)/len(c_ok):.3f}")
    print("  " + _mcnemar(sum(1 for w, c in zip(w_ok, c_ok) if w and not c),
                          sum(1 for w, c in zip(w_ok, c_ok) if c and not w))
          + "  (warm-only / cold-only)")

    # ---- Measurement 1: spatial locality of the dead-end
    print("\n-- M1 SPATIAL LOCALITY of the dead-end (hops to nearest closed edge) --")
    for mode, flag in (("warm", True), ("cold", False)):
        term = [c[flag]["recovery"]["median_terminal_d_closure"] for c in cells.values()]
        wp = [c[flag]["recovery"]["median_walkpath_d_closure"] for c in cells.values()]
        rnd = [c[flag]["null"]["d_random_median"] for c in cells.values()]
        print(f"  {mode}: " + _fmt(term, "terminal->closure"))
        print(f"        " + _fmt(wp, "its own path nodes->closure"))
        print(f"        " + _fmt(rnd, "random node->closure (null)"))
        print(f"        terminal vs random  : {_wilcoxon(term, rnd)}")
        print(f"        terminal vs own path: {_wilcoxon(term, wp)}")
    wt = [c[True]["recovery"]["median_terminal_d_closure"] for c in cells.values()]
    ct = [c[False]["recovery"]["median_terminal_d_closure"] for c in cells.values()]
    print(f"  warm vs cold terminal->closure: {_wilcoxon(wt, ct)}")
    print("  (first post-disruption iteration only)")
    wt1 = [c[True]["recovery"]["first_iter_d_closure"] for c in cells.values()
           if not c[True]["recovery"]["first_iter_walk_ok"]]
    ct1 = [c[False]["recovery"]["first_iter_d_closure"] for c in cells.values()
           if not c[False]["recovery"]["first_iter_walk_ok"]]
    print(f"        warm " + _fmt(wt1, "first-iter terminal->closure"))
    print(f"        cold " + _fmt(ct1, "first-iter terminal->closure"))
    print("\n  -- explanation C's geometry: terminal -> retained high-D corridor --")
    for mode, flag in (("warm", True), ("cold", False)):
        corr = [c[flag]["recovery"]["median_terminal_d_corridor"] for c in cells.values()]
        print(f"  {mode}: " + _fmt(corr, "terminal->corridor"))

    # ---- Measurement 2 / 3: does the field predict failure?
    print("\n-- M2/M3: does any field descriptor PREDICT warm walk failure? --")
    warm_rows = [c[True] for c in cells.values()]
    ok = [r for r in warm_rows if r["recovery"]["walk_ok_any"]]
    bad = [r for r in warm_rows if not r["recovery"]["walk_ok_any"]]
    fields = [
        ("sev_mass_pre", lambda r: r["sev_mass_pre"]),
        ("near1_excess", lambda r: r["field"]["near1_excess"]),
        ("near2_excess", lambda r: r["field"]["near2_excess"]),
        ("near3_excess", lambda r: r["field"]["near3_excess"]),
        ("near2_D_share", lambda r: r["field"]["near2_D_share"]),
        ("D_total", lambda r: r["field"]["D_total"]),
        ("D_median", lambda r: r["field"]["D_median"]),
        ("D_gini", lambda r: r["field"]["D_gini"]),
        ("D_top5pct_share", lambda r: r["field"]["D_top5pct_share"]),
        ("D_share_below_theta", lambda r: r["field"]["D_share_below_theta"]),
        ("reference_hops", lambda r: r["reference_hops"]),
    ]
    from scipy.stats import mannwhitneyu

    print(f"  warm cells: walk ok n={len(ok)}, walk failed n={len(bad)}")
    for name, get in fields:
        a = [get(r) for r in ok if get(r) is not None]
        b = [get(r) for r in bad if get(r) is not None]
        if len(a) < 4 or len(b) < 4:
            print(f"    {name:<22} UNDERPOWERED (n={len(a)}/{len(b)})")
            continue
        u = mannwhitneyu(a, b)
        print(
            f"    {name:<22} ok_med={statistics.median(a):>10.4f} "
            f"fail_med={statistics.median(b):>10.4f}  p={u.pvalue:.3g}"
        )
    print("\n  field LEVEL, warm vs cold at the moment of disruption (paired):")
    for key in ("D_total", "D_median", "D_gini", "D_share_below_theta",
                "D_top5pct_share", "near2_excess"):
        a = [c[True]["field"][key] for c in cells.values()]
        b = [c[False]["field"][key] for c in cells.values()]
        print(f"    {key:<22} {_wilcoxon(a, b)}")

    # ---- Measurement 4: does it decay over recovery?
    print("\n-- M4: walk success over recovery iterations --")
    for mode, flag in (("warm", True), ("cold", False)):
        f10 = [c[flag]["recovery"]["walk_ok_rate_first10"] for c in cells.values()]
        l10 = [c[flag]["recovery"]["walk_ok_rate_last10"] for c in cells.values()]
        rate = [c[flag]["recovery"]["walk_ok_rate"] for c in cells.values()]
        iters = [c[flag]["recovery"]["n_iters"] for c in cells.values()]
        print(f"  {mode}: " + _fmt(f10, "walk-ok rate, first 10 iters"))
        print(f"        " + _fmt(l10, "walk-ok rate, last 10 iters"))
        print(f"        " + _fmt(rate, "walk-ok rate, whole recovery"))
        print(f"        " + _fmt(iters, "recovery iterations"))
        print(f"        first10 vs last10: {_wilcoxon(f10, l10)}")

    # ---- M6 extended: is warm recovery just the intact-field walk again?
    print("\n-- M6 EXTENDED: does the INTACT converged field already predict it? --")
    print("   (cross-tab: walk on the FINAL pre-disruption iteration, graph intact,")
    print("    vs walk completion during recovery. Pre-disruption is identical")
    print("    warm and cold by construction, so this is one predictor, two outcomes.)")
    for mode, flag in (("warm", True), ("cold", False)):
        tab = Counter(
            (
                bool(c[flag]["initial"]["walk_ok_last_iter"]),
                bool(c[flag]["recovery"]["walk_ok_any"]),
            )
            for c in cells.values()
        )
        n = sum(tab.values())
        agree = tab[(True, True)] + tab[(False, False)]
        print(
            f"  {mode}: intact_ok&rec_ok={tab[(True, True)]} "
            f"intact_ok&rec_fail={tab[(True, False)]} "
            f"intact_fail&rec_ok={tab[(False, True)]} "
            f"intact_fail&rec_fail={tab[(False, False)]}  "
            f"agreement={agree}/{n}={agree / max(n, 1):.3f}"
        )
        try:
            from scipy.stats import fisher_exact

            odds, pv = fisher_exact(
                [
                    [tab[(True, True)], tab[(True, False)]],
                    [tab[(False, True)], tab[(False, False)]],
                ]
            )
            print(f"        Fisher exact p={pv:.3g} odds={odds:.3g}")
        except Exception as exc:  # pragma: no cover
            print(f"        Fisher unavailable: {exc}")

    # ---- Path length: the confound that has to be declared, not buried
    print("\n-- CONFOUND CHECK: path length vs every cross-cell predictor --")
    hops = sorted(c[True]["reference_hops"] for c in cells.values())
    t1 = _q(hops, 1 / 3) or 0
    t2 = _q(hops, 2 / 3) or 0
    print(f"   reference_hops terciles at {t1:.0f} / {t2:.0f}")
    for lo, hi, lbl in ((-1, t1, "short"), (t1, t2, "medium"), (t2, 10**9, "long")):
        grp = [c for c in cells.values() if lo < c[True]["reference_hops"] <= hi]
        if not grp:
            continue
        wk = [bool(c[True]["recovery"]["walk_ok_any"]) for c in grp]
        ck = [bool(c[False]["recovery"]["walk_ok_any"]) for c in grp]
        print(
            f"   {lbl:<7} n={len(grp):<4} warm_ok={sum(wk) / len(wk):.3f} "
            f"cold_ok={sum(ck) / len(ck):.3f} "
            f"intact_final_ok="
            f"{sum(bool(c[True]['initial']['walk_ok_last_iter']) for c in grp) / len(grp):.3f}"
        )
    print("   within-tercile test of the closure-relative predictors (warm cells):")
    from scipy.stats import mannwhitneyu as _mw

    for lo, hi, lbl in ((-1, t1, "short"), (t1, t2, "medium"), (t2, 10**9, "long")):
        grp = [c[True] for c in cells.values() if lo < c[True]["reference_hops"] <= hi]
        okg = [r for r in grp if r["recovery"]["walk_ok_any"]]
        bad = [r for r in grp if not r["recovery"]["walk_ok_any"]]
        if len(okg) < 4 or len(bad) < 4:
            print(f"   {lbl:<7} UNDERPOWERED (ok={len(okg)}, fail={len(bad)})")
            continue
        for name, get in (
            ("sev_mass_pre", lambda r: r["sev_mass_pre"]),
            ("near2_excess", lambda r: r["field"]["near2_excess"]),
            ("D_total", lambda r: r["field"]["D_total"]),
            ("D_gini", lambda r: r["field"]["D_gini"]),
        ):
            a = [get(r) for r in okg if get(r) is not None]
            b = [get(r) for r in bad if get(r) is not None]
            if len(a) < 4 or len(b) < 4:
                continue
            print(
                f"   {lbl:<7} {name:<14} ok_med={statistics.median(a):>9.4f} "
                f"fail_med={statistics.median(b):>9.4f} p={_mw(a, b).pvalue:.3g}"
            )

    # ---- Measurement 5: the placebo
    print("\n-- M5 PLACEBO closure (far from the retained corridor) --")
    if not pla:
        print("  no placebo cells")
    else:
        pw = [bool(c[True]["recovery"]["walk_ok_any"]) for c in pla.values()]
        pc = [bool(c[False]["recovery"]["walk_ok_any"]) for c in pla.values()]
        print(f"  placebo: warm {sum(pw)}/{len(pw)} = {sum(pw)/len(pw):.3f}   "
              f"cold {sum(pc)}/{len(pc)} = {sum(pc)/len(pc):.3f}")
        print("  " + _mcnemar(sum(1 for w, c in zip(pw, pc) if w and not c),
                              sum(1 for w, c in zip(pw, pc) if c and not w))
              + "  (warm-only / cold-only)")
        # Paired against the SAME OD pair's real closures.
        real_by_od: dict[tuple, list[bool]] = {}
        for (s, t, _sc, _tr), c in cells.items():
            real_by_od.setdefault((s, t), []).append(
                bool(c[True]["recovery"]["walk_ok_any"])
            )
        comp = [
            (bool(c[True]["recovery"]["walk_ok_any"]),
             sum(real_by_od[(s, t)]) / len(real_by_od[(s, t)]))
            for (s, t, _sc, _tr), c in pla.items()
            if (s, t) in real_by_od
        ]
        print("  per-OD: placebo warm walk ok  vs  mean real-closure warm walk ok")
        for (p_ok, r_rate), (s, t, _sc, _tr) in zip(comp, pla.keys()):
            print(f"    {s}->{t}: placebo={int(p_ok)}  real_mean={r_rate:.2f}")
        if comp:
            print(f"  placebo warm ok rate {sum(1 for x in comp if x[0])/len(comp):.3f} "
                  f"vs real warm ok rate "
                  f"{sum(x[1] for x in comp)/len(comp):.3f}  (n={len(comp)} OD pairs)")
            exact = sum(1 for p_ok, r in comp if (r in (0.0, 1.0)) and bool(p_ok) == bool(r))
            unani = sum(1 for _p, r in comp if r in (0.0, 1.0))
            print(
                f"  OD pairs whose real closures were UNANIMOUS warm: {unani}/{len(comp)}; "
                f"placebo agreed with that unanimous outcome on {exact}/{unani}"
            )
            # Paired per (od, traffic) placebo vs each real scenario cell.
            a = b = 0
            for (s_, t_, _sc, tr), c in pla.items():
                pw_ = bool(c[True]["recovery"]["walk_ok_any"])
                for (s2, t2, _sc2, tr2), c2 in cells.items():
                    if (s2, t2, tr2) != (s_, t_, tr):
                        continue
                    rw = bool(c2[True]["recovery"]["walk_ok_any"])
                    a += int(pw_ and not rw)
                    b += int(rw and not pw_)
            print(f"  placebo-vs-real-closure, warm, paired per (od,traffic,scenario): "
                  + _mcnemar(a, b) + "  (placebo-only / real-only)")


def pick_od_pairs(od_pairs, n_per_stratum: int):
    by: dict[str, list] = {}
    for p in od_pairs:
        by.setdefault(p.stratum, []).append(p)
    out = []
    for stratum in ("short", "medium", "long"):
        out.extend(by.get(stratum, [])[:n_per_stratum])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--check", action="store_true", help="equivalence check only")
    ap.add_argument("--n-per-stratum", type=int, default=N_PER_STRATUM)
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--no-placebo", action="store_true")
    ap.add_argument(
        "--all-od", action="store_true",
        help="census: every OD pair of the sweep's draw, not the pilot sample",
    )
    ap.add_argument("--traffic", default=TRAFFIC, help="comma-separated levels")
    ap.add_argument("--scale-check", action="store_true",
                    help="numerical probe: is the walk invariant to D's LEVEL?")
    args = ap.parse_args()

    if args.report_only:
        rows = [json.loads(l) for l in args.out.read_text().splitlines() if l.strip()]
        report(rows)
        return 0

    ctx = load_context(verbose=True)
    G = ctx.graph
    levels = [x for x in args.traffic.split(",") if x]
    options = ctx.options("physarum")
    budget = ctx.budget("physarum")
    od_sel = (
        list(ctx.od_pairs)
        if args.all_od
        else pick_od_pairs(ctx.od_pairs, args.n_per_stratum)
    )
    snapshot = ctx.snapshot(levels[0])
    print(
        f"  budget={budget} od={[(p.source, p.target, p.stratum) for p in od_sel]}",
        flush=True,
    )

    if args.scale_check:
        res = scale_invariance_probe(
            ctx, od_sel, options, ctx.snapshot(levels[0]), budget
        )
        print(json.dumps(res, indent=2))
        return 0

    if args.check:
        res = verify_equivalence(ctx, od_sel[0], "S1", options, snapshot, budget)
        print(json.dumps(res, indent=2))
        return 0 if all(all(v.values()) for v in res.values()) else 1

    print("  building undirected projection ...", flush=True)
    H = undirected_projection(G)
    rng = random.Random(NULL_SEED)
    random_nodes = rng.sample(sorted(G.nodes()), min(N_RANDOM_NODES, G.number_of_nodes()))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    scenarios = [s for s in args.scenarios.split(",") if s]
    jobs: list[tuple[Any, str, bool, str]] = [
        (od, s, False, lv) for lv in levels for od in od_sel for s in scenarios
    ]
    if not args.no_placebo:
        jobs += [(od, "PLACEBO", True, lv) for lv in levels for od in od_sel]

    import time

    all_rows: list[dict[str, Any]] = []
    with args.out.open("w", encoding="utf-8") as fh:
        for n, (od, scenario_id, placebo, level) in enumerate(jobs, start=1):
            t0 = time.perf_counter()
            rows = measure_cell(
                ctx, od, scenario_id, H, random_nodes, budget, options,
                ctx.snapshot(level), level=level, placebo=placebo,
            )
            for r in rows:
                fh.write(json.dumps(r) + "\n")
            fh.flush()
            all_rows.extend(rows)
            got = {
                ("warm" if r.get("warm_start") else "cold"): r.get("recovery", {}).get(
                    "walk_ok_any"
                )
                for r in rows
                if "recovery" in r
            }
            print(
                f"  [{n}/{len(jobs)}] {level} {od.source}->{od.target} "
                f"{scenario_id}: {got} ({time.perf_counter() - t0:.1f}s)",
                flush=True,
            )

    cc = crosscheck_parquet(all_rows, ROOT / "data/results/final_14a535c02b83.parquet")
    print(json.dumps({"parquet_crosscheck": cc}, indent=2)[:4000])
    print(f"  wrote {len(all_rows)} rows -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
