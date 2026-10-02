"""Metric computation from Router results, and the concurrency-safe
Parquet write path.

IMPLEMENTED IN: Phase 6.

This module is the measurement layer Phase 8's sweep runs through. It owns
three things:

1. **The metric definitions.** Each one is a small, separately testable
   function so the definition lives in exactly one place:

   | Metric | Definition |
   |---|---|
   | `compute_stretch` | `cost_alg / cost_dijkstra`, both via `TrafficSnapshot.cost_of_path` on the SAME graph and SAME snapshot, so the min-over-parallel-edges convention is identical on both sides of the ratio. Raises if the ratio lands below 1.0 (CLAUDE.md: that is a bug, not a result). |
   | `first_valid_path_iteration` | The iteration at which the router first produced ANY path avoiding every severed edge -- measured separately from convergence, because a method can find a valid route fast and a good one slowly. |
   | `recovery_iterations` / `recovery_wallclock_s` | Disruption applied -> re-stabilised valid path. |
   | `post_disruption_stretch` | Against Dijkstra recomputed ON THE DISRUPTED GRAPH -- never against the pre-disruption optimum, which would conflate "the algorithm adapted badly" with "the network genuinely got worse". |
   | `peak_memory_kb` | `tracemalloc` peak across the whole measured solve. |
   | `failed` | No valid path within budget, or an exception. RECORDED as a row, never dropped. |

2. **`run_and_collect`** -- the single entry point Phase 8 calls. It drives
   `run_to_convergence` for the initial solve and (for a disruption
   scenario) for the recovery, instruments both, and returns one
   `ResultRecord`. It catches every per-run exception and returns a
   `failed=True` record instead of propagating: one bad OD pair must never
   kill a multi-hour sweep.

3. **The Parquet write path**, built to the discipline Phase 4d had to
   retrofit onto the congestion cache after a 12-worker race corrupted it
   (see `congestion/base.py`'s module docstring):

   * **one shard per worker**, never many writers appending to one file.
     The shard name carries pid + a random suffix, so two workers cannot
     even name the same final file;
   * every shard write goes to a **unique temp path** (pid + thread id +
     uuid), is **fsynced**, and is then `os.replace`d with bounded
     exponential-backoff **retry on Windows sharing violations**
     (ERROR_ACCESS_DENIED / ERROR_SHARING_VIOLATION -- Windows refuses the
     replace while ANY handle is open on the source or the destination,
     including a reader's);
   * `merge_shards` does the one-time combine at the end, reading each
     shard under the explicit `RESULT_SCHEMA` so shards never disagree on
     column type, and de-duplicating on `run_id` so a resumed sweep that
     re-ran a few cells does not double-count them.

   Note on tracemalloc: `wallclock_s` is measured WITH tracing active (one
   frame, the cheapest mode). That inflates absolute timings slightly, but
   identically for every algorithm, so the cross-algorithm comparisons the
   project reports remain fair. This is a deliberate trade for having a
   never-null memory column.
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import threading
import time
import tracemalloc
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import networkx as nx
import pyarrow as pa
import pyarrow.parquet as pq

from bioroute.algorithms.base import Router, RouterResult, run_to_convergence
from bioroute.congestion.snapshot import TrafficSnapshot
from bioroute.metrics.schema import (
    ALL_FIELDS,
    RESULT_SCHEMA,
    STRETCH_TOLERANCE,
    ResultRecord,
    ScenarioSpec,  # re-exported: Phase 8 builds these without importing schema
    SchemaViolation,
    as_scenario_spec,
    records_to_table,
    validate,
)

EdgeKey = tuple[int, int, int]

# Phase 2d made the free-flow denominator selectable; if a snapshot somehow
# carries no `baseline` provenance we fall back to Phase 1's graph rather
# than inventing a value. Literal rather than an import from network.graph
# so the metrics layer stays importable without osmnx.
DEFAULT_BASELINE_GRAPH = "config_speeds"

#: Max characters kept from an exception message in `failure_reason`. Long
#: tracebacks in a Parquet column are useless and bloat the results file.
FAILURE_REASON_MAX_CHARS = 400


# ==========================================================================
# Metric definitions
# ==========================================================================


class StretchError(ValueError):
    """Raised when a computed stretch falls below 1.0 -- i.e. an algorithm
    reportedly beat Dijkstra on the same weighted graph. CLAUDE.md is
    explicit that this is a bug and must assert, not be recorded."""


def dijkstra_reference(
    G: nx.MultiDiGraph, source: int, target: int, snapshot: TrafficSnapshot
) -> tuple[list[int] | None, float | None]:
    """The optimal path and its cost under `snapshot` on `G`.

    This is the denominator of every stretch in the project. It goes
    through `snapshot.nx_weight()` (min over parallel edges) for the search
    and `snapshot.cost_of_path` for the cost, so the denominator obeys the
    same path-cost convention as the numerator."""
    try:
        path = nx.shortest_path(
            G, source, target, weight=snapshot.nx_weight(), method="dijkstra"
        )
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None, None
    return list(path), snapshot.cost_of_path(G, list(path))


def compute_stretch(
    G: nx.MultiDiGraph,
    snapshot: TrafficSnapshot,
    path: Sequence[int],
    reference_cost: float,
) -> float:
    """`cost_alg / cost_dijkstra`, both measured on the SAME graph and the
    SAME snapshot with `TrafficSnapshot.cost_of_path`.

    Raises `StretchError` if the result is below 1.0 by more than
    `STRETCH_TOLERANCE`. Never compare stretch across differently-weighted
    graphs (CLAUDE.md invariant) -- pass the reference cost that was
    computed on this same (G, snapshot) pair."""
    if reference_cost is None or not math.isfinite(reference_cost) or reference_cost <= 0:
        raise StretchError(f"Invalid Dijkstra reference cost: {reference_cost!r}")
    cost = snapshot.cost_of_path(G, list(path))
    stretch = cost / reference_cost
    if stretch < 1.0 - STRETCH_TOLERANCE:
        raise StretchError(
            f"stretch = {stretch!r} (< 1.0): algorithm cost {cost!r} beats the "
            f"Dijkstra optimum {reference_cost!r} on the same weighted graph. "
            "This is a bug (CLAUDE.md invariant), not a result."
        )
    # Clamp the ulp-level case only -- anything larger already raised above.
    return max(stretch, 1.0)


def disrupted_graph(G: nx.MultiDiGraph, removed_edges: Iterable[EdgeKey]) -> nx.MultiDiGraph:
    """A copy of `G` with `removed_edges` severed.

    Phase 6 builds this itself rather than importing `disruption/apply.py`:
    the metrics layer is specified against "a list of removed edge keys"
    and must not depend on how Phase 7 chooses them. Edges already absent
    are ignored."""
    G2 = G.copy()
    G2.remove_edges_from([e for e in removed_edges if G2.has_edge(*e)])
    return G2


def path_avoids_removed_edges(
    G: nx.MultiDiGraph, path: Sequence[int] | None, removed_edges: Iterable[EdgeKey]
) -> bool:
    """Whether `path` is traversable on `G` without using any removed edge.

    A path is a plain node list, so "uses edge (u,v,k)" is only well-defined
    via the project's min-over-parallel-edges convention: the hop u->v is
    valid iff at least one parallel edge between u and v SURVIVES. This
    checks exactly that, which is equivalent to the path existing in the
    disrupted graph."""
    if not path:
        return False
    removed = set(removed_edges)
    for u, v in zip(path, path[1:]):
        edge_dict = G.get_edge_data(u, v)
        if not edge_dict:
            return False
        if all((u, v, k) in removed for k in edge_dict.keys()):
            return False
    return True


def first_valid_path_iteration(result: RouterResult) -> int | None:
    """The 1-based iteration at which the router first reported a valid
    path, or None if it never did.

    `run_to_convergence` records this itself (it is the same driver for
    every algorithm, so the measurement is identical across them) rather
    than each algorithm remembering to log it. After a disruption it is
    meaningful because the `Router` contract requires `best_path()` /
    `best_cost()` to reflect the DISRUPTED graph only from
    `apply_disruption` onward -- a stale pre-disruption best that used a
    severed edge must have been invalidated. `run_and_collect` additionally
    re-checks the final recovered path against the removed edges, so a
    router violating that contract surfaces as a failed row rather than a
    silently wrong first-valid-path number."""
    return result.time_to_first_valid_path_iters


@contextmanager
def measure_peak_memory():
    """Yield a one-element list that receives the tracemalloc peak in KiB.

    Uses a single stack frame (the cheapest tracing mode). If tracing is
    already active (a nested measurement, or a caller profiling the sweep)
    we reset the peak instead of starting/stopping, so we never tear down
    someone else's tracer -- the peak then includes blocks allocated before
    this block, which is the documented caveat of the nested case."""
    out: list[float] = [0.0]
    already = tracemalloc.is_tracing()
    if already:
        tracemalloc.reset_peak()
    else:
        tracemalloc.start(1)
    try:
        yield out
    finally:
        try:
            _, peak = tracemalloc.get_traced_memory()
            out[0] = peak / 1024.0
        finally:
            if not already:
                tracemalloc.stop()


def router_diagnostics(router: Router) -> dict[str, Any]:
    """Best-effort per-algorithm diagnostics for the nullable columns.

    Duck-typed on purpose: `metrics/` must not import `algorithms/aco.py`
    or `physarum.py` (it would pull numpy/scipy into every consumer and
    couple the schema to two concrete implementations). Anything missing
    stays null, which is exactly what the schema says those columns mean.

    * `extraction_method` -- Physarum's `last_extraction_method`: the method
      that fired on the LAST iteration, i.e. how the run ended.
    * `best_path_extraction_method` -- Physarum's
      `best_path_extraction_method`: the method that produced the path the
      row actually REPORTS in `path_cost`/`stretch`. Not derivable from the
      column above, which disagreed with it on 1 of 6 pairs measured in Phase
      8a; and because one Physarum fallback is plain Dijkstra on the full
      graph, this column is what separates a genuine flux-walk stretch of 1.0
      from "Dijkstra's answer, obtained via the fallback". See the schema
      docstring.
    * `ant_completion_rate` -- ACO's completed/total ant counters. These are
      cumulative from `prepare()` onward and are NOT reset by
      `apply_disruption(warm=True)`, so on a disruption run the rate spans
      the initial solve and the recovery together.
    """
    out: dict[str, Any] = {
        "extraction_method": None,
        "best_path_extraction_method": None,
        "ant_completion_rate": None,
    }

    for column, accessor in (
        ("extraction_method", "last_extraction_method"),
        ("best_path_extraction_method", "best_path_extraction_method"),
    ):
        method = getattr(router, accessor, None)
        try:
            if callable(method):
                method = method()
            if isinstance(method, str) and method:
                out[column] = method
        except Exception:  # diagnostics must never break a run
            pass

    try:
        total = getattr(router, "_ants_total", None)
        completed = getattr(router, "_ants_completed", None)
        if isinstance(total, int) and isinstance(completed, int) and total > 0:
            out["ant_completion_rate"] = min(1.0, max(0.0, completed / total))
    except Exception:
        pass

    return out


def make_run_id(
    *,
    algorithm: str,
    od_source: int,
    od_target: int,
    scenario_id: str,
    traffic_level: str,
    congestion_source: str,
    baseline_graph: str,
    warm_start: bool,
    seed: int,
    config_hash: str,
) -> str:
    """A deterministic id for one cell of the experiment matrix.

    Deterministic (not a uuid) so Phase 8 can resume: a completed run is
    identified by re-deriving its id from the matrix cell, and
    `merge_shards` de-duplicates on it when a resumed sweep re-ran a cell
    that a killed worker had already written."""
    key = "|".join(
        str(x)
        for x in (
            algorithm,
            od_source,
            od_target,
            scenario_id,
            traffic_level,
            congestion_source,
            baseline_graph,
            int(bool(warm_start)),
            seed,
            config_hash,
        )
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def utc_now_iso() -> str:
    """Wall-clock timestamp for a result row, ISO-8601 with a Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ==========================================================================
