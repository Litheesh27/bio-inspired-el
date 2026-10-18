"""Regeneration of the artifacts the results Parquet cannot carry.

IMPLEMENTED IN: Phase 9b, driven by scripts/06_make_traces.py.

--------------------------------------------------------------------------
WHY THIS MODULE EXISTS AT ALL
--------------------------------------------------------------------------

`data/results/final_14a535c02b83.parquet` is the project's experimental
evidence and every *measured* Phase 9 number comes from it. But it stores
ONE FLAT ROW PER RUN, so three of Phase 9's seven figures have no source
column anywhere in it:

  * figure 1 (convergence, cost vs iteration) needs a per-iteration trace;
    the Parquet keeps `iterations` and the final `stretch`, not the curve.
  * figure 5 (routes before/after a closure) needs the NODE LISTS; the
    Parquet keeps `path_hops`, not the paths.
  * figure 6 (congestion light vs heavy) is a property of the
    `TrafficSnapshot`, not of any result row.

So this module RE-RUNS a small, explicitly chosen, seeded subset under the
sweep's own config, graph and snapshots, and writes the regenerated
artifacts to `data/results/traces/` as JSON.

**EVERYTHING THIS MODULE PRODUCES IS ILLUSTRATIVE AND DERIVED FROM A
RE-RUN. IT IS NOT THE SWEEP'S MEASURED RESULT.** Figures 1, 5 and 6 must
say so in their captions, and no statistic may be quoted from here --
Phase 8e's Parquet is the only admissible source for a measured claim.
`scripts/06_make_traces.py` cross-checks the re-run costs against the
matching Parquet cells precisely so that the illustration and the evidence
can be shown to agree.

--------------------------------------------------------------------------
HOW THE SWEEP'S CONDITIONS ARE REPRODUCED
--------------------------------------------------------------------------

Nothing here re-derives a condition that `experiments/` already owns:

  * the config goes through `load_experiment_config` AND
    `with_area_hash(cfg, config_hash(area_cfg))`. Both steps are required:
    without the area hash the config hash comes out `078ba9094218` instead
    of the sweep's `14a535c02b83`, and the artifacts would be silently
    filed under a hash that matches no Parquet.
  * the graph goes through `load_sweep_graph`, never `load_baseline_graph`
    directly (CLAUDE.md Phase 8 GOTCHA: the baseline hash folds in
    `netconvert_flags`, so the default `None` yields a different hash and a
    FileNotFoundError whose message misleadingly blames SUMO).
  * snapshots come from `prepare_resources` / `Resources.snapshot`, so they
    are the same cached snapshot files the sweep's workers read.
  * OD pairs come from `sample_od_pairs(G, cfg.od)` -- the SAME draw (seed
    8080). A figure must correspond to a real sweep cell, so a fresh draw
    is forbidden here.
  * disruptions come from `generate_disruption(..., scenario_id, seed=...)`
    with the config's `scenario_seed` (1337) and `cfg.disruption`.
  * the measurement driver is `algorithms.base.run_to_convergence`, the
    same one `metrics.collectors` uses. Its `RouterResult.trace` IS the
    convergence trace, so figure 1 plots exactly the quantity the sweep
    measured rather than a parallel reimplementation that could drift.

--------------------------------------------------------------------------
WHAT LIVES WHERE
--------------------------------------------------------------------------

This module: regeneration, the JSON schemas, and figure 1 (a line chart).
`viz/maps.py`: figures 5 and 6, which are both drawn on street geometry.
`viz/figures.py` + `scripts/04_make_figures.py` (Phase 9a, a different
author) own figures 2, 3, 4 and 7, all of which come straight from the
Parquet. Nothing in this file reads the Parquet except the cross-check.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import networkx as nx

from bioroute.algorithms.base import Router, RouterResult, run_to_convergence
from bioroute.congestion.snapshot import TrafficSnapshot
from bioroute.disruption.apply import apply_disruption
from bioroute.disruption.scenarios import generate_disruption
from bioroute.experiments.matrix import (
    Condition,
    ExperimentConfig,
    ODPair,
    load_experiment_config,
    sample_od_pairs,
    with_area_hash,
)
from bioroute.experiments.runner import (
    Resources,
    _disruption_config,  # noqa: PLC2701 - see the comment at its use site
    load_sweep_graph,
    make_router,
    prepare_resources,
)
from bioroute.metrics.collectors import dijkstra_reference

#: Default artifact directory. Gitignored (only the final Parquet is
#: tracked), so these files are regenerable rather than evidence.
DEFAULT_TRACE_DIR = Path("data/results/traces")

#: Default experiment config -- THE sweep's config, not a figure-specific one.
DEFAULT_CONFIG_PATH = "configs/experiments/disruption.yaml"

#: Downsample ceiling for a convergence series. docs/PLAN.md says "~100
#: points"; 150 is chosen instead because the ACO budget is exactly 150, so
#: at this ceiling every ACO trace is carried at FULL resolution and the
#: downsampler is a no-op for them -- i.e. figure 1's ACO curves are exact,
#: and only a Physarum run that burns its 300-iteration budget is thinned.
#: The whole bundle still comes to a few hundred KB, far inside the
#: dashboard's 16 MB budget.
MAX_TRACE_POINTS = 150


# ==========================================================================
# Context: the sweep's graph, snapshots and OD draw
# ==========================================================================


@dataclass
class TraceContext:
    """Everything a regeneration needs, loaded once.

    Deliberately holds live objects rather than paths: unlike the Phase 8
    runner this is single-process and small, so there is nothing to pickle
    into workers and no reason to pay the reload.
    """

    cfg: ExperimentConfig
    graph: nx.MultiDiGraph
    resources: Resources
    od_pairs: list[ODPair]
    meta: dict[str, Any]

    @property
    def config_hash(self) -> str:
        return self.cfg.config_hash

    def condition(self, level: str, congestion_source: str = "sumo") -> Condition:
        """The sweep's `Condition` for a traffic level.

        Looked up in `cfg.conditions` rather than constructed, so asking
        for a level the sweep never ran fails here instead of producing a
        snapshot the Parquet has no rows for.
        """
        for cond in self.cfg.conditions:
            if cond.level == level and cond.congestion_source == congestion_source:
                return cond
        raise KeyError(
            f"No condition {congestion_source}/{level} in the sweep config; have "
            f"{[(c.congestion_source, c.level) for c in self.cfg.conditions]}"
        )

    def snapshot(self, level: str, congestion_source: str = "sumo") -> TrafficSnapshot:
        return self.resources.snapshot(self.condition(level, congestion_source))

    def od(self, source: int, target: int) -> ODPair:
        """The drawn `ODPair` with these endpoints.

        Raises if the pair is not a member of the sweep's draw. That is the
        point: a figure built on a freshly drawn pair would not correspond
        to any sweep cell, and the cross-check against the Parquet would
        have nothing to compare to.
        """
        for pair in self.od_pairs:
            if pair.source == source and pair.target == target:
                return pair
        raise KeyError(
            f"OD pair ({source}, {target}) is not in the sweep's draw "
            f"(seed {self.cfg.od.seed}, {len(self.od_pairs)} pairs); "
            "Phase 9b must not draw fresh pairs."
        )

    def budget(self, algorithm: str) -> int:
        return int(self.cfg.algorithm(algorithm).budget)

    def options(self, algorithm: str) -> Mapping[str, Any]:
        return dict(self.cfg.algorithm(algorithm).options)

    def seeds(self, algorithm: str) -> list[int]:
        return list(self.cfg.algorithm(algorithm).seeds)


def load_context(
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    bundle_dir: str | Path | None = None,
    verbose: bool = True,
) -> TraceContext:
    """Load the sweep's config, graph, snapshots and OD draw.

    `bundle_dir` is where `prepare_resources` parks the pre-warmed snapshot
    JSONs; it defaults to the trace directory so a regeneration never
    writes into the sweep's own shard directory.
    """
    from bioroute.config import config_hash, load_area_config  # noqa: PLC0415

    cfg = load_experiment_config(config_path)
    area_cfg = load_area_config(cfg.area_config_path)
    # MANDATORY second step: the area hash is part of the experiment config
    # hash the Parquet was stamped with. See the module docstring.
    cfg = with_area_hash(cfg, config_hash(area_cfg))

    bundle_dir = Path(bundle_dir) if bundle_dir is not None else DEFAULT_TRACE_DIR
    resources, _spec, meta = prepare_resources(cfg, bundle_dir, verbose=verbose)
    od_pairs = sample_od_pairs(resources.graph, cfg.od)

    if verbose:
        print(
            f"  config_hash={cfg.config_hash} baseline={meta['baseline']} "
            f"baseline_hash={meta['baseline_hash']} od_pairs={len(od_pairs)}",
            flush=True,
        )

    return TraceContext(
        cfg=cfg,
        graph=resources.graph,
        resources=resources,
        od_pairs=od_pairs,
        meta=dict(meta),
    )


# ==========================================================================
# Downsampling
# ==========================================================================


def downsample(values: Sequence[float], max_points: int = MAX_TRACE_POINTS):
    """Thin `values` to at most `max_points`, KEEPING THE FIRST AND LAST.

    Returns `(indices, kept)` with 0-based iteration indices, so a thinned
    series still plots against its true iteration numbers.

    The endpoints are preserved deliberately: the first point is where the
    algorithm started and the last is the result the sweep reported, so a
    downsampler that resampled them away would make the figure disagree
    with the Parquet at exactly the two values a reader checks.
    """
    n = len(values)
    if max_points < 2:
        raise ValueError("max_points must be at least 2 to keep both endpoints")
    if n <= max_points:
        return list(range(n)), [float(v) for v in values]

    # Evenly spaced interior picks, then force the endpoints in.
    step = (n - 1) / (max_points - 1)
    idx = sorted({int(round(i * step)) for i in range(max_points)} | {0, n - 1})
    return idx, [float(values[i]) for i in idx]


def _jsonable_trace(values: Sequence[float]) -> list[float | None]:
    """`math.inf` -> `None` for JSON.

    `run_to_convergence` writes `inf` for an iteration before any valid
    path existed. JSON has no infinity, and a reader that sees `null` knows
    "no path yet" rather than inheriting a silently clipped number.
    """
    return [None if not math.isfinite(v) else float(v) for v in values]


# ==========================================================================
# Figure 1: convergence traces
# ==========================================================================


@dataclass
class ConvergenceTrace:
    """One algorithm/seed's per-iteration best cost on one OD pair.

    `costs` is `RouterResult.trace`: index i holds the best cost known
    after the (i+1)-th `step()`, with `None` for iterations before any
    valid path appeared. `stretches` is the same series divided by
    `reference_cost`, which is Dijkstra on THIS graph and THIS snapshot --
    the CLAUDE.md stretch invariant, same denominator as the Parquet's
    `stretch` column for the same cell.
    """

    od_source: int
    od_target: int
    stratum: str
    algorithm: str
    seed: int
    scenario_id: str
    congestion_source: str
    traffic_level: str
    budget: int
    iterations: int
    converged: bool
    reference_cost: float
    reference_hops: int
    best_cost: float | None
    stretch: float | None
    path_hops: int | None
    wallclock_s: float
    iteration_index: list[int] = field(default_factory=list)
    costs: list[float | None] = field(default_factory=list)
    stretches: list[float | None] = field(default_factory=list)
    downsampled: bool = False
    #: Physarum only; null elsewhere (CLAUDE.md: a Physarum cost must never
    #: be shown without saying where its path came from).
    best_path_extraction_method: str | None = None
    walk_best_cost: float | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _physarum_provenance(router: Router) -> tuple[str | None, float | None]:
    """`(best_path_extraction_method, walk_best_cost)` if the router has
    them, `(None, None)` otherwise.

    Duck-typed rather than isinstance-checked so this module does not
    import `algorithms.physarum` (and with it scipy) just to label a curve.
    """
    method = getattr(router, "best_path_extraction_method", None)
    walk = getattr(router, "walk_best_cost", None)
    return (
        method() if callable(method) else None,
        walk() if callable(walk) else None,
    )


def collect_convergence(
    ctx: TraceContext,
    od: ODPair,
    algorithm: str,
    seed: int,
    level: str = "heavy",
    congestion_source: str = "sumo",
    budget: int | None = None,
    max_points: int = MAX_TRACE_POINTS,
) -> ConvergenceTrace:
    """Re-run one (OD, algorithm, seed) on scenario S0 and keep the trace.

    S0 only: a convergence curve is about the search itself, and a
    recovery curve would mix two searches on two graphs into one series.
    """
    snapshot = ctx.snapshot(level, congestion_source)
    G = ctx.graph
    budget = ctx.budget(algorithm) if budget is None else int(budget)

    ref_path, ref_cost = dijkstra_reference(G, od.source, od.target, snapshot)
    if ref_cost is None or ref_path is None:
        raise ValueError(
            f"No path {od.source}->{od.target} on the undisrupted graph; this "
            "pair cannot be in the sweep's draw."
        )

    router = make_router(algorithm, ctx.options(algorithm))
    router.prepare(G, od.source, od.target, snapshot, seed)
    res: RouterResult = run_to_convergence(router, budget)

    idx, kept = downsample(res.trace, max_points)
    method, walk_cost = _physarum_provenance(router)

    return ConvergenceTrace(
        od_source=od.source,
        od_target=od.target,
        stratum=od.stratum,
        algorithm=algorithm,
        seed=seed,
        scenario_id="S0",
        congestion_source=congestion_source,
        traffic_level=level,
        budget=budget,
        iterations=int(res.iterations),
        converged=bool(res.converged),
        reference_cost=float(ref_cost),
        reference_hops=len(ref_path) - 1,
        best_cost=None if res.cost is None else float(res.cost),
        stretch=None if res.cost is None else float(res.cost) / float(ref_cost),
        path_hops=None if not res.path else len(res.path) - 1,
        wallclock_s=float(res.wallclock_s),
        iteration_index=list(idx),
        costs=_jsonable_trace(kept),
        stretches=[
            None if not math.isfinite(v) else float(v) / float(ref_cost) for v in kept
        ],
        downsampled=len(idx) < len(res.trace),
        best_path_extraction_method=method,
        walk_best_cost=None if walk_cost is None else float(walk_cost),
    )


def collect_convergence_set(
    ctx: TraceContext,
    od_pairs: Sequence[ODPair],
    algorithms: Sequence[str] = ("dijkstra", "astar", "aco", "physarum"),
    level: str = "heavy",
    congestion_source: str = "sumo",
    max_points: int = MAX_TRACE_POINTS,
    verbose: bool = True,
) -> list[ConvergenceTrace]:
    """Every (OD pair, algorithm, config seed) trace for figure 1.

    Seeds come from the config, so ACO contributes all 5 of the sweep's
    seeds. Phase 4d measured one pair giving stretches
    {1.000, 1.000, 1.000, 1.233, 1.446} across seeds, so the spread is a
    finding: figure 1 draws every seed and a median, never a mean.
    """
    traces: list[ConvergenceTrace] = []
    for od in od_pairs:
        for algorithm in algorithms:
            for seed in ctx.seeds(algorithm):
                started = time.perf_counter()
                trace = collect_convergence(
                    ctx,
                    od,
                    algorithm,
                    seed,
                    level=level,
                    congestion_source=congestion_source,
                    max_points=max_points,
                )
                traces.append(trace)
                if verbose:
                    print(
                        f"    {od.stratum:<6} {od.source}->{od.target} "
                        f"{algorithm}/seed{seed}: {trace.iterations} iters, "
                        f"stretch {trace.stretch:.4f} "
                        f"({time.perf_counter() - started:.1f}s)",
                        flush=True,
                    )
    return traces


# ==========================================================================
# Figure 5: routes before and after a disruption
# ==========================================================================


@dataclass
class RouteRecord:
    """One algorithm's route on one cell, before and after the closure."""

    algorithm: str
    seed: int
    warm_start: bool
    path_before: list[int] = field(default_factory=list)
    path_after: list[int] = field(default_factory=list)
    cost_before: float | None = None
    cost_after: float | None = None
    stretch_before: float | None = None
    stretch_after: float | None = None
    hops_before: int | None = None
    hops_after: int | None = None
    iterations_before: int = 0
    iterations_after: int = 0
    #: Physarum only. CLAUDE.md Phase 9 forbids presenting a
    #: fallback-derived path as the model's own output, and a map is
    #: exactly where that would mislead -- so the provenance travels with
    #: the route and figure 5 puts it in the legend.
    extraction_before: str | None = None
    extraction_after: str | None = None
    walk_cost_before: float | None = None
    walk_cost_after: float | None = None
    failure_reason: str = ""


