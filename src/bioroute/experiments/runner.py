"""Resumable, parallel experiment runner.

IMPLEMENTED IN: Phase 8, driven by scripts/03_run_experiments.py.

This module executes the matrix `experiments/matrix.py` enumerates. It owns
scheduling, resume, parallelism and the shard bookkeeping -- and NOTHING
else. Measurement belongs to `metrics/collectors.run_and_collect`, closure
selection to `disruption/scenarios.generate_disruption`, and the Parquet
write path to `metrics/collectors.ShardWriter`; the runner calls all three
and reimplements none of them.

--------------------------------------------------------------------------
THE UNIT OF WORK IS A CELL, NOT A RUN
--------------------------------------------------------------------------

One parallel task = one `CellSpec` = one (OD pair, scenario, traffic
condition), and every algorithm/seed/warm-mode inside it runs in that same
task. That grouping is not a performance choice, it is a correctness one:

  * the pre-disruption Dijkstra reference cost,
  * the scenario's removed-edge set,
  * the disrupted graph,
  * the post-disruption Dijkstra reference cost

are computed ONCE per cell and passed into every `run_and_collect` call in
it via its precomputed arguments. If each algorithm computed its own
denominator, two algorithms in nominally the same cell could be scored
against different numbers -- a silent violation of the CLAUDE.md stretch
invariant that no downstream figure would reveal. The disruption in
particular is drawn from the CELL's `scenario_seed`, never from an
algorithm seed, so ACO seed 3 and ACO seed 4 are solving the identical
closure and the comparison stays paired.

--------------------------------------------------------------------------
CONCURRENCY DISCIPLINE (all of it paid for in earlier phases)
--------------------------------------------------------------------------

1. **Snapshots are pre-warmed serially, before any worker exists.**
   `congestion.base.prewarm_snapshots` is called from the parent process in
   `prepare_resources`, and the materialised snapshots are then written to
   the sweep's own bundle directory. Workers only ever READ. Phase 4c lost
   a 12-worker run to concurrent writers racing on one shared temp file;
   CLAUDE.md flags this requirement in capitals.

2. **One shard per worker.** Each worker process lazily creates a single
   `ShardWriter` (pid + random suffix in the filename) and appends to it,
   flushing after every cell. No two processes ever write one file, and a
   watchdog kill costs only the in-flight cell. `merge_shards` does the
   one-time combine at the end and de-duplicates on `run_id`.

3. **A failed run is a row.** `run_and_collect` already converts any
   per-run exception into `failed=True` with a reason. The runner adds the
   level above it: a cell that blows up entirely (unreachable OD pair,
   infeasible scenario, a router constructor that raises) still emits one
   failed row per run in that cell. Nothing is ever dropped silently.

4. **Wall-clock from a parallel run is NOT a performance number.** This
   machine's throughput swings up to 3.4x with thermal state (Phase 4b);
   the runner prints elapsed time for progress and planning only.

--------------------------------------------------------------------------
RESUME
--------------------------------------------------------------------------

`make_run_id` is the key. A resumed sweep reads every existing shard,
collects the `run_id`s already present, and drops them from the matrix; a
cell whose runs are all present is never scheduled at all. Because the
experiment config hash is folded into every `run_id`, changing any
experimental parameter changes every id and resume correctly refuses to
reuse the old rows (CLAUDE.md: changing a config must invalidate the
cache).
"""

from __future__ import annotations

import atexit
import json
import os
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import networkx as nx

from bioroute.algorithms.base import Router
from bioroute.congestion.base import (
    load_snapshot_cache,
    prewarm_snapshots,
    save_snapshot_cache,
)
from bioroute.congestion.snapshot import TrafficSnapshot
from bioroute.disruption.apply import apply_disruption
from bioroute.disruption.scenarios import DisruptionConfig, generate_disruption
from bioroute.experiments.matrix import (
    CellSpec,
    Condition,
    ExperimentConfig,
    RunSpec,
)
from bioroute.metrics.collectors import (
    SHARD_GLOB,
    ShardWriter,
    dijkstra_reference,
    merge_shards,
    read_shard,
    run_and_collect,
    utc_now_iso,
)
from bioroute.metrics.schema import ResultRecord, ScenarioSpec