# The entry point Phase 8 calls
# ==========================================================================


def run_and_collect(
    router: Router,
    G: nx.MultiDiGraph,
    source: int,
    target: int,
    snapshot: TrafficSnapshot,
    scenario: Any = None,
    budget: int = 100,
    warm: bool = False,
    seed: int = 0,
    *,
    config_hash: str = "",
    algorithm: str | None = None,
    traffic_level: str | None = None,
    congestion_source: str | None = None,
    baseline_graph: str | None = None,
    baseline_hash: str | None = None,
    run_id: str | None = None,
    reference_cost: float | None = None,
    disrupted_reference_cost: float | None = None,
    G_disrupted: nx.MultiDiGraph | None = None,
) -> ResultRecord:
    """Run one experiment cell end to end and return ONE validated record.

    The full protocol:
      1. Dijkstra reference on (G, snapshot)            -> stretch denominator
      2. `router.prepare(...)` + `run_to_convergence`   -> initial solve
      3. if the scenario removes edges:
           `router.apply_disruption(removed, warm)` + `run_to_convergence`
           -> recovery, measured against Dijkstra recomputed on the
              DISRUPTED graph (never against the pre-disruption optimum)
      4. one `ResultRecord`, validated before return.

    Never raises for a run-level problem: an unreachable OD pair, an
    exhausted budget, or an exception anywhere inside the router all come
    back as `failed=True` with a `failure_reason`, because CLAUDE.md
    requires failures to be recorded rather than dropped. The only
    exception that escapes is `SchemaViolation` from the final `validate`,
    which means the record itself is malformed -- a Phase 6 bug that must
    stop the sweep rather than write a bad row.

    `reference_cost` / `disrupted_reference_cost` / `G_disrupted` are
    optional precomputed values: the three algorithms in a sweep cell share
    the same Dijkstra denominator and the same disrupted graph, so Phase 8
    can compute them once per cell instead of three times.
    """
    spec = as_scenario_spec(scenario)
    removed = list(spec.removed_edges)
    control = spec.is_control

    algorithm = algorithm or getattr(router, "name", "unknown")
    traffic_level = traffic_level if traffic_level is not None else snapshot.level
    congestion_source = (
        congestion_source if congestion_source is not None else snapshot.source
    )
    meta = snapshot.meta or {}
    baseline_graph = (
        baseline_graph
        if baseline_graph is not None
        else str(meta.get("baseline", DEFAULT_BASELINE_GRAPH))
    )
    baseline_hash = (
        baseline_hash if baseline_hash is not None else str(meta.get("baseline_hash", ""))
    )
    if run_id is None:
        run_id = make_run_id(
            algorithm=algorithm,
            od_source=source,
            od_target=target,
            scenario_id=spec.scenario_id,
            traffic_level=traffic_level,
            congestion_source=congestion_source,
            baseline_graph=baseline_graph,
            warm_start=warm,
            seed=seed,
            config_hash=config_hash,
        )

    record = ResultRecord(
        run_id=run_id,
        algorithm=algorithm,
        od_source=int(source),
        od_target=int(target),
        scenario_id=str(spec.scenario_id),
        traffic_level=str(traffic_level),
        congestion_source=str(congestion_source),
        baseline_graph=str(baseline_graph),
        baseline_hash=str(baseline_hash),
        warm_start=bool(warm),
        seed=int(seed),
        config_hash=str(config_hash),
        timestamp_utc=utc_now_iso(),
        iterations=0,
        iteration_budget=int(budget),
        converged=False,
        wallclock_s=0.0,
        peak_memory_kb=0.0,
        failed=True,
        failure_reason="run did not complete",
        n_removed_edges=len(removed),
    )

    try:
        with measure_peak_memory() as peak_kb:
            _run_into(
                record,
                router=router,
                G=G,
                source=source,
                target=target,
                snapshot=snapshot,
                removed=removed,
                control=control,
                budget=int(budget),
                warm=bool(warm),
                seed=int(seed),
                reference_cost=reference_cost,
                disrupted_reference_cost=disrupted_reference_cost,
                G_disrupted=G_disrupted,
            )
        record.peak_memory_kb = float(peak_kb[0])
    except Exception as exc:  # noqa: BLE001 -- a failed run is a ROW, not a crash
        record.failed = True
        record.failure_reason = _format_exception(exc)
        # Whatever partial numbers were already filled in stay: a run that
        # solved and then blew up in recovery keeps its initial-solve
        # measurements, which are real measurements.

    diagnostics = router_diagnostics(router)
    record.extraction_method = diagnostics["extraction_method"]
    record.best_path_extraction_method = diagnostics["best_path_extraction_method"]
    record.ant_completion_rate = diagnostics["ant_completion_rate"]

    if record.failed:
        # A failed run reports no recovery numbers it cannot stand behind.
        # (For the control they are null regardless -- see the schema.)
        if record.post_disruption_stretch is None:
            record.post_disruption_cost = None
    validate(record)
    return record