@dataclass
class RouteBundle:
    """Everything figure 5 draws for one (OD, scenario, condition) cell."""

    od_source: int
    od_target: int
    stratum: str
    distance_m: float
    scenario_id: str
    congestion_source: str
    traffic_level: str
    scenario_seed: int
    warm_start: bool
    removed_edges: list[list[int]] = field(default_factory=list)
    reference_path: list[int] = field(default_factory=list)
    reference_cost: float = 0.0
    disrupted_reference_path: list[int] = field(default_factory=list)
    disrupted_reference_cost: float = 0.0
    disruption_rejections: int = 0
    disruption_attempts: int = 0
    routes: list[RouteRecord] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def route(self, algorithm: str) -> RouteRecord:
        for rec in self.routes:
            if rec.algorithm == algorithm:
                return rec
        raise KeyError(f"no route recorded for {algorithm!r}")


def collect_routes(
    ctx: TraceContext,
    od: ODPair,
    scenario_id: str,
    algorithms: Sequence[str] = ("dijkstra", "astar", "aco", "physarum"),
    level: str = "heavy",
    congestion_source: str = "sumo",
    warm: bool = True,
    seed: int = 0,
    verbose: bool = True,
) -> RouteBundle:
    """Re-run one disrupted cell and keep every algorithm's node list.

    This mirrors `runner.execute_cell` / `collectors._run_into` step for
    step, including the ONE-DENOMINATOR-PER-CELL rule: the pre- and
    post-disruption Dijkstra references are computed once here and every
    algorithm's stretch is measured against them. Computing them per
    algorithm would silently break the CLAUDE.md stretch invariant in a
    way no figure would reveal.
    """
    G = ctx.graph
    snapshot = ctx.snapshot(level, congestion_source)
    cond = ctx.condition(level, congestion_source)

    ref_path, ref_cost = dijkstra_reference(G, od.source, od.target, snapshot)
    if ref_cost is None or ref_path is None:
        raise ValueError(f"no path {od.source}->{od.target} on the undisrupted graph")

    # `generate_disruption`'s `scenario_id` is the FIFTH positional
    # argument (CLAUDE.md Phase 8d GOTCHA), and `_disruption_config` is
    # imported from the runner rather than rebuilt here so the closure is
    # drawn under byte-identical settings to the sweep's -- a reimplemented
    # DisruptionConfig that drifted would produce a DIFFERENT closure and
    # the figure would illustrate a cell that does not exist.
    disruption = generate_disruption(
        G,
        od.source,
        od.target,
        snapshot,
        scenario_id,
        seed=ctx.cfg.scenario_seed,
        path=ref_path,
        config=_disruption_config(ctx.cfg.disruption),
    )
    # MUST be checked before use: an `ok=False` Disruption carries zero
    # removed edges and passing it on raises SchemaViolation downstream.
    if not disruption.ok:
        raise ValueError(
            f"scenario {scenario_id} infeasible for {od.source}->{od.target}: "
            f"{disruption.reason}"
        )

    removed = list(disruption.removed_edges)
    # A restricted_view, not a copy (Phase 7): it shares the base graph's
    # edge ATTRIBUTE dicts, which is safe here because nothing below writes
    # an attribute -- all costs come from the snapshot.
    G_disrupted = apply_disruption(G, removed, copy=False)
    dis_ref_path, dis_ref_cost = dijkstra_reference(
        G_disrupted, od.source, od.target, snapshot
    )
    if dis_ref_cost is None or dis_ref_path is None:
        raise ValueError(
            f"scenario {scenario_id} left no post-disruption Dijkstra reference"
        )

    bundle = RouteBundle(
        od_source=od.source,
        od_target=od.target,
        stratum=od.stratum,
        distance_m=float(od.distance_m),
        scenario_id=scenario_id,
        congestion_source=cond.congestion_source,
        traffic_level=cond.level,
        scenario_seed=int(ctx.cfg.scenario_seed),
        warm_start=bool(warm),
        removed_edges=[[int(u), int(v), int(k)] for u, v, k in removed],
        reference_path=[int(n) for n in ref_path],
        reference_cost=float(ref_cost),
        disrupted_reference_path=[int(n) for n in dis_ref_path],
        disrupted_reference_cost=float(dis_ref_cost),
        disruption_rejections=int(disruption.rejections),
        disruption_attempts=int(disruption.attempts),
    )

    for algorithm in algorithms:
        spec = ctx.cfg.algorithm(algorithm)
        # A classical router has no adaptation state, so the sweep runs it
        # cold-only; forcing warm=True on it would be meaningless rather
        # than wrong, but recording the sweep's own mode keeps the figure's
        # metadata honest.
        run_warm = warm and not spec.is_classical
        run_seed = seed if not spec.is_classical else 0
        budget = int(spec.budget)

        rec = RouteRecord(algorithm=algorithm, seed=run_seed, warm_start=run_warm)
        router = make_router(algorithm, ctx.options(algorithm))
        router.prepare(G, od.source, od.target, snapshot, run_seed)
        before: RouterResult = run_to_convergence(router, budget)
        rec.iterations_before = int(before.iterations)
        rec.extraction_before, rec.walk_cost_before = _physarum_provenance(router)

        if not before.path:
            rec.failure_reason = "no valid path before disruption within budget"
            bundle.routes.append(rec)
            continue
        rec.path_before = [int(n) for n in before.path]
        rec.cost_before = float(snapshot.cost_of_path(G, before.path))
        rec.stretch_before = rec.cost_before / float(ref_cost)
        rec.hops_before = len(before.path) - 1

        router.apply_disruption(list(removed), warm=run_warm)
        after: RouterResult = run_to_convergence(router, budget)
        rec.iterations_after = int(after.iterations)
        rec.extraction_after, rec.walk_cost_after = _physarum_provenance(router)

        if not after.path:
            rec.failure_reason = "no valid path after disruption within budget"
            bundle.routes.append(rec)
            continue
        rec.path_after = [int(n) for n in after.path]
        rec.cost_after = float(snapshot.cost_of_path(G_disrupted, after.path))
        rec.stretch_after = rec.cost_after / float(dis_ref_cost)
        rec.hops_after = len(after.path) - 1
        bundle.routes.append(rec)

        if verbose:
            print(
                f"    {algorithm}: stretch {rec.stretch_before:.4f} -> "
                f"{rec.stretch_after:.4f} "
                f"({rec.hops_before} -> {rec.hops_after} hops, "
                f"{rec.iterations_before}/{rec.iterations_after} iters"
                + (
                    f", via {rec.extraction_after}"
                    if rec.extraction_after
                    else ""
                )
                + ")",
                flush=True,
            )

    return bundle