EdgeKey = tuple[int, int, int]


# ==========================================================================
# Router construction
# ==========================================================================
#
# A registry rather than an if/elif chain so that tests can register a stub
# (notably a deliberately failing router, to prove one bad cell does not
# abort the sweep) without the runner importing anything from tests.

RouterFactory = Callable[[Mapping[str, Any]], Router]

ROUTER_FACTORIES: dict[str, RouterFactory] = {}


def register_router(name: str, factory: RouterFactory) -> None:
    """Register a router factory under `name` (the config's algorithm
    name). Re-registering replaces -- tests rely on that."""
    ROUTER_FACTORIES[name] = factory


def _default_factories() -> None:
    """Install the project's four routers lazily.

    Deferred to first use rather than done at import time so that importing
    `runner` (for the matrix arithmetic, or in a test) does not drag in
    numpy/scipy via `algorithms.physarum`.
    """
    if ROUTER_FACTORIES:
        return
    from bioroute.algorithms.aco import ACORouter  # noqa: PLC0415
    from bioroute.algorithms.classical import AStarRouter, DijkstraRouter  # noqa: PLC0415
    from bioroute.algorithms.physarum import PhysarumRouter  # noqa: PLC0415

    register_router("dijkstra", lambda opts: DijkstraRouter(**dict(opts)))
    register_router("astar", lambda opts: AStarRouter(**dict(opts)))
    register_router("aco", lambda opts: ACORouter(**dict(opts)))
    register_router("physarum", lambda opts: PhysarumRouter(**dict(opts)))


def make_router(name: str, options: Mapping[str, Any] | None = None) -> Router:
    _default_factories()
    try:
        factory = ROUTER_FACTORIES[name]
    except KeyError as exc:
        raise KeyError(
            f"No router registered for algorithm {name!r}; known: "
            f"{sorted(ROUTER_FACTORIES)}"
        ) from exc
    return factory(options or {})


# ==========================================================================
# Resources: the graph and the pre-warmed snapshots
# ==========================================================================


@dataclass(frozen=True)
class ResourceSpec:
    """A picklable pointer to everything a worker needs.

    Deliberately paths, not objects: shipping a 2,856-node graph and a
    7,015-entry travel-time dict in every task payload would dominate the
    runtime of the cheap cells. A worker materialises them once and caches
    them at module level for the life of the process.
    """

    graph_path: str
    baseline_graph: str
    #: "{source}|{level}|{time_bin}" -> path to a cached snapshot JSON.
    snapshot_paths: Mapping[str, str]


def _condition_key(condition: Condition) -> str:
    return f"{condition.congestion_source}|{condition.level}|{condition.time_bin}"


class Resources:
    """The graph plus every snapshot the sweep needs, in memory."""

    def __init__(
        self,
        graph: nx.MultiDiGraph,
        snapshots: Mapping[str, TrafficSnapshot],
        baseline_graph: str = "",
    ):
        self.graph = graph
        self.snapshots = dict(snapshots)
        self.baseline_graph = baseline_graph

    def snapshot(self, condition: Condition) -> TrafficSnapshot:
        key = _condition_key(condition)
        try:
            return self.snapshots[key]
        except KeyError as exc:
            raise KeyError(
                f"No pre-warmed snapshot for condition {key!r}; have "
                f"{sorted(self.snapshots)}"
            ) from exc

    @classmethod
    def from_spec(cls, spec: ResourceSpec) -> "Resources":
        from bioroute.network.graph import load_cached_graph  # noqa: PLC0415

        graph = load_cached_graph(spec.graph_path)
        graph.graph.setdefault("baseline", spec.baseline_graph)
        snapshots: dict[str, TrafficSnapshot] = {}
        for key, path in spec.snapshot_paths.items():
            snap = load_snapshot_cache(path)
            if snap is None:
                raise RuntimeError(
                    f"Pre-warmed snapshot {key!r} missing or corrupt at {path}. "
                    "prepare_resources() must run serially in the parent before "
                    "any worker starts."
                )
            snapshots[key] = snap
        return cls(graph, snapshots, spec.baseline_graph)