def _run_into(
    record: ResultRecord,
    *,
    router: Router,
    G: nx.MultiDiGraph,
    source: int,
    target: int,
    snapshot: TrafficSnapshot,
    removed: list[EdgeKey],
    control: bool,
    budget: int,
    warm: bool,
    seed: int,
    reference_cost: float | None,
    disrupted_reference_cost: float | None,
    G_disrupted: nx.MultiDiGraph | None,
) -> None:
    """The measured body of `run_and_collect`, mutating `record` in place so
    that a failure partway through still keeps everything measured so far."""
    # -- 1. stretch denominator -------------------------------------------
    if reference_cost is None:
        _, reference_cost = dijkstra_reference(G, source, target, snapshot)
    if reference_cost is None:
        record.failed = True
        record.failure_reason = (
            f"no path exists from {source} to {target} on the undisrupted graph"
        )
        return

    # -- 2. initial solve --------------------------------------------------
    router.prepare(G, source, target, snapshot, seed)
    res: RouterResult = run_to_convergence(router, budget)

    record.iterations = int(res.iterations)
    record.converged = bool(res.converged)
    record.wallclock_s = float(res.wallclock_s)
    record.initial_time_to_first_valid_path_iters = first_valid_path_iteration(res)

    if not res.path:
        record.failed = True
        record.failure_reason = (
            f"no valid path found within budget ({res.iterations} iterations)"
        )
        return

    record.path_cost = float(snapshot.cost_of_path(G, res.path))
    record.stretch = compute_stretch(G, snapshot, res.path, reference_cost)
    record.path_hops = len(res.path) - 1

    if control or not removed:
        record.failed = False
        record.failure_reason = ""
        return

    # -- 3. recovery -------------------------------------------------------
    Gd = G_disrupted if G_disrupted is not None else disrupted_graph(G, removed)
    if disrupted_reference_cost is None:
        _, disrupted_reference_cost = dijkstra_reference(Gd, source, target, snapshot)
    if disrupted_reference_cost is None:
        record.failed = True
        record.failure_reason = (
            f"scenario {record.scenario_id} disconnected {source} from {target}: "
            "no post-disruption Dijkstra reference exists (Phase 7 must preserve "
            "connectivity)"
        )
        return

    router.apply_disruption(list(removed), warm=warm)
    rec: RouterResult = run_to_convergence(router, budget)

    record.recovery_iterations = int(rec.iterations)
    record.recovery_wallclock_s = float(rec.wallclock_s)
    record.recovery_converged = bool(rec.converged)
    record.time_to_first_valid_path_iters = first_valid_path_iteration(rec)

    if not rec.path:
        record.failed = True
        record.failure_reason = (
            f"no valid path after disruption within budget ({rec.iterations} iterations)"
        )
        return
    if not path_avoids_removed_edges(Gd, rec.path, removed):
        # A Router contract violation: best_path() must reflect the
        # disrupted graph only. Recorded as a failure rather than reported
        # as a (meaningless) post-disruption stretch.
        record.failed = True
        record.failure_reason = (
            "recovered path traverses a removed edge (router returned a stale "
            "pre-disruption path)"
        )
        record.time_to_first_valid_path_iters = None
        return

    record.post_disruption_cost = float(snapshot.cost_of_path(Gd, rec.path))
    record.post_disruption_stretch = compute_stretch(
        Gd, snapshot, rec.path, disrupted_reference_cost
    )
    record.failed = False
    record.failure_reason = ""