# ==========================================================================
# Figure 6: congestion ratios
# ==========================================================================


def congestion_ratios(
    G: nx.MultiDiGraph, snapshot: TrafficSnapshot
) -> dict[tuple[int, int, int], float]:
    """Per-edge `snapshot travel_time / baseline free-flow travel_time`.

    The denominator is the BASELINE GRAPH's own `travel_time` attribute --
    on the sweep's `sumo_speeds` graph that is the one shared free-flow
    denominator Phase 2d established, the same one every SUMO/BPR ratio in
    this project is measured against. A ratio of EXACTLY 1.0 means the
    provider had no traveltime observation for that edge at all, and the
    count of those matches the snapshot's own `n_fallback_edges` exactly
    (1,616 light / 812 heavy of 7,015, measured).

    **That is NOT the same statistic as Phase 2d's "61% of SUMO edges see
    no vehicle even at heavy demand"**, and conflating them would overstate
    how much of this network the simulation leaves untouched. netconvert
    splits ways while OSMnx merges them, so one graph edge aggregates
    several SUMO segments and reports a non-unit ratio if ANY of them
    carried traffic. 61% of SUMO EDGES idle; 12% of GRAPH EDGES have no
    observation.

    Edges absent from the snapshot are skipped rather than imputed: a
    missing edge is a coverage gap, and filling it with 1.0 would quietly
    report a congestion measurement where none exists.
    """
    out: dict[tuple[int, int, int], float] = {}
    tt = snapshot.travel_time
    for u, v, k, data in G.edges(keys=True, data=True):
        free = data.get("travel_time")
        congested = tt.get((u, v, k))
        if free is None or congested is None:
            continue
        free = float(free)
        if free <= 0:
            continue
        # Node ids are NOT coerced to int here: the function is generic
        # over node labels (the unit tests run it on a lettered toy
        # graph), and `congestion_bundle` is where the keys become the
        # "u|v|k" strings JSON needs.
        out[(u, v, k)] = float(congested) / free
    return out