#: Per-process cache so each worker materialises the graph and snapshots
#: exactly once, however many cells it is handed.
_WORKER_RESOURCES: dict[Any, Resources] = {}


def _resources_for(spec: ResourceSpec) -> Resources:
    key = (spec.graph_path, spec.baseline_graph, tuple(sorted(spec.snapshot_paths.items())))
    cached = _WORKER_RESOURCES.get(key)
    if cached is None:
        cached = Resources.from_spec(spec)
        _WORKER_RESOURCES[key] = cached
    return cached


def prepare_resources(
    cfg: ExperimentConfig,
    bundle_dir: str | Path,
    verbose: bool = True,
) -> tuple[Resources, ResourceSpec, dict[str, Any]]:
    """Load the baseline graph and SERIALLY pre-warm every snapshot.

    THIS MUST RUN IN THE PARENT PROCESS, BEFORE ANY PARALLEL SECTION
    (CLAUDE.md, Phase 4d). After it returns, every snapshot the sweep can
    ask for exists as a complete file in `bundle_dir`, and the workers do
    nothing but read -- concurrent readers of a complete file are safe on
    every platform; it is the concurrent writers that are not.
    """
    from bioroute.config import config_hash, load_area_config  # noqa: PLC0415

    bundle_dir = Path(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    area_cfg = load_area_config(cfg.area_config_path)
    area_hash = config_hash(area_cfg)
    graph, info = load_sweep_graph(cfg, area_cfg)
    if verbose:
        print(
            f"  graph: {graph.number_of_nodes()} nodes / {graph.number_of_edges()} edges "
            f"[baseline={info['baseline']} hash={info['baseline_hash'] or '-'}]",
            flush=True,
        )

    # Group the required (level, time_bin) pairs by congestion source so
    # each provider is constructed at most once -- SUMOProvider alone reads
    # a 41 MB net and a 59 MB edgedata file.
    wanted: dict[str, list[tuple[str, int]]] = {}
    for condition in cfg.conditions:
        wanted.setdefault(condition.congestion_source, []).append(
            (condition.level, condition.time_bin)
        )

    snapshots: dict[str, TrafficSnapshot] = {}
    snapshot_paths: dict[str, str] = {}
    for source, pairs in wanted.items():
        provider = _build_provider(source, graph, area_cfg, verbose=verbose)
        levels = sorted({lvl for lvl, _ in pairs})
        bins = sorted({b for _, b in pairs})
        if verbose:
            print(
                f"  pre-warming {source} snapshots serially: levels={levels} bins={bins}",
                flush=True,
            )
        warmed = prewarm_snapshots(provider, levels=levels, time_bins=bins, verbose=verbose)
        for level, time_bin in pairs:
            snap = warmed[(level, time_bin)]
            key = f"{source}|{level}|{time_bin}"
            path = bundle_dir / f"snapshot_{source}_{level}_bin{time_bin}.json"
            save_snapshot_cache(path, snap)
            snapshots[key] = snap
            snapshot_paths[key] = str(path)

    spec = ResourceSpec(
        graph_path=str(info["path"]),
        baseline_graph=str(info["baseline"]),
        snapshot_paths=snapshot_paths,
    )
    meta = {
        "area_config_hash": area_hash,
        "baseline": info["baseline"],
        "baseline_hash": info["baseline_hash"],
        "graph_path": str(info["path"]),
        "n_nodes": graph.number_of_nodes(),
        "n_edges": graph.number_of_edges(),
        "snapshot_paths": dict(snapshot_paths),
    }
    return Resources(graph, snapshots, str(info["baseline"])), spec, meta


def netconvert_flags_for(cfg: ExperimentConfig) -> list[str]:
    """The netconvert flag list that identifies the `sumo_speeds` baseline
    artifact.

    `sumo_speeds_baseline_hash` folds these flags into the artifact's cache
    path, so loading the baseline with the WRONG (or an empty) flag list
    does not silently load the wrong graph -- it fails to find any graph at
    all. Reading them from configs/sumo.yaml is therefore mandatory, not
    cosmetic."""
    import yaml  # noqa: PLC0415

    path = Path(cfg.sumo_config_path)
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [str(f) for f in raw.get("netconvert_flags", [])]


def load_sweep_graph(cfg: ExperimentConfig, area_cfg=None):
    """Load THE graph the whole sweep runs on, under `cfg.baseline_graph`."""
    from bioroute.config import load_area_config  # noqa: PLC0415
    from bioroute.network.graph import load_baseline_graph  # noqa: PLC0415

    if area_cfg is None:
        area_cfg = load_area_config(cfg.area_config_path)
    return load_baseline_graph(
        area_cfg,
        baseline=cfg.baseline_graph,
        netconvert_flags=netconvert_flags_for(cfg),
    )


def _build_provider(source: str, graph, area_cfg, verbose: bool = False):
    """Construct the congestion provider named by a condition."""
    if source == "freeflow":
        from bioroute.congestion.base import FreeFlowProvider  # noqa: PLC0415

        return FreeFlowProvider(graph)
    if source == "bpr":
        from bioroute.congestion.bpr import BPRProvider  # noqa: PLC0415

        return BPRProvider(graph)
    if source == "sumo":
        from bioroute.congestion.sumo import SUMOProvider  # noqa: PLC0415

        if verbose:
            print("  constructing SUMOProvider (reads net.net.xml + edgedata) ...", flush=True)
        return SUMOProvider(graph, area_cfg)
    raise ValueError(
        f"Unknown congestion_source {source!r}; expected freeflow / bpr / sumo"
    )


# ==========================================================================
# Shard bookkeeping
# ==========================================================================

_WORKER_WRITERS: dict[str, ShardWriter] = {}


def _writer_for(shard_dir: str | Path, flush_every: int = 5) -> ShardWriter:
    """This process's single shard writer for `shard_dir`.

    One per worker, created on first use and flushed at process exit as
    well as after every cell -- so a watchdog kill costs the in-flight cell
    and nothing more.
    """
    key = str(Path(shard_dir).resolve())
    writer = _WORKER_WRITERS.get(key)
    if writer is None:
        writer = ShardWriter(shard_dir, flush_every=max(1, int(flush_every)))
        _WORKER_WRITERS[key] = writer
        atexit.register(_flush_writers)
    return writer


def _flush_writers() -> None:
    for writer in list(_WORKER_WRITERS.values()):
        try:
            writer.flush()
        except Exception:  # pragma: no cover - best effort at shutdown
            pass


def close_writers() -> None:
    """Flush and forget this process's writers (tests; the in-process
    sequential path, which must not keep a writer alive across sweeps)."""
    _flush_writers()
    _WORKER_WRITERS.clear()


def completed_run_ids(shard_dir: str | Path) -> set[str]:
    """Every `run_id` already present in a shard under `shard_dir`.

    A shard that cannot be read is NOT skipped silently -- it raises, so a
    resume can never quietly re-run (or quietly lose) measured evidence.
    """
    shard_dir = Path(shard_dir)
    if not shard_dir.exists():
        return set()
    done: set[str] = set()
    for shard in sorted(shard_dir.glob(SHARD_GLOB)):
        table = read_shard(shard)
        done.update(table.column("run_id").to_pylist())
    return done


def filter_completed(
    cells: Sequence[CellSpec], done: set[str]
) -> tuple[list[CellSpec], int]:
    """Drop runs already measured, and drop cells left with no runs.

    Returns (remaining cells, number of runs skipped). A partially finished
    cell keeps its outstanding runs; its Dijkstra reference is recomputed,
    which is cheap and keeps the denominator derived the same way it was
    the first time.
    """
    if not done:
        return list(cells), 0
    out: list[CellSpec] = []
    skipped = 0
    for cell in cells:
        keep = tuple(r for r in cell.runs if r.run_id not in done)
        skipped += len(cell.runs) - len(keep)
        if keep:
            out.append(
                CellSpec(
                    od=cell.od,
                    scenario_id=cell.scenario_id,
                    condition=cell.condition,
                    scenario_seed=cell.scenario_seed,
                    runs=keep,
                )
            )
    return out, skipped


# ==========================================================================
# Executing one cell
# ==========================================================================


def _failed_record(
    run: RunSpec, reason: str, snapshot: TrafficSnapshot | None, n_removed: int = 0
) -> ResultRecord:
    """A complete, schema-valid `failed=True` row for a run that could not
    even be attempted. CLAUDE.md: failures are recorded, never dropped."""
    meta = (snapshot.meta if snapshot is not None else {}) or {}
    return ResultRecord(
        run_id=run.run_id,
        algorithm=run.algorithm,
        od_source=int(run.od_source),
        od_target=int(run.od_target),
        scenario_id=run.scenario_id,
        traffic_level=run.traffic_level,
        congestion_source=run.congestion_source,
        baseline_graph=run.baseline_graph,
        baseline_hash=str(meta.get("baseline_hash", "")),
        warm_start=bool(run.warm_start),
        seed=int(run.seed),
        config_hash=run.config_hash,
        timestamp_utc=utc_now_iso(),
        iterations=0,
        iteration_budget=int(run.budget),
        converged=False,
        wallclock_s=0.0,
        peak_memory_kb=0.0,
        failed=True,
        failure_reason=reason[:400],
        n_removed_edges=int(n_removed),
    )


@dataclass
class CellOutcome:
    """What one cell produced. Returned from a worker INSTEAD of the rows
    themselves -- the rows are already on disk in the worker's shard, and
    shipping them back would put the whole results table through a pickle."""

    cell_key: tuple
    n_runs: int = 0
    n_failed: int = 0
    seconds: float = 0.0
    disruption: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def _disruption_config(raw: Mapping[str, Any]) -> DisruptionConfig:
    allowed = {
        "max_attempts",
        "both_directions",
        "close_parallel_edges",
        "betweenness_k",
        "strict",
    }
    kwargs = {k: v for k, v in dict(raw).items() if k in allowed}
    return DisruptionConfig(**kwargs)


def execute_cell(
    cell: CellSpec,
    resources: Resources,
    algorithm_options: Mapping[str, Mapping[str, Any]],
    disruption_cfg: Mapping[str, Any],
    emit: Callable[[ResultRecord], None],
) -> CellOutcome:
    """Run every outstanding run in one cell and hand each record to `emit`.

    This is where the "one denominator per cell" requirement is actually
    enforced: the reference cost, the removed-edge set, the disrupted graph
    and the disrupted reference cost are computed here, once, and passed
    into every `run_and_collect` call below.
    """
    started = time.perf_counter()
    outcome = CellOutcome(cell_key=cell.key, n_runs=len(cell.runs))
    G = resources.graph
    snapshot = resources.snapshot(cell.condition)
    source, target = cell.od.source, cell.od.target

    # -- the ONE denominator -------------------------------------------
    ref_path, ref_cost = dijkstra_reference(G, source, target, snapshot)
    if ref_cost is None:
        reason = f"no path from {source} to {target} on the undisrupted graph"
        for run in cell.runs:
            emit(_failed_record(run, reason, snapshot))
        outcome.n_failed = len(cell.runs)
        outcome.seconds = time.perf_counter() - started
        outcome.error = reason
        return outcome

    removed: list[EdgeKey] = []
    G_disrupted: nx.MultiDiGraph | None = None
    disrupted_ref_cost: float | None = None

    if not cell.is_control:
        disruption = generate_disruption(
            G,
            source,
            target,
            snapshot,
            cell.scenario_id,
            seed=cell.scenario_seed,
            path=ref_path,
            config=_disruption_config(disruption_cfg),
        )
        # Phase 7 asks for the rejection count to be RECORDED by Phase 8,
        # never swallowed: a high rejection rate is a finding about how
        # fragile this road network is. The results schema is fixed (Phase
        # 6), so it travels in the sweep's disruption sidecar instead.
        outcome.disruption = {
            "cell": list(cell.key),
            "scenario_id": cell.scenario_id,
            "od_source": source,
            "od_target": target,
            "congestion_source": cell.condition.congestion_source,
            "traffic_level": cell.condition.level,
            "time_bin": cell.condition.time_bin,
            "scenario_seed": cell.scenario_seed,
            "ok": bool(disruption.ok),
            "rejections": int(disruption.rejections),
            "attempts": int(disruption.attempts),
            "n_removed_edges": disruption.n_removed,
            "reason": disruption.reason,
            "meta": _jsonable(disruption.meta),
        }
        if not disruption.ok:
            reason = f"scenario {cell.scenario_id} infeasible: {disruption.reason}"
            for run in cell.runs:
                emit(_failed_record(run, reason, snapshot))
            outcome.n_failed = len(cell.runs)
            outcome.seconds = time.perf_counter() - started
            return outcome

        removed = list(disruption.removed_edges)
        # A VIEW, never a copy: `apply_disruption`'s module docstring is
        # explicit that a per-run copy of this graph is the difference
        # between minutes and hours across a full sweep.
        G_disrupted = apply_disruption(G, removed)
        _, disrupted_ref_cost = dijkstra_reference(
            G_disrupted, source, target, snapshot
        )
        if disrupted_ref_cost is None:
            reason = (
                f"scenario {cell.scenario_id} left no post-disruption Dijkstra "
                "reference (connectivity not preserved)"
            )
            for run in cell.runs:
                emit(_failed_record(run, reason, snapshot, n_removed=len(removed)))
            outcome.n_failed = len(cell.runs)
            outcome.seconds = time.perf_counter() - started
            outcome.error = reason
            return outcome

    spec = ScenarioSpec(cell.scenario_id, list(removed))

    for run in cell.runs:
        try:
            router = make_router(run.algorithm, algorithm_options.get(run.algorithm, {}))
        except Exception as exc:  # noqa: BLE001 - a bad router is a ROW
            emit(
                _failed_record(
                    run,
                    f"router construction failed: {type(exc).__name__}: {exc}",
                    snapshot,
                    n_removed=len(removed),
                )
            )
            outcome.n_failed += 1
            continue

        try:
            record = run_and_collect(
                router,
                G,
                source,
                target,
                snapshot,
                scenario=spec,
                budget=run.budget,
                warm=run.warm_start,
                seed=run.seed,
                config_hash=run.config_hash,
                algorithm=run.algorithm,
                traffic_level=run.traffic_level,
                congestion_source=run.congestion_source,
                baseline_graph=run.baseline_graph,
                run_id=run.run_id,
                reference_cost=ref_cost,
                disrupted_reference_cost=disrupted_ref_cost,
                G_disrupted=G_disrupted,
            )
        except Exception as exc:  # noqa: BLE001
            # run_and_collect only lets a SchemaViolation escape (a Phase 6
            # bug). Even that must not abort a multi-hour sweep -- it
            # becomes a failed row and the sweep summary reports it.
            record = _failed_record(
                run,
                f"collector raised: {type(exc).__name__}: {exc}",
                snapshot,
                n_removed=len(removed),
            )
        emit(record)
        if record.failed:
            outcome.n_failed += 1

    outcome.seconds = time.perf_counter() - started
    return outcome


def _jsonable(obj: Any) -> Any:
    """Make Phase 7's scenario meta JSON-safe (it carries tuples and numpy
    scalars in places)."""
    if isinstance(obj, Mapping):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


# ==========================================================================
# The parallel task
# ==========================================================================


def _run_cell_task(
    cell: CellSpec,
    spec: ResourceSpec,
    algorithm_options: Mapping[str, Mapping[str, Any]],
    disruption_cfg: Mapping[str, Any],
    shard_dir: str,
    flush_every: int,
) -> CellOutcome:
    """Worker entry point. Materialises (once per process) the graph and
    snapshots, runs the cell, writes to THIS worker's shard."""
    try:
        resources = _resources_for(spec)
        writer = _writer_for(shard_dir, flush_every)
        outcome = execute_cell(
            cell,
            resources,
            algorithm_options,
            disruption_cfg,
            emit=writer.append,
        )
        writer.flush()
        return outcome
    except Exception as exc:  # noqa: BLE001 - one bad cell must not kill the sweep
        return CellOutcome(
            cell_key=cell.key,
            n_runs=len(cell.runs),
            n_failed=len(cell.runs),
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}"[:800],
        )