def _format_exception(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
    if len(text) > FAILURE_REASON_MAX_CHARS:
        text = text[: FAILURE_REASON_MAX_CHARS - 3] + "..."
    return text or type(exc).__name__


# ==========================================================================
# Parquet write path (Phase 4d discipline -- see the module docstring)
# ==========================================================================

SHARD_GLOB = "shard_*.parquet"

# Same bounded-retry shape as congestion/base.py's cache replace: Windows
# can hold a handle on either file for a short unpredictable window.
REPLACE_RETRIES = 12
RETRY_BASE_DELAY_S = 0.01
RETRY_MAX_DELAY_S = 0.5

# A PRIVATE Random for the backoff jitter: CLAUDE.md forbids experimental
# code depending on unseeded RNG, and drawing from the global `random`
# stream here would perturb a caller that seeded it.
_RETRY_JITTER = random.Random()


def _is_sharing_violation(exc: OSError) -> bool:
    """True for 'someone holds a handle on this file', the retryable case.
    5 = ERROR_ACCESS_DENIED, 32 = ERROR_SHARING_VIOLATION,
    33 = ERROR_LOCK_VIOLATION."""
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32, 33)


def _unique_tmp_path(path: Path) -> Path:
    """A temp path unique to THIS writer. The Phase 4d post-mortem: a temp
    name derived deterministically from the destination is shared by every
    concurrent writer, so two of them interleave inside one file."""
    unique = f"{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    return path.with_name(f"{path.name}.{unique}.tmp")