def congestion_bundle(
    ctx: TraceContext,
    levels: Sequence[str] = ("light", "heavy"),
    congestion_source: str = "sumo",
    round_to: int = 4,
) -> dict[str, Any]:
    """Figure 6's artifact: per-edge ratios for each level, plus summary.

    Edges are keyed `"u|v|k"` because JSON object keys must be strings.
    Ratios are rounded to keep the dashboard bundle small; 4 decimals is
    far finer than any colour scale can show.
    """
    per_level: dict[str, dict[str, float]] = {}
    summary: dict[str, Any] = {}
    for level in levels:
        snapshot = ctx.snapshot(level, congestion_source)
        ratios = congestion_ratios(ctx.graph, snapshot)
        per_level[level] = {
            f"{u}|{v}|{k}": round(r, round_to) for (u, v, k), r in ratios.items()
        }
        values = sorted(ratios.values())
        n = len(values)
        summary[level] = {
            "n_edges": n,
            "median": values[n // 2] if n else None,
            "p90": values[int(0.90 * (n - 1))] if n else None,
            "max": values[-1] if n else None,
            # "free-flow" to within a tenth of a percent. On SUMO this is
            # dominated by edges no vehicle ever traversed.
            "share_at_freeflow": (
                sum(1 for r in values if r <= 1.001) / n if n else None
            ),
            "snapshot_meta": dict(snapshot.meta),
        }
    return {
        "congestion_source": congestion_source,
        "levels": list(levels),
        "ratios": per_level,
        "summary": summary,
    }


# ==========================================================================
# Artifact IO
# ==========================================================================

#: Stamped into every artifact so a stale bundle is identifiable rather
#: than merely wrong.
ARTIFACT_PROVENANCE = (
    "REGENERATED by Phase 9b (viz/traces.py) -- illustrative, NOT the "
    "sweep's measured results. The measured evidence is "
    "data/results/final_<confighash>.parquet."
)


def _artifact_header(ctx: TraceContext) -> dict[str, Any]:
    return {
        "provenance": ARTIFACT_PROVENANCE,
        "config_hash": ctx.config_hash,
        "area_config_path": str(ctx.cfg.area_config_path),
        "baseline_graph": ctx.meta.get("baseline"),
        "baseline_hash": ctx.meta.get("baseline_hash"),
        "graph_path": ctx.meta.get("graph_path"),
        "n_nodes": ctx.meta.get("n_nodes"),
        "n_edges": ctx.meta.get("n_edges"),
        "scenario_seed": int(ctx.cfg.scenario_seed),
        "od_seed": int(ctx.cfg.od.seed),
    }


def write_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Write `payload` as compact UTF-8 JSON, creating parents."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=False),
        encoding="utf-8",
    )
    return path