# ==========================================================================
# The sweep
# ==========================================================================


def run_sweep(
    cfg: ExperimentConfig,
    cells: Sequence[CellSpec],
    resources: Resources | None = None,
    spec: ResourceSpec | None = None,
    workers: int = 1,
    shard_dir: str | Path | None = None,
    progress: bool = True,
    merge: bool = True,
    extra_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute `cells`, writing shards and (optionally) merging them.

    Either `resources` (in-process, workers == 1) or `spec` (parallel) must
    be given. `prepare_resources` returns both, so the caller normally
    passes both and the runner picks by `workers`.
    """
    shard_dir = Path(shard_dir) if shard_dir is not None else cfg.shard_dir
    shard_dir.mkdir(parents=True, exist_ok=True)
    algorithm_options = {a.name: dict(a.options) for a in cfg.algorithms}
    disruption_cfg = dict(cfg.disruption)

    workers = max(1, int(workers))
    if workers > 1 and spec is None:
        raise ValueError("Parallel execution needs a ResourceSpec (paths workers can load)")
    if workers == 1 and resources is None:
        if spec is None:
            raise ValueError("run_sweep needs either `resources` or `spec`")
        resources = Resources.from_spec(spec)

    started = time.perf_counter()
    outcomes: list[CellOutcome] = []
    bar = _progress_bar(len(cells), enabled=progress)

    try:
        if workers == 1:
            writer = _writer_for(shard_dir, cfg.flush_every)
            for cell in cells:
                try:
                    outcome = execute_cell(
                        cell, resources, algorithm_options, disruption_cfg, writer.append
                    )
                except Exception as exc:  # noqa: BLE001
                    outcome = CellOutcome(
                        cell_key=cell.key,
                        n_runs=len(cell.runs),
                        n_failed=len(cell.runs),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                writer.flush()
                outcomes.append(outcome)
                _tick(bar, outcomes)
            close_writers()
        else:
            from joblib import Parallel, delayed  # noqa: PLC0415

            parallel = Parallel(
                n_jobs=workers,
                backend="loky",
                return_as="generator_unordered",
                batch_size=1,
                pre_dispatch="2*n_jobs",
            )
            tasks = (
                delayed(_run_cell_task)(
                    cell,
                    spec,
                    algorithm_options,
                    disruption_cfg,
                    str(shard_dir),
                    cfg.flush_every,
                )
                for cell in cells
            )
            for outcome in parallel(tasks):
                outcomes.append(outcome)
                _tick(bar, outcomes)
    finally:
        if bar is not None:
            bar.close()

    elapsed = time.perf_counter() - started

    disruptions = [o.disruption for o in outcomes if o.disruption]
    if disruptions:
        _append_disruption_log(shard_dir, disruptions)

    summary: dict[str, Any] = {
        "name": cfg.name,
        "config_hash": cfg.config_hash,
        "cells_run": len(outcomes),
        "runs_attempted": sum(o.n_runs for o in outcomes),
        "runs_failed": sum(o.n_failed for o in outcomes),
        "cell_errors": [
            {"cell": list(o.cell_key), "error": o.error} for o in outcomes if o.error
        ],
        "wallclock_s": elapsed,
        "workers": workers,
        "shard_dir": str(shard_dir),
        "timestamp_utc": utc_now_iso(),
    }
    attempted = summary["runs_attempted"] or 1
    summary["failed_rate"] = summary["runs_failed"] / attempted
    summary["disruption_rejections_total"] = sum(
        int(d.get("rejections", 0)) for d in disruptions
    )
    summary["disruption_infeasible"] = sum(1 for d in disruptions if not d.get("ok"))
    if extra_meta:
        summary["resources"] = dict(extra_meta)

    if merge:
        out_path = cfg.final_path
        merge_shards(shard_dir, out_path)
        summary["output_path"] = str(out_path)
        table = read_shard(out_path)
        summary["rows_in_output"] = table.num_rows
        summary["failed_rows_in_output"] = sum(
            1 for f in table.column("failed").to_pylist() if f
        )
        summary["failed_rate_in_output"] = (
            summary["failed_rows_in_output"] / table.num_rows if table.num_rows else 0.0
        )
    _write_summary(shard_dir, summary)
    return summary


def _progress_bar(total: int, enabled: bool):
    if not enabled:
        return None
    try:
        from tqdm import tqdm  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return None
    return tqdm(total=total, unit="cell", dynamic_ncols=True)


def _tick(bar, outcomes: Sequence[CellOutcome]) -> None:
    if bar is None:
        return
    failed = sum(o.n_failed for o in outcomes)
    runs = sum(o.n_runs for o in outcomes)
    bar.set_postfix_str(f"runs={runs} failed={failed}")
    bar.update(1)


def _append_disruption_log(shard_dir: Path, entries: Sequence[Mapping[str, Any]]) -> None:
    """Phase 7's rejection counts and chosen closures, one JSON object per
    line. Appended (not rewritten) so a resumed sweep accumulates rather
    than overwrites, and written per-sweep-process so concurrent sweeps
    cannot interleave inside one line."""
    path = shard_dir / f"disruptions_{os.getpid():06d}_{uuid.uuid4().hex[:8]}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _write_summary(shard_dir: Path, summary: Mapping[str, Any]) -> None:
    path = shard_dir / f"summary_{os.getpid():06d}_{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")


def measured_seconds_per_solve(parquet_path: str | Path) -> dict[str, float]:
    """Median seconds per SOLVE, per algorithm, from a finished results file.

    Used to turn a smoke run into a cost projection for the full sweep.
    A disruption row covers two solves (initial + recovery), so its total
    time is halved before taking the median -- the same per-solve unit
    `matrix.project_cost` consumes.

    THE RESULTING NUMBER IS A PLANNING ESTIMATE, NOT A BENCHMARK. If the
    smoke run that produced it was parallel, every solve in it competed for
    cores with 13 others on a machine whose throughput swings up to 3.4x
    with thermal state.
    """
    import statistics  # noqa: PLC0415

    table = read_shard(parquet_path)
    cols = {
        name: table.column(name).to_pylist()
        for name in ("algorithm", "wallclock_s", "recovery_wallclock_s", "failed")
    }
    buckets: dict[str, list[float]] = {}
    for algo, wall, rec, failed in zip(
        cols["algorithm"], cols["wallclock_s"], cols["recovery_wallclock_s"], cols["failed"]
    ):
        if failed:
            continue
        total = float(wall or 0.0)
        solves = 1
        if rec is not None:
            total += float(rec)
            solves = 2
        buckets.setdefault(algo, []).append(total / solves)
    return {k: statistics.median(v) for k, v in sorted(buckets.items()) if v}