def _atomic_replace(tmp: Path, dest: Path, retries: int = REPLACE_RETRIES) -> None:
    last: OSError | None = None
    for attempt in range(retries):
        try:
            os.replace(tmp, dest)
            return
        except OSError as exc:
            if not _is_sharing_violation(exc):
                raise
            last = exc
        delay = min(RETRY_BASE_DELAY_S * (2**attempt), RETRY_MAX_DELAY_S)
        time.sleep(delay * (0.5 + _RETRY_JITTER.random()))
    raise OSError(f"Could not atomically replace {dest} after {retries} attempts: {last!r}")


def write_table_atomic(table: pa.Table, dest: str | Path) -> Path:
    """Write `table` to `dest` so no reader can ever observe a partial file.

    Unique temp per writer -> fsync -> `os.replace` with retry. The fsync
    matters for the same reason it does in the congestion cache: without
    it, a crash between the replace and the OS writeback can leave a
    zero-length file at the final path, which is the 'silently reused
    corrupt artifact' failure mode."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp_path(dest)
    try:
        pq.write_table(table, tmp, compression="snappy")
        with tmp.open("rb+") as f:
            os.fsync(f.fileno())
        _atomic_replace(tmp, dest)
        return dest
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def new_shard_path(out_dir: str | Path, shard_id: str | None = None) -> Path:
    """A shard filename unique to this worker.

    The pid + random suffix is what makes the write path safe by
    CONSTRUCTION: separate workers never target the same final file, so the
    atomic-replace machinery is a backstop rather than the primary defence
    (exactly the shape Phase 4d settled on with `prewarm_snapshots`)."""
    if shard_id is None:
        shard_id = f"{os.getpid():06d}_{uuid.uuid4().hex[:8]}"
    return Path(out_dir) / f"shard_{shard_id}.parquet"


def write_shard(
    records: Iterable[Any], out_dir: str | Path, shard_id: str | None = None
) -> Path:
    """Validate `records` and write them as ONE shard Parquet file.

    Every record passes through `schema.validate` first -- nothing reaches
    Parquet unvalidated."""
    table = records_to_table(list(records))
    path = new_shard_path(out_dir, shard_id)
    return write_table_atomic(table, path)


class ShardWriter:
    """Incremental, crash-tolerant writer for ONE worker's shard.

    Phase 8 wants incremental append (a killed worker must not lose an
    hour of solves) but many processes appending to one Parquet file is
    exactly the race Phase 4d spent a day on. So: each worker owns a
    private shard, `append` validates immediately (a malformed row is a bug
    and should surface at the run that produced it, not at merge time), and
    every `flush` atomically REWRITES the whole shard. Rewriting is cheap --
    a shard holds a few hundred rows of scalars -- and it keeps the file on
    disk always complete and always readable.

    Usage:
        with ShardWriter(out_dir) as w:
            w.append(record)
    """

    def __init__(
        self,
        out_dir: str | Path,
        shard_id: str | None = None,
        flush_every: int = 20,
    ):
        self.path = new_shard_path(out_dir, shard_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.flush_every = max(1, int(flush_every))
        self._rows: list[dict[str, Any]] = []
        self._pending = 0

    def append(self, record: Any) -> None:
        self._rows.append(validate(record))
        self._pending += 1
        if self._pending >= self.flush_every:
            self.flush()

    def extend(self, records: Iterable[Any]) -> None:
        for record in records:
            self.append(record)

    def flush(self) -> Path | None:
        if not self._rows:
            return None
        table = records_to_table(self._rows, validate_rows=False)
        write_table_atomic(table, self.path)
        self._pending = 0
        return self.path

    def close(self) -> Path | None:
        return self.flush()

    def __enter__(self) -> "ShardWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __len__(self) -> int:
        return len(self._rows)


READ_RETRIES = 6
READ_BASE_DELAY_S = 0.005
READ_MAX_DELAY_S = 0.1


def _read_bytes_retrying(path: Path, retries: int = READ_RETRIES) -> bytes:
    """Read a whole file, retrying the Windows sharing-violation window.

    The same read-side hazard Phase 4d documented for the congestion cache:
    while another process is inside `os.replace` onto this path, opening it
    raises PermissionError even though a complete file is there. A results
    shard is small (a few hundred rows of scalars), so slurping it and
    parsing from memory is cheaper than holding a handle open across the
    parse -- and a handle held open on the destination is itself what makes
    a concurrent writer's replace fail."""
    last: OSError | None = None
    for attempt in range(max(1, retries)):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise
        except OSError as exc:
            if not _is_sharing_violation(exc):
                raise
            last = exc
        delay = min(READ_BASE_DELAY_S * (2**attempt), READ_MAX_DELAY_S)
        time.sleep(delay * (0.5 + _RETRY_JITTER.random()))
    raise OSError(f"Could not read {path} after {retries} attempts: {last!r}")