def write_traces(
    ctx: TraceContext, traces: Iterable[ConvergenceTrace], out_dir: str | Path | None = None
) -> Path:
    out_dir = Path(out_dir) if out_dir is not None else DEFAULT_TRACE_DIR
    payload = {
        **_artifact_header(ctx),
        "artifact": "convergence_traces",
        "figure": "fig01_convergence",
        "max_trace_points": MAX_TRACE_POINTS,
        "traces": [t.to_json() for t in traces],
    }
    return write_json(out_dir / f"traces_{ctx.config_hash}.json", payload)


def write_routes(
    ctx: TraceContext, bundles: Iterable[RouteBundle], out_dir: str | Path | None = None
) -> Path:
    out_dir = Path(out_dir) if out_dir is not None else DEFAULT_TRACE_DIR
    payload = {
        **_artifact_header(ctx),
        "artifact": "routes_before_after",
        "figure": "fig05_routes_before_after",
        "bundles": [b.to_json() for b in bundles],
    }
    return write_json(out_dir / f"routes_{ctx.config_hash}.json", payload)


def write_congestion(
    ctx: TraceContext, bundle: Mapping[str, Any], out_dir: str | Path | None = None
) -> Path:
    out_dir = Path(out_dir) if out_dir is not None else DEFAULT_TRACE_DIR
    payload = {
        **_artifact_header(ctx),
        "artifact": "congestion_ratios",
        "figure": "fig06_congestion_heatmap",
        **dict(bundle),
    }
    return write_json(out_dir / f"congestion_{ctx.config_hash}.json", payload)


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ==========================================================================
# Cross-check against the Parquet
# ==========================================================================


