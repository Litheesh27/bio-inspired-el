"""Flat result record schema, persisted to Parquet.

IMPLEMENTED IN: Phase 6.

ONE run == ONE flat record. Every figure in Phase 9 and every number in the
paper derives from a Parquet file of these rows, so the definitions here are
load-bearing and deliberately explicit.

Three nullability tiers, enforced by `validate()`:

  1. ALWAYS REQUIRED (never null, in any row, ever) -- identity, provenance
     and the counters that are defined even for a failed run:
     `run_id`, `algorithm`, `od_source`, `od_target`, `scenario_id`,
     `traffic_level`, `congestion_source`, `baseline_graph`,
     `baseline_hash`, `warm_start`, `seed`, `config_hash`, `timestamp_utc`,
     `iterations`, `iteration_budget`, `converged`, `wallclock_s`,
     `peak_memory_kb`, `failed`, `failure_reason`, `n_removed_edges`.
     `failure_reason` is the empty string (NOT null) when `failed` is False,
     so the column has no nulls at all and a `!= ""` filter is exact.

  2. NULL ONLY IF `failed` -- the outcome of the initial solve:
     `path_cost`, `stretch`, `path_hops`,
     `initial_time_to_first_valid_path_iters`.
     A failed run is RECORDED, never dropped (CLAUDE.md invariant), and a
     run that found no path has no cost to report -- that is the one
     legitimate reason for these to be null.

  3. NULL ONLY FOR THE NO-DISRUPTION CONTROL (or a failed run) -- the
     recovery block: `recovery_iterations`, `recovery_wallclock_s`,
     `recovery_converged`, `time_to_first_valid_path_iters`,
     `post_disruption_cost`, `post_disruption_stretch`.
     For a control scenario (S0) these MUST be null -- there was no
     disruption, so a zero would be a lie that averages into the results.

  Per-algorithm diagnostics (`extraction_method`,
  `best_path_extraction_method`, `ant_completion_rate`) are null wherever
  they do not apply, by construction. Only Physarum populates the two
  extraction columns; for ACO and the classical routers they are null, and
  null there means "this algorithm has no path-extraction step", not
  "unknown".

  The two extraction columns are NOT redundant (Phase 8b):

    * `extraction_method` -- the method that fired on the router's LAST
      iteration, i.e. how the run ended.
    * `best_path_extraction_method` -- the method that produced the path
      actually REPORTED in `path_cost` / `stretch`, recorded at the moment
      `best_cost` last improved.

  Physarum's `best_cost` is a minimum over every iteration and every
  extraction method, and one of its fallbacks (`full_graph_fallback`) is
  plain Dijkstra on the full graph. So without the second column a reported
  Physarum stretch of exactly 1.0 is ambiguous between "the max-flux walk
  found the optimum" and "Dijkstra's answer, obtained via the fallback" --
  Phase 8a measured the latter on 3 of 6 undisrupted pairs, and measured the
  two columns disagreeing on 1 of 6. Phase 9 must caveat Physarum's stretch
  figures using this column; see `docs/tuning/physarum_extraction_provenance.md`.

A note on the two first-valid-path columns, because the distinction is the
project's central speed-vs-quality question (CLAUDE.md honest-reporting
note, PLAN.md Phase 6):

  * `time_to_first_valid_path_iters` is the RECOVERY measure -- iterations
    after the disruption is applied until the router first produces a path
    that avoids every severed edge. It is null for the control, and it is
    bounded by `recovery_iterations`, not by `iterations`.
  * `initial_time_to_first_valid_path_iters` is the same quantity for the
    initial (pre-disruption) solve, bounded by `iterations`. Without it the
    S0 control rows would carry no first-valid-path measurement at all,
    which would throw away exactly the "finds *a* route fast, a *good* one
    slowly" signal on the one condition where routing quality is measured
    in isolation.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence

import pyarrow as pa

EdgeKey = tuple[int, int, int]

# --------------------------------------------------------------------------
# Scenario vocabulary
# --------------------------------------------------------------------------
#
# Phase 7 owns the scenario definitions (disruption/scenarios.py); Phase 6
# must not import from it (the two phases are built concurrently and the
# metrics layer must not depend on a disruption implementation detail). The
# only thing this module needs to know is WHICH scenario ids mean "no
# disruption happened", because that is the sole case in which the recovery
# columns are legitimately null.
#
# PLAN.md names the control S0. The aliases are accepted case-insensitively
# as a hedge against a naming mismatch: mis-classifying the control would
# silently corrupt the nullability rule, which is a far worse failure than
# accepting a synonym.
CONTROL_SCENARIO_IDS = frozenset({"s0", "none", "no_disruption", "control", "baseline"})

NO_DISRUPTION_SCENARIO = "S0"


def is_control_scenario(scenario_id: str) -> bool:
    """True if `scenario_id` denotes the no-disruption control, for which
    every recovery column must be null."""
    return str(scenario_id).strip().lower() in CONTROL_SCENARIO_IDS


# Allowed enumerations. These mirror the literals other phases already emit
# (TrafficSnapshot.source; network.graph.BASELINE_*), so a typo in a Phase 8
# config is caught at write time rather than found in a figure.
CONGESTION_SOURCES = frozenset({"freeflow", "bpr", "sumo"})
BASELINE_GRAPHS = frozenset({"config_speeds", "sumo_speeds"})

# stretch = cost_alg / cost_dijkstra is a ratio of two sums of the same
# float values; equality of the optimum yields exactly 1.0 in practice, but
# a different-but-equal-cost path can land a few ulps below. This is the
# only tolerance allowed -- anything beyond it is the bug CLAUDE.md says to
# assert on.
STRETCH_TOLERANCE = 1e-9


@dataclass
class ScenarioSpec:
    """The minimal disruption description the metrics layer needs.

    Deliberately NOT imported from `disruption/` -- Phase 6 is specified
    against "a scenario id string plus a list of removed edge keys" and
    nothing more, so the two phases stay decoupled. `as_scenario_spec`
    below adapts whatever object Phase 7 hands the runner into this."""

    scenario_id: str
    removed_edges: list[EdgeKey]

    def __post_init__(self) -> None:
        self.removed_edges = [
            (int(u), int(v), int(k)) for (u, v, k) in self.removed_edges
        ]
        if is_control_scenario(self.scenario_id) and self.removed_edges:
            raise ValueError(
                f"ScenarioSpec {self.scenario_id!r} is a no-disruption control "
                f"but carries {len(self.removed_edges)} removed edges"
            )

    @property
    def is_control(self) -> bool:
        return is_control_scenario(self.scenario_id)


def as_scenario_spec(obj: Any) -> ScenarioSpec:
    """Adapt a Phase 7 scenario object (or None, or a tuple, or a mapping)
    into a `ScenarioSpec`.

    Accepted forms:
      * None                          -> the no-disruption control
      * ScenarioSpec                  -> returned as-is
      * (scenario_id, removed_edges)  -> tuple/list pair
      * mapping with those keys
      * any object exposing an id attribute (`scenario_id` / `id` / `name`)
        and an edge-list attribute (`removed_edges` / `edges` / `removed`)
    """
    if obj is None:
        return ScenarioSpec(NO_DISRUPTION_SCENARIO, [])
    if isinstance(obj, ScenarioSpec):
        return obj
    if isinstance(obj, Mapping):
        sid = _first_present(obj.get, ("scenario_id", "id", "name"))
        edges = _first_present(obj.get, ("removed_edges", "edges", "removed"))
        return ScenarioSpec(str(sid), list(edges or []))
    if isinstance(obj, (tuple, list)) and len(obj) == 2:
        sid, edges = obj
        return ScenarioSpec(str(sid), list(edges or []))
    getter = lambda name: getattr(obj, name, None)  # noqa: E731
    sid = _first_present(getter, ("scenario_id", "id", "name"))
    edges = _first_present(getter, ("removed_edges", "edges", "removed"))
    if sid is None:
        raise TypeError(f"Cannot interpret {obj!r} as a scenario specification")
    return ScenarioSpec(str(sid), list(edges or []))


def _first_present(getter, names: Sequence[str]):
    for name in names:
        val = getter(name)
        if val is not None:
            return val
    return None


@dataclass
class ResultRecord:
    """One run of one algorithm on one (OD pair, scenario, traffic level,
    warm/cold, seed). Flat by design: this maps 1:1 onto a Parquet row.

    See the module docstring for the nullability tiers. `validate()` is the
    enforcement point and MUST be called before any row is appended.
    """

    # -- identity / provenance (tier 1) ------------------------------------
    run_id: str
    algorithm: str
    od_source: int
    od_target: int
    scenario_id: str
    traffic_level: str
    congestion_source: str
    baseline_graph: str
    baseline_hash: str
    warm_start: bool
    seed: int
    config_hash: str
    timestamp_utc: str

    # -- results -----------------------------------------------------------
    iterations: int
    iteration_budget: int
    converged: bool
    wallclock_s: float
    peak_memory_kb: float
    failed: bool
    failure_reason: str
    n_removed_edges: int

    path_cost: float | None = None  # tier 2
    stretch: float | None = None  # tier 2
    path_hops: int | None = None  # tier 2
    initial_time_to_first_valid_path_iters: int | None = None  # tier 2

    # -- recovery (tier 3: null only for the control) ----------------------
    recovery_iterations: int | None = None
    recovery_wallclock_s: float | None = None
    recovery_converged: bool | None = None
    time_to_first_valid_path_iters: int | None = None
    post_disruption_cost: float | None = None
    post_disruption_stretch: float | None = None

    # -- per-algorithm diagnostics (null where inapplicable) ---------------
    extraction_method: str | None = None
    best_path_extraction_method: str | None = None
    ant_completion_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Column groups, used by `validate` and by the Parquet schema below. Keep
# these in sync with the dataclass -- `test_metrics.py` asserts they cover
# every field exactly once.
REQUIRED_FIELDS: tuple[str, ...] = (
    "run_id",
    "algorithm",
    "od_source",
    "od_target",
    "scenario_id",
    "traffic_level",
    "congestion_source",
    "baseline_graph",
    "baseline_hash",
    "warm_start",
    "seed",
    "config_hash",
    "timestamp_utc",
    "iterations",
    "iteration_budget",
    "converged",
    "wallclock_s",
    "peak_memory_kb",
    "failed",
    "failure_reason",
    "n_removed_edges",
)

SOLVE_FIELDS: tuple[str, ...] = (
    "path_cost",
    "stretch",
    "path_hops",
    "initial_time_to_first_valid_path_iters",
)

RECOVERY_FIELDS: tuple[str, ...] = (
    "recovery_iterations",
    "recovery_wallclock_s",
    "recovery_converged",
    "time_to_first_valid_path_iters",
    "post_disruption_cost",
    "post_disruption_stretch",
)

DIAGNOSTIC_FIELDS: tuple[str, ...] = (
    "extraction_method",
    "best_path_extraction_method",
    "ant_completion_rate",
)

ALL_FIELDS: tuple[str, ...] = REQUIRED_FIELDS + SOLVE_FIELDS + RECOVERY_FIELDS + DIAGNOSTIC_FIELDS


# --------------------------------------------------------------------------
# Explicit pyarrow schema
# --------------------------------------------------------------------------
#
# Written out by hand rather than inferred from the first batch: inference
# turns an all-null shard column into `null` type and an int column with one
# null into `double`, and then two shards refuse to merge. An explicit
# schema makes every shard in the sweep byte-compatible.
#
# Nullability is expressed in the pyarrow schema too (nullable=False for
# tier 1), so a violation is caught by Arrow itself even if someone bypasses
# `validate`.
RESULT_SCHEMA = pa.schema(
    [
        pa.field("run_id", pa.string(), nullable=False),
        pa.field("algorithm", pa.string(), nullable=False),
        pa.field("od_source", pa.int64(), nullable=False),
        pa.field("od_target", pa.int64(), nullable=False),
        pa.field("scenario_id", pa.string(), nullable=False),
        pa.field("traffic_level", pa.string(), nullable=False),
        pa.field("congestion_source", pa.string(), nullable=False),
        pa.field("baseline_graph", pa.string(), nullable=False),
        pa.field("baseline_hash", pa.string(), nullable=False),
        pa.field("warm_start", pa.bool_(), nullable=False),
        pa.field("seed", pa.int64(), nullable=False),
        pa.field("config_hash", pa.string(), nullable=False),
        pa.field("timestamp_utc", pa.string(), nullable=False),
        pa.field("iterations", pa.int64(), nullable=False),
        pa.field("iteration_budget", pa.int64(), nullable=False),
        pa.field("converged", pa.bool_(), nullable=False),
        pa.field("wallclock_s", pa.float64(), nullable=False),
        pa.field("peak_memory_kb", pa.float64(), nullable=False),
        pa.field("failed", pa.bool_(), nullable=False),
        pa.field("failure_reason", pa.string(), nullable=False),
        pa.field("n_removed_edges", pa.int64(), nullable=False),
        pa.field("path_cost", pa.float64(), nullable=True),
        pa.field("stretch", pa.float64(), nullable=True),
        pa.field("path_hops", pa.int64(), nullable=True),
        pa.field("initial_time_to_first_valid_path_iters", pa.int64(), nullable=True),
        pa.field("recovery_iterations", pa.int64(), nullable=True),
        pa.field("recovery_wallclock_s", pa.float64(), nullable=True),
        pa.field("recovery_converged", pa.bool_(), nullable=True),
        pa.field("time_to_first_valid_path_iters", pa.int64(), nullable=True),
        pa.field("post_disruption_cost", pa.float64(), nullable=True),
        pa.field("post_disruption_stretch", pa.float64(), nullable=True),
        pa.field("extraction_method", pa.string(), nullable=True),
        pa.field("best_path_extraction_method", pa.string(), nullable=True),
        pa.field("ant_completion_rate", pa.float64(), nullable=True),
    ]
)


class SchemaViolation(ValueError):
    """Raised by `validate` when a record breaks a schema invariant. A
    distinct type so Phase 8's runner can distinguish 'this run failed'
    (recorded as a row) from 'this row is malformed' (a bug, must stop)."""


def _as_mapping(record: Any) -> dict[str, Any]:
    if isinstance(record, ResultRecord):
        return record.to_dict()
    if isinstance(record, Mapping):
        return dict(record)
    if hasattr(record, "__dataclass_fields__"):
        return asdict(record)
    raise SchemaViolation(f"Not a result record: {type(record)!r}")


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return False


def validate(record: Any) -> dict[str, Any]:
    """Enforce every schema invariant and return the record as a plain dict
    ready for Arrow. Raises `SchemaViolation` on any breach.

    Called before EVERY append (see collectors.write_shard). Nothing reaches
    Parquet without passing through here.
    """
    data = _as_mapping(record)

    unknown = set(data) - set(ALL_FIELDS)
    if unknown:
        raise SchemaViolation(f"Unknown result fields: {sorted(unknown)}")
    missing = [name for name in ALL_FIELDS if name not in data]
    if missing:
        raise SchemaViolation(f"Missing result fields: {missing}")

    # -- tier 1: never null ------------------------------------------------
    nulls = [name for name in REQUIRED_FIELDS if _is_null(data[name])]
    if nulls:
        raise SchemaViolation(f"Required fields must not be null: {nulls}")

    failed = bool(data["failed"])
    scenario_id = str(data["scenario_id"])
    control = is_control_scenario(scenario_id)

    # -- enumerations ------------------------------------------------------
    if data["congestion_source"] not in CONGESTION_SOURCES:
        raise SchemaViolation(
            f"congestion_source {data['congestion_source']!r} not in {sorted(CONGESTION_SOURCES)}"
        )
    if data["baseline_graph"] not in BASELINE_GRAPHS:
        raise SchemaViolation(
            f"baseline_graph {data['baseline_graph']!r} not in {sorted(BASELINE_GRAPHS)}. "
            "Phase 2d made the free-flow denominator selectable; every row must record which."
        )

    # -- counters ----------------------------------------------------------
    if int(data["iterations"]) < 0:
        raise SchemaViolation("iterations must be >= 0")
    if int(data["iteration_budget"]) < 1:
        raise SchemaViolation("iteration_budget must be >= 1")
    if int(data["iterations"]) > int(data["iteration_budget"]):
        raise SchemaViolation(
            f"iterations ({data['iterations']}) exceeds iteration_budget "
            f"({data['iteration_budget']})"
        )
    if float(data["wallclock_s"]) < 0:
        raise SchemaViolation("wallclock_s must be >= 0")
    if float(data["peak_memory_kb"]) < 0:
        raise SchemaViolation("peak_memory_kb must be >= 0")
    if int(data["n_removed_edges"]) < 0:
        raise SchemaViolation("n_removed_edges must be >= 0")

    # -- failure bookkeeping ----------------------------------------------
    reason = data["failure_reason"]
    if not isinstance(reason, str):
        raise SchemaViolation("failure_reason must be a string ('' when not failed)")
    if failed and not reason:
        raise SchemaViolation("failed=True requires a non-empty failure_reason")
    if not failed and reason:
        raise SchemaViolation(
            f"failed=False must carry an empty failure_reason, got {reason!r}"
        )

    # -- scenario <-> removed-edge consistency -----------------------------
    n_removed = int(data["n_removed_edges"])
    if control and n_removed != 0:
        raise SchemaViolation(
            f"control scenario {scenario_id!r} must remove 0 edges, got {n_removed}"
        )
    if not control and not failed and n_removed < 1:
        raise SchemaViolation(
            f"disruption scenario {scenario_id!r} recorded 0 removed edges"
        )

    # -- tier 2: solve outcome, null only when failed ----------------------
    for name in SOLVE_FIELDS:
        if _is_null(data[name]) and not failed:
            raise SchemaViolation(
                f"{name} may only be null on a failed run (failed=False here)"
            )

    # -- tier 3: recovery block, null only for the control -----------------
    for name in RECOVERY_FIELDS:
        null = _is_null(data[name])
        if control and not null:
            raise SchemaViolation(
                f"{name} must be null for the no-disruption control "
                f"{scenario_id!r}, got {data[name]!r}"
            )
        if not control and null and not failed:
            raise SchemaViolation(
                f"{name} must not be null for disruption scenario {scenario_id!r} "
                "on a successful run"
            )

    # -- the stretch invariant (CLAUDE.md: < 1.0 is a bug) -----------------
    for name in ("stretch", "post_disruption_stretch"):
        val = data[name]
        if _is_null(val):
            continue
        val = float(val)
        if not math.isfinite(val):
            raise SchemaViolation(f"{name} must be finite, got {val!r}")
        if val < 1.0 - STRETCH_TOLERANCE:
            raise SchemaViolation(
                f"{name} = {val!r} is below 1.0: the algorithm beat Dijkstra on "
                "the same weighted graph, which is a bug (CLAUDE.md invariant), "
                "not a result."
            )

    # -- first-valid-path bounds -------------------------------------------
    # Each first-valid-path counter is bounded by the iteration counter of
    # the phase it belongs to: the initial one by `iterations`, the recovery
    # one by `recovery_iterations`. (The fallback to `iterations` covers a
    # malformed row that carries a recovery first-valid-path without a
    # recovery iteration count.)
    _check_first_valid(
        data, "initial_time_to_first_valid_path_iters", data["iterations"], "iterations"
    )
    bound = data["recovery_iterations"]
    bound_name = "recovery_iterations"
    if _is_null(bound):
        bound, bound_name = data["iterations"], "iterations"
    _check_first_valid(data, "time_to_first_valid_path_iters", bound, bound_name)

    # -- path shape --------------------------------------------------------
    hops = data["path_hops"]
    if not _is_null(hops) and int(hops) < 1:
        raise SchemaViolation(f"path_hops must be >= 1 for a real path, got {hops!r}")
    for name in ("path_cost", "post_disruption_cost"):
        val = data[name]
        if not _is_null(val) and (not math.isfinite(float(val)) or float(val) <= 0):
            raise SchemaViolation(f"{name} must be finite and > 0, got {val!r}")

    rate = data["ant_completion_rate"]
    if not _is_null(rate) and not (0.0 <= float(rate) <= 1.0):
        raise SchemaViolation(f"ant_completion_rate must be in [0, 1], got {rate!r}")

    return data


def _check_first_valid(data: Mapping[str, Any], name: str, bound: Any, bound_name: str) -> None:
    val = data[name]
    if _is_null(val):
        return
    if int(val) < 1:
        raise SchemaViolation(f"{name} is 1-based and must be >= 1, got {val!r}")
    if _is_null(bound):
        return
    if int(val) > int(bound):
        raise SchemaViolation(
            f"{name} ({val}) exceeds {bound_name} ({bound}): a first valid path "
            "cannot appear after the search stopped"
        )


def records_to_table(records: Iterable[Any], *, validate_rows: bool = True) -> pa.Table:
    """Build an Arrow table under `RESULT_SCHEMA` from result records.

    `validate_rows=False` exists only for tests that deliberately construct
    malformed rows; the write path always validates.
    """
    rows = [validate(r) if validate_rows else _as_mapping(r) for r in records]
    columns: dict[str, list[Any]] = {name: [] for name in ALL_FIELDS}
    for row in rows:
        for name in ALL_FIELDS:
            val = row.get(name)
            columns[name].append(None if _is_null(val) else val)
    arrays = [
        pa.array(columns[field.name], type=field.type) for field in RESULT_SCHEMA
    ]
    return pa.Table.from_arrays(arrays, schema=RESULT_SCHEMA)