def read_shard(path: str | Path) -> pa.Table:
    """Read one results Parquet file under the explicit `RESULT_SCHEMA`.

    Column order and type are normalised here rather than trusted, so two
    shards written by different builds still concatenate."""
    table = pq.read_table(pa.BufferReader(_read_bytes_retrying(Path(path))))
    missing = [name for name in ALL_FIELDS if name not in table.column_names]
    if missing:
        raise ValueError(f"{path}: missing result columns {missing}")
    return table.select(list(ALL_FIELDS)).cast(RESULT_SCHEMA)


def merge_shards(
    shard_dir: str | Path,
    out_path: str | Path | None = None,
    *,
    pattern: str = SHARD_GLOB,
    dedupe: bool = True,
    skip_corrupt: bool = False,
) -> Path:
    """Merge every shard in `shard_dir` into one Parquet file.

    De-duplicates on `run_id` (keeping the first occurrence in sorted shard
    order) because a resumed sweep can legitimately re-run a cell whose
    worker was killed after writing it.

    A shard that cannot be read raises by default: silently dropping a
    shard would silently drop experimental evidence. `skip_corrupt=True`
    is the deliberate, logged escape hatch."""
    shard_dir = Path(shard_dir)
    out_path = Path(out_path) if out_path is not None else shard_dir / "merged.parquet"
    shards = sorted(p for p in shard_dir.glob(pattern) if p.resolve() != out_path.resolve())

    tables: list[pa.Table] = []
    bad: list[str] = []
    for shard in shards:
        try:
            tables.append(read_shard(shard))
        except Exception as exc:  # noqa: BLE001
            if not skip_corrupt:
                raise ValueError(f"Unreadable results shard {shard}: {exc!r}") from exc
            bad.append(f"{shard.name}: {exc!r}")
    if bad:
        print(f"merge_shards: SKIPPED {len(bad)} unreadable shard(s): {bad}")

    if tables:
        table = pa.concat_tables(tables)
    else:
        table = RESULT_SCHEMA.empty_table()

    if dedupe and table.num_rows:
        seen: set[str] = set()
        keep: list[int] = []
        for i, run_id in enumerate(table.column("run_id").to_pylist()):
            if run_id in seen:
                continue
            seen.add(run_id)
            keep.append(i)
        if len(keep) != table.num_rows:
            table = table.take(pa.array(keep, type=pa.int64()))

    return write_table_atomic(table, out_path)


def load_results(path: str | Path) -> pa.Table:
    """Read a merged results file back under the canonical schema."""
    return read_shard(path)