@dataclass
class CrossCheck:
    """One re-run value against the same cell's Parquet value."""

    key: str
    field: str
    regenerated: float | None
    parquet: float | None
    match: bool

    @property
    def delta(self) -> float | None:
        if self.regenerated is None or self.parquet is None:
            return None
        return self.regenerated - self.parquet


def crosscheck_traces(
    traces: Sequence[ConvergenceTrace],
    parquet_path: str | Path,
    tol: float = 1e-6,
) -> list[CrossCheck]:
    """Compare each regenerated S0 solve to its Parquet row.

    Compares COSTS and STRETCHES only, never wall-clock: CLAUDE.md records
    that this machine's throughput swings up to 3.4x with thermal state, so
    a timing difference is expected and a cost difference is not. A
    mismatch here means the figures and the measured results disagree, and
    is reported rather than tolerated.
    """
    import pandas as pd  # noqa: PLC0415

    df = pd.read_parquet(parquet_path)
    out: list[CrossCheck] = []
    for t in traces:
        rows = df[
            (df.algorithm == t.algorithm)
            & (df.od_source == t.od_source)
            & (df.od_target == t.od_target)
            & (df.scenario_id == t.scenario_id)
            & (df.traffic_level == t.traffic_level)
            & (df.congestion_source == t.congestion_source)
            & (df.seed == t.seed)
            & (~df.warm_start)
        ]
        key = (
            f"{t.algorithm}/seed{t.seed} {t.od_source}->{t.od_target} "
            f"{t.scenario_id}/{t.traffic_level}"
        )
        if rows.empty:
            out.append(CrossCheck(key, "path_cost", t.best_cost, None, False))
            continue
        row = rows.iloc[0]
        for attr, column in (("best_cost", "path_cost"), ("stretch", "stretch")):
            mine = getattr(t, attr)
            theirs = row[column]
            theirs = None if theirs is None or pd.isna(theirs) else float(theirs)
            match = (
                mine is None and theirs is None
            ) or (
                mine is not None
                and theirs is not None
                and abs(mine - theirs) <= tol * max(1.0, abs(theirs))
            )
            out.append(CrossCheck(key, column, mine, theirs, bool(match)))
    return out


def crosscheck_routes(
    bundle: RouteBundle, parquet_path: str | Path, tol: float = 1e-6
) -> list[CrossCheck]:
    """Compare a regenerated disrupted cell to its Parquet rows."""
    import pandas as pd  # noqa: PLC0415

    df = pd.read_parquet(parquet_path)
    out: list[CrossCheck] = []
    for rec in bundle.routes:
        rows = df[
            (df.algorithm == rec.algorithm)
            & (df.od_source == bundle.od_source)
            & (df.od_target == bundle.od_target)
            & (df.scenario_id == bundle.scenario_id)
            & (df.traffic_level == bundle.traffic_level)
            & (df.congestion_source == bundle.congestion_source)
            & (df.seed == rec.seed)
            & (df.warm_start == rec.warm_start)
        ]
        key = (
            f"{rec.algorithm}/seed{rec.seed} "
            f"{bundle.od_source}->{bundle.od_target} "
            f"{bundle.scenario_id}/{bundle.traffic_level}/"
            f"{'warm' if rec.warm_start else 'cold'}"
        )
        if rows.empty:
            out.append(CrossCheck(key, "path_cost", rec.cost_before, None, False))
            continue
        row = rows.iloc[0]
        pairs = (
            ("cost_before", "path_cost"),
            ("stretch_before", "stretch"),
            ("cost_after", "post_disruption_cost"),
            ("stretch_after", "post_disruption_stretch"),
        )
        for attr, column in pairs:
            mine = getattr(rec, attr)
            theirs = row[column]
            theirs = None if theirs is None or pd.isna(theirs) else float(theirs)
            match = (mine is None and theirs is None) or (
                mine is not None
                and theirs is not None
                and abs(mine - theirs) <= tol * max(1.0, abs(theirs))
            )
            out.append(CrossCheck(key, column, mine, theirs, bool(match)))
    return out


# ==========================================================================
# Figure 1 -- convergence curves
# ==========================================================================

FIG1_CAPTION = (
    "Figure 1. Convergence of ACO and Physarum on three OD pairs, one per "
    "distance stratum, scenario S0 (no disruption) under SUMO heavy demand. "
    "y is stretch = cost / Dijkstra cost on the same snapshot, so the "
    "optimum is the Dijkstra line at exactly 1.0. Note the y-RANGES DIFFER "
    "per panel (the short pair's worst early ant is 5.7x optimal and the "
    "long pair's 3.6x); every panel's floor is the same 1.0. x is log-scaled "
    "because almost all of the improvement happens in the first ~30 "
    "iterations. "
    "Thin ACO lines are the sweep's five seeds, the thick line their "
    "per-iteration median: the spread is a finding, not noise (Phase 4d). "
    "Dijkstra and A* are one-shot, so they are drawn as levels rather than "
    "curves (A* dashed on top of Dijkstra, which it traces exactly). "
    "Physarum's curve is coloured and its panel title labelled by the "
    "PROVENANCE of its reported path -- only max_flux_walk is the model's "
    "own extraction; the two fallbacks are Dijkstra on a subgraph, so a "
    "green curve reaching 1.0 is not the model solving the problem. ILLUSTRATIVE: regenerated by a seeded "
    "re-run (Phase 9b), not read from the sweep's results Parquet."
)


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def _median_curve(
    traces: Sequence[ConvergenceTrace],
) -> tuple[list[int], list[float]]:
    """Per-iteration median over several seeds' stretch series.

    Seeds stop at different iterations (MMAS's own convergence fires on
    only 79%/69% of solves), so a series is HELD AT ITS FINAL VALUE past
    its own end rather than dropped from the median. Dropping it would
    make the median jump upward at the moment a good seed stops, which
    reads as the colony getting worse.
    """
    if not traces:
        return [], []
    # Work on the dense series: each trace carries explicit indices, so
    # rebuild a per-iteration view by forward-filling between kept points.
    dense: list[list[float | None]] = []
    longest = 0
    for t in traces:
        total = max(t.iteration_index) + 1 if t.iteration_index else 0
        series: list[float | None] = [None] * total
        for pos, i in enumerate(t.iteration_index):
            series[i] = t.stretches[pos]
        last: float | None = None
        for i in range(total):
            if series[i] is None:
                series[i] = last
            else:
                last = series[i]
        dense.append(series)
        longest = max(longest, total)

    xs: list[int] = []
    ys: list[float] = []
    for i in range(longest):
        vals: list[float] = []
        for series in dense:
            if not series:
                continue
            v = series[i] if i < len(series) else series[-1]
            if v is not None:
                vals.append(float(v))
        if vals:
            xs.append(i + 1)  # 1-based iteration numbers
            ys.append(_median(vals))
    return xs, ys


def _plot_series(ax, trace: ConvergenceTrace, **kwargs) -> None:
    xs = [i + 1 for i, v in zip(trace.iteration_index, trace.stretches) if v is not None]
    ys = [v for v in trace.stretches if v is not None]
    if xs:
        ax.plot(xs, ys, **kwargs)


def plot_convergence(
    traces: Sequence[ConvergenceTrace],
    out_stem: str | Path,
    strata_order: Sequence[str] = ("short", "medium", "long"),
) -> list[Path]:
    """Render figure 1 to `<out_stem>.png` and `<out_stem>.svg`.

    One panel per OD pair in `strata_order`. A step/line chart is the right
    form here: the data's job is change-over-iteration for a handful of
    identified series, which is exactly what a line chart does and what a
    bar or distribution plot would destroy.
    """
    import matplotlib.pyplot as plt  # noqa: PLC0415

    from bioroute.viz import style  # noqa: PLC0415

    style.apply_style()

    by_pair: dict[tuple[int, int], list[ConvergenceTrace]] = {}
    for t in traces:
        by_pair.setdefault((t.od_source, t.od_target), []).append(t)

    order = sorted(
        by_pair,
        key=lambda key: strata_order.index(by_pair[key][0].stratum)
        if by_pair[key][0].stratum in strata_order
        else len(strata_order),
    )

    # IEEE: FULL WIDTH, and all three strata KEPT. At 7.16 in each panel is
    # ~2.2 in, which a two-curve line chart survives; dropping a stratum
    # would be the wrong economy, because the finding this figure carries
    # is "Physarum's with-fallback curve sits at 1.0 from iteration 1 in
    # TWO OF THREE panels" -- that is a count, and a two-panel version
    # cannot state it.
    fig, axes = plt.subplots(
        1, len(order), sharey=False,
        figsize=style.pick(
            (4.0 * len(order), 3.5), (style.IEEE_PAGE_WIDTH_IN, 2.75),
        ),
    )
    if len(order) == 1:
        axes = [axes]

    legend_handles: dict[str, Any] = {}

    for ax, key in zip(axes, order):
        group = by_pair[key]
        stratum = group[0].stratum
        ref_hops = group[0].reference_hops
        provenance: list[str] = []

        # One-shot exact routers: a horizontal LEVEL, not a curve, so the
        # figure never implies they iterate. No separate "optimum" line is
        # drawn: Dijkstra IS the denominator, so its level is the optimum
        # at exactly 1.0, and a third line there would be the same line
        # three times over. A* is dashed on top of it because it traces
        # Dijkstra exactly and would otherwise be invisible.
        for algorithm in style.EXACT_ALGOS:
            exact = [t for t in group if t.algorithm == algorithm]
            if not exact or exact[0].stretch is None:
                continue
            h = ax.axhline(
                exact[0].stretch,
                color=style.color(algorithm),
                linewidth=style.pick(1.8, 1.3),
                alpha=0.95,
                linestyle=(0, (5, 3)) if algorithm == "astar" else "-",
                zorder=2 if algorithm == "dijkstra" else 6,
            )
            suffix = style.pick(
                " = the optimum, stretch 1.0", " = optimum 1.0",
            ) if algorithm == "dijkstra" else ""
            legend_handles.setdefault(
                f"{style.label(algorithm)} (one-shot){suffix}", h
            )

        # ACO: every seed thin, the per-iteration median thick.
        aco = sorted((t for t in group if t.algorithm == "aco"), key=lambda t: t.seed)
        for t in aco:
            _plot_series(
                ax,
                t,
                color=style.color("aco"),
                linewidth=style.pick(0.8, 0.6),
                alpha=0.45,
                zorder=3,
            )
        if aco:
            xs, ys = _median_curve(aco)
            (h,) = ax.plot(
                xs, ys, color=style.color("aco"),
                linewidth=style.pick(2.2, 1.5), zorder=5,
            )
            legend_handles.setdefault(
                f"{style.label('aco')}: {len(aco)} seeds + median", h
            )

        # Physarum: one seed (measured, not assumed -- see matrix.py), and
        # the provenance of its reported path goes in the legend.
        for t in (t for t in group if t.algorithm == "physarum"):
            series_key = (
                "physarum_walk"
                if t.best_path_extraction_method == "max_flux_walk"
                else "physarum"
            )
            _plot_series(
                ax,
                t,
                color=style.color(series_key),
                linewidth=style.pick(2.0, 1.5),
                zorder=4,
            )
            # ONE legend entry per series key, never one per extraction
            # method: the two fallbacks share the with-fallback colour, so
            # method-level entries would print the same swatch twice. The
            # method itself goes in the PANEL TITLE, where it belongs --
            # one Physarum run per panel means one provenance per panel.
            legend_handles.setdefault(
                style.label(series_key),
                plt.Line2D([], [], color=style.color(series_key),
                           linewidth=style.pick(2.0, 1.5)),
            )
            provenance.append(t.best_path_extraction_method or "unknown")

        # The OD node ids are 10-11 digits each and would be ~1.6 in of
        # title on a 2.2 in panel; they move to the caption file, which is
        # where a reader who wants to re-run the cell will look anyway.
        title = style.pick(
            f"{stratum} — {key[0]}→{key[1]}\n"
            f"{group[0].reference_cost:.0f} s optimum, {ref_hops} hops",
            f"{stratum}: {ref_hops} hops, "
            f"{group[0].reference_cost:.0f} s optimum",
        )
        if provenance:
            title += style.pick(
                "\nPhysarum path from: ", "\nPhysarum: ",
            ) + "/".join(provenance)
        ax.set_title(title, fontsize=style.pick(8, 7))
        ax.set_xlabel("iteration")
        ax.set_xscale("log")
        ax.margins(x=0.02)

    axes[0].set_ylabel(
        style.pick("stretch  (cost / Dijkstra cost)", "stretch")
    )

    fig.legend(
        legend_handles.values(),
        legend_handles.keys(),
        loc="lower center",
        ncol=style.pick(2, 3),
        bbox_to_anchor=style.pick((0.5, -0.20), (0.5, -0.004)),
    )
    if style.is_ieee():
        # The "illustrative" label is a standing honesty requirement for
        # figures 1, 5 and 6 (they are a seeded re-run, not the sweep's
        # measured evidence). The suptitle goes, so the label is kept as
        # one small line rather than deferred to the LaTeX caption alone.
        fig.text(
            0.5, 0.985,
            "illustrative seeded re-run — not the sweep's measured results",
            ha="center", va="top", fontsize=6.5, color="#6B6B6B",
        )
        fig.subplots_adjust(
            left=0.062, right=0.995, top=0.835, bottom=0.265, wspace=0.17,
        )
    else:
        fig.suptitle(
            "Convergence under SUMO heavy demand (S0, no disruption)\n"
            "illustrative re-run — not the sweep's measured results",
            fontsize=10,
        )
        fig.tight_layout(rect=(0, 0, 1, 0.92))

    out_stem = Path(out_stem)
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for ext in style.output_formats():
        path = out_stem.with_suffix(f".{ext}")
        fig.savefig(path)
        written.append(path)
    plt.close(fig)
    return written
