"""Phase 6 tests: metric definitions, schema enforcement, and the
concurrency-safe Parquet write path.

Everything here runs on toy graphs and stub routers so the suite stays fast
-- EXCEPT the two concurrency tests, which deliberately spawn real OS
processes. Phase 4d lost a 12-worker run to a cache race that every
single-process test passed, so a happy-path in-process test of the write
path would be worthless here: the failure mode (two writers sharing one
temp file; Windows refusing `os.replace` while any handle is open on the
source OR the destination) only exists across processes.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from dataclasses import fields as dc_fields
from pathlib import Path
from typing import Any

import networkx as nx
import pyarrow as pa
import pytest

from bioroute.algorithms.base import Router
from bioroute.algorithms.classical import DijkstraRouter
from bioroute.congestion.snapshot import TrafficSnapshot
from bioroute.metrics import collectors as C
from bioroute.metrics import schema as S


# ==========================================================================
# Fixtures: a toy graph, a snapshot, and stub routers
# ==========================================================================


def toy_graph() -> nx.MultiDiGraph:
    """Two disjoint routes 0 -> 5 plus a parallel edge, so the tests cover
    the project's min-over-parallel-edges cost convention.

        fast:  0 -1-> 1 -1-> 2 -1-> 5      (cost 3)
        slow:  0 -2-> 3 -2-> 4 -2-> 5      (cost 6)
        plus a second, expensive 0 -> 1 edge with key 1 (cost 10)
    """
    G = nx.MultiDiGraph()
    edges = [
        (0, 1, 0, 1.0),
        (0, 1, 1, 10.0),  # parallel, expensive
        (1, 2, 0, 1.0),
        (2, 5, 0, 1.0),
        (0, 3, 0, 2.0),
        (3, 4, 0, 2.0),
        (4, 5, 0, 2.0),
    ]
    for u, v, k, tt in edges:
        G.add_edge(u, v, key=k, travel_time=tt, length=tt * 10.0)
    return G


def toy_snapshot(G: nx.MultiDiGraph, level: str = "freeflow") -> TrafficSnapshot:
    return TrafficSnapshot(
        level=level,
        time_bin=0,
        source="freeflow",
        travel_time={
            (u, v, k): float(d["travel_time"]) for u, v, k, d in G.edges(keys=True, data=True)
        },
        meta={"baseline": "config_speeds", "baseline_hash": "toyhash0001"},
    )


class StubRouter(Router):
    """A Router whose output is scripted, so a test can pin exactly which
    iteration a valid path appears on, when convergence fires, and where an
    exception is thrown.

    `script` is one entry per iteration: a node list (a path found at that
    iteration) or None (nothing found yet). The best path is monotone in
    cost, mirroring the real routers' `step()` contract.
    """

    name = "stub"

    def __init__(
        self,
        script: list[list[int] | None],
        recovery_script: list[list[int] | None] | None = None,
        raise_at: int | None = None,
        raise_in: str = "step",
    ):
        self._script = list(script)
        self._recovery_script = list(recovery_script or [])
        self._raise_at = raise_at
        self._raise_in = raise_in
        self._i = 0
        self._best: list[int] | None = None
        self._cost: float | None = None
        self._phase = "initial"

    # -- Router contract ---------------------------------------------------
    def prepare(self, G, source, target, snapshot, seed) -> None:
        if self._raise_in == "prepare":
            raise RuntimeError("stub exploded in prepare")
        self._G, self._snapshot = G, snapshot
        self._i = 0
        self._best = None
        self._cost = None

    def step(self) -> float | None:
        script = self._script if self._phase == "initial" else self._recovery_script
        self._i += 1
        if self._raise_in == "step" and self._raise_at == self._i:
            raise RuntimeError(f"stub exploded at iteration {self._i}")
        path = script[self._i - 1] if self._i - 1 < len(script) else None
        if path is not None:
            cost = self._snapshot.cost_of_path(self._G, path)
            if self._cost is None or cost < self._cost:
                self._best, self._cost = list(path), cost
        return self._cost

    def best_path(self):
        return self._best

    def best_cost(self):
        return self._cost

    def converged(self) -> bool:
        script = self._script if self._phase == "initial" else self._recovery_script
        return self._i >= len(script)

    def apply_disruption(self, removed_edges, warm: bool) -> None:
        G2 = self._G.copy()
        G2.remove_edges_from(list(removed_edges))
        self._G = G2
        self._phase = "recovery"
        self._i = 0
        self._best = None
        self._cost = None

    def state(self) -> dict[str, Any]:
        return {}

    def load_state(self, state) -> None:
        return None


def minimal_record(**overrides) -> S.ResultRecord:
    """A valid control-scenario record; tests override single fields."""
    base = dict(
        run_id="r0",
        algorithm="dijkstra",
        od_source=0,
        od_target=5,
        scenario_id="S0",
        traffic_level="freeflow",
        congestion_source="freeflow",
        baseline_graph="config_speeds",
        baseline_hash="h",
        warm_start=False,
        seed=1,
        config_hash="cfg",
        timestamp_utc="2026-10-01T00:00:00.000000Z",
        iterations=1,
        iteration_budget=10,
        converged=True,
        wallclock_s=0.001,
        peak_memory_kb=12.5,
        failed=False,
        failure_reason="",
        n_removed_edges=0,
        path_cost=3.0,
        stretch=1.0,
        path_hops=3,
        initial_time_to_first_valid_path_iters=1,
    )
    base.update(overrides)
    return S.ResultRecord(**base)


def disruption_record(**overrides) -> S.ResultRecord:
    base = dict(
        scenario_id="S1",
        n_removed_edges=1,
        recovery_iterations=4,
        recovery_wallclock_s=0.01,
        recovery_converged=True,
        time_to_first_valid_path_iters=2,
        post_disruption_cost=6.0,
        post_disruption_stretch=1.0,
    )
    base.update(overrides)
    return minimal_record(**base)


# ==========================================================================
# Schema: field coverage and required fields
# ==========================================================================


def test_column_groups_cover_every_field_exactly_once():
    declared = [f.name for f in dc_fields(S.ResultRecord)]
    assert sorted(S.ALL_FIELDS) == sorted(declared)
    assert len(S.ALL_FIELDS) == len(set(S.ALL_FIELDS))
    # The pyarrow schema must agree in NAME and ORDER, otherwise shards from
    # different builds stop concatenating.
    assert [f.name for f in S.RESULT_SCHEMA] == list(S.ALL_FIELDS)


def test_required_columns_are_non_nullable_in_arrow_schema():
    for name in S.REQUIRED_FIELDS:
        assert not S.RESULT_SCHEMA.field(name).nullable, name
    for name in S.SOLVE_FIELDS + S.RECOVERY_FIELDS + S.DIAGNOSTIC_FIELDS:
        assert S.RESULT_SCHEMA.field(name).nullable, name


def test_record_missing_a_required_field_fails_validate():
    data = minimal_record().to_dict()
    del data["seed"]
    with pytest.raises(S.SchemaViolation, match="Missing result fields.*seed"):
        S.validate(data)


def test_required_field_set_to_null_fails_validate():
    with pytest.raises(S.SchemaViolation, match="must not be null"):
        S.validate(minimal_record(config_hash=None))


def test_unknown_field_is_rejected():
    data = minimal_record().to_dict()
    data["nonsense"] = 1
    with pytest.raises(S.SchemaViolation, match="Unknown result fields"):
        S.validate(data)


def test_enumerations_are_enforced():
    with pytest.raises(S.SchemaViolation, match="congestion_source"):
        S.validate(minimal_record(congestion_source="guesswork"))
    # Phase 2d made the free-flow denominator selectable -- a row that does
    # not record which baseline produced it is not interpretable.
    with pytest.raises(S.SchemaViolation, match="baseline_graph"):
        S.validate(minimal_record(baseline_graph="whatever"))


def test_failure_reason_and_failed_flag_must_agree():
    with pytest.raises(S.SchemaViolation, match="non-empty failure_reason"):
        S.validate(minimal_record(failed=True, failure_reason=""))
    with pytest.raises(S.SchemaViolation, match="empty failure_reason"):
        S.validate(minimal_record(failed=False, failure_reason="boom"))


def test_solve_fields_may_be_null_only_when_failed():
    with pytest.raises(S.SchemaViolation, match="only be null on a failed run"):
        S.validate(minimal_record(path_cost=None))
    # A failed run with no path is legal -- and MUST be recorded, never
    # dropped (CLAUDE.md invariant).
    S.validate(
        minimal_record(
            failed=True,
            failure_reason="no valid path found within budget (10 iterations)",
            path_cost=None,
            stretch=None,
            path_hops=None,
            initial_time_to_first_valid_path_iters=None,
        )
    )


# ==========================================================================
# The stretch invariant
# ==========================================================================


def test_stretch_below_one_raises_rather_than_being_recorded():
    G = toy_graph()
    snap = toy_snapshot(G)
    # Pretend Dijkstra reported a worse cost than the algorithm's path: that
    # can only mean a bug, so it must raise, not produce stretch < 1.
    with pytest.raises(C.StretchError, match="below 1.0|< 1.0"):
        C.compute_stretch(G, snap, [0, 1, 2, 5], reference_cost=4.0)
    # And the schema refuses it too, in case a value is assembled by hand.
    with pytest.raises(S.SchemaViolation, match="below 1.0"):
        S.validate(minimal_record(stretch=0.98))
    with pytest.raises(S.SchemaViolation, match="below 1.0"):
        S.validate(disruption_record(post_disruption_stretch=0.5))


def test_stretch_uses_the_min_parallel_edge_convention():
    G = toy_graph()
    snap = toy_snapshot(G)
    path, cost = C.dijkstra_reference(G, 0, 5, snap)
    assert path == [0, 1, 2, 5]
    # 1 + 1 + 1, taking the cheap parallel 0->1 edge, not the cost-10 one.
    assert cost == pytest.approx(3.0)
    assert C.compute_stretch(G, snap, [0, 1, 2, 5], cost) == pytest.approx(1.0)
    assert C.compute_stretch(G, snap, [0, 3, 4, 5], cost) == pytest.approx(2.0)


def test_dijkstra_reference_returns_none_for_unreachable_pair():
    G = toy_graph()
    G.add_node(99)
    assert C.dijkstra_reference(G, 0, 99, toy_snapshot(G)) == (None, None)


# ==========================================================================
# Recovery nullability and first-valid-path bounds
# ==========================================================================


def test_recovery_fields_may_be_null_only_for_the_no_disruption_control():
    S.validate(minimal_record())  # S0 with all recovery fields null: fine
    for name in S.RECOVERY_FIELDS:
        with pytest.raises(S.SchemaViolation, match="must be null for the no-disruption"):
            S.validate(minimal_record(**{name: 1}))
    S.validate(disruption_record())  # S1 with all recovery fields set: fine
    for name in S.RECOVERY_FIELDS:
        with pytest.raises(S.SchemaViolation, match="must not be null for disruption"):
            S.validate(disruption_record(**{name: None}))


def test_control_scenario_aliases_and_removed_edge_consistency():
    assert S.is_control_scenario("S0")
    assert S.is_control_scenario("s0")
    assert S.is_control_scenario("none")
    assert not S.is_control_scenario("S1")
    with pytest.raises(S.SchemaViolation, match="must remove 0 edges"):
        S.validate(minimal_record(n_removed_edges=2))
    with pytest.raises(S.SchemaViolation, match="recorded 0 removed edges"):
        S.validate(disruption_record(n_removed_edges=0))


def test_first_valid_path_iters_cannot_exceed_its_iteration_counter():
    # Both non-null: the first valid path cannot appear after the search
    # stopped. Each counter is bounded by the phase it belongs to.
    S.validate(minimal_record(iterations=5, initial_time_to_first_valid_path_iters=5))
    with pytest.raises(S.SchemaViolation, match="exceeds iterations"):
        S.validate(minimal_record(iterations=3, initial_time_to_first_valid_path_iters=4))
    S.validate(disruption_record(recovery_iterations=4, time_to_first_valid_path_iters=4))
    with pytest.raises(S.SchemaViolation, match="exceeds recovery_iterations"):
        S.validate(disruption_record(recovery_iterations=2, time_to_first_valid_path_iters=3))
    with pytest.raises(S.SchemaViolation, match="1-based"):
        S.validate(minimal_record(initial_time_to_first_valid_path_iters=0))


def test_iterations_cannot_exceed_budget():
    with pytest.raises(S.SchemaViolation, match="exceeds iteration_budget"):
        S.validate(minimal_record(iterations=11, iteration_budget=10))


def test_ant_completion_rate_bounds():
    S.validate(minimal_record(ant_completion_rate=0.0))
    S.validate(minimal_record(ant_completion_rate=1.0))
    with pytest.raises(S.SchemaViolation, match="ant_completion_rate"):
        S.validate(minimal_record(ant_completion_rate=1.5))


# ==========================================================================
# path_avoids_removed_edges
# ==========================================================================


def test_path_validity_follows_the_parallel_edge_convention():
    G = toy_graph()
    # Removing ONE of two parallel 0->1 edges leaves the hop traversable.
    assert C.path_avoids_removed_edges(G, [0, 1, 2, 5], [(0, 1, 1)])
    # Removing both does not.
    assert not C.path_avoids_removed_edges(G, [0, 1, 2, 5], [(0, 1, 0), (0, 1, 1)])
    assert not C.path_avoids_removed_edges(G, [0, 1, 2, 5], [(1, 2, 0)])
    assert not C.path_avoids_removed_edges(G, None, [])
    # Equivalent to "the path exists in the disrupted graph".
    Gd = C.disrupted_graph(G, [(1, 2, 0)])
    assert not Gd.has_edge(1, 2)
    assert Gd.number_of_edges() == G.number_of_edges() - 1
    assert C.path_avoids_removed_edges(Gd, [0, 3, 4, 5], [(1, 2, 0)])


# ==========================================================================
# run_and_collect
# ==========================================================================


def test_run_and_collect_control_scenario_end_to_end():
    G = toy_graph()
    snap = toy_snapshot(G)
    rec = C.run_and_collect(
        DijkstraRouter(), G, 0, 5, snap, None, budget=5, warm=False, seed=7,
        config_hash="abc123",
    )
    S.validate(rec)
    assert rec.failed is False and rec.failure_reason == ""
    assert rec.scenario_id == S.NO_DISRUPTION_SCENARIO
    assert rec.n_removed_edges == 0
    assert rec.stretch == pytest.approx(1.0)
    assert rec.path_cost == pytest.approx(3.0)
    assert rec.path_hops == 3
    assert rec.iterations == 1 and rec.converged is True
    assert rec.initial_time_to_first_valid_path_iters == 1
    # Provenance is taken from the snapshot, not invented.
    assert rec.traffic_level == "freeflow"
    assert rec.congestion_source == "freeflow"
    assert rec.baseline_graph == "config_speeds"
    assert rec.baseline_hash == "toyhash0001"
    assert rec.seed == 7 and rec.config_hash == "abc123"
    assert rec.peak_memory_kb > 0.0
    # Every recovery field is null for the control.
    for name in S.RECOVERY_FIELDS:
        assert getattr(rec, name) is None, name


def test_run_and_collect_disruption_measures_recovery_against_disrupted_dijkstra():
    G = toy_graph()
    snap = toy_snapshot(G)
    removed = [(1, 2, 0)]  # severs the fast route
    rec = C.run_and_collect(
        DijkstraRouter(), G, 0, 5, snap, ("S1", removed), budget=5, warm=True, seed=3,
    )
    S.validate(rec)
    assert rec.failed is False
    assert rec.n_removed_edges == 1 and rec.warm_start is True
    # Pre-disruption numbers are untouched by the disruption.
    assert rec.path_cost == pytest.approx(3.0) and rec.stretch == pytest.approx(1.0)
    # Post-disruption: the surviving route costs 6, and it IS the disrupted
    # optimum -- so stretch is 1.0 vs Dijkstra ON THE DISRUPTED GRAPH, even
    # though the cost doubled vs the pre-disruption optimum. Measuring
    # against the pre-disruption optimum would have reported 2.0 and
    # confused "adapted badly" with "the network got worse".
    assert rec.post_disruption_cost == pytest.approx(6.0)
    assert rec.post_disruption_stretch == pytest.approx(1.0)
    assert rec.recovery_iterations == 1
    assert rec.recovery_converged is True
    assert rec.recovery_wallclock_s >= 0.0
    assert rec.time_to_first_valid_path_iters == 1


def test_time_to_first_valid_path_is_measured_separately_from_convergence():
    """The project's central speed-vs-quality distinction: a valid path can
    appear long before a good one."""
    G = toy_graph()
    snap = toy_snapshot(G)
    router = StubRouter(
        script=[None, [0, 3, 4, 5], None, [0, 1, 2, 5]],  # valid at 2, optimal at 4
        recovery_script=[None, None, [0, 3, 4, 5]],  # valid at 3 after the break
    )
    rec = C.run_and_collect(
        router, G, 0, 5, snap, ("S2", [(1, 2, 0)]), budget=10, warm=True, seed=1
    )
    S.validate(rec)
    assert rec.failed is False
    assert rec.initial_time_to_first_valid_path_iters == 2
    assert rec.iterations == 4
    assert rec.initial_time_to_first_valid_path_iters < rec.iterations
    assert rec.time_to_first_valid_path_iters == 3
    assert rec.recovery_iterations == 3
    assert rec.time_to_first_valid_path_iters <= rec.recovery_iterations


def test_suboptimal_router_records_stretch_above_one():
    G = toy_graph()
    snap = toy_snapshot(G)
    rec = C.run_and_collect(
        StubRouter(script=[[0, 3, 4, 5]]), G, 0, 5, snap, None, budget=3, seed=0
    )
    assert rec.failed is False
    assert rec.stretch == pytest.approx(2.0)


def test_router_that_raises_mid_solve_yields_a_failed_record_not_an_exception():
    G = toy_graph()
    snap = toy_snapshot(G)
    rec = C.run_and_collect(
        StubRouter(script=[None, None, None], raise_at=2), G, 0, 5, snap, None,
        budget=5, seed=1,
    )
    S.validate(rec)
    assert rec.failed is True
    assert "RuntimeError" in rec.failure_reason
    assert "iteration 2" in rec.failure_reason
    assert rec.path_cost is None and rec.stretch is None


def test_router_that_raises_in_prepare_yields_a_failed_record():
    G = toy_graph()
    rec = C.run_and_collect(
        StubRouter(script=[], raise_in="prepare"), G, 0, 5, toy_snapshot(G), None, seed=1
    )
    assert rec.failed is True and "prepare" in rec.failure_reason


def test_no_path_within_budget_is_recorded_as_failed_never_dropped():
    G = toy_graph()
    rec = C.run_and_collect(
        StubRouter(script=[None, None]), G, 0, 5, toy_snapshot(G), None, budget=2, seed=1
    )
    S.validate(rec)
    assert rec.failed is True
    assert "no valid path found within budget" in rec.failure_reason
    assert rec.iterations == 2
    assert rec.initial_time_to_first_valid_path_iters is None


def test_unreachable_od_pair_is_recorded_as_failed():
    G = toy_graph()
    G.add_node(99)
    rec = C.run_and_collect(DijkstraRouter(), G, 0, 99, toy_snapshot(G), None, seed=1)
    assert rec.failed is True
    assert "no path exists" in rec.failure_reason


def test_scenario_that_disconnects_the_pair_is_recorded_as_failed():
    G = toy_graph()
    snap = toy_snapshot(G)
    removed = [(2, 5, 0), (4, 5, 0)]  # isolates the target
    rec = C.run_and_collect(
        DijkstraRouter(), G, 0, 5, snap, ("S4", removed), budget=3, seed=1
    )
    S.validate(rec)
    assert rec.failed is True
    assert "no post-disruption Dijkstra reference" in rec.failure_reason
    # The initial solve really happened, so its numbers are kept.
    assert rec.stretch == pytest.approx(1.0)
    assert rec.recovery_iterations is None


def test_stale_pre_disruption_path_is_caught_rather_than_scored():
    """A Router that violates its contract by returning a path over a
    severed edge must surface as a failed row, not as a bogus stretch."""

    class StaleRouter(StubRouter):
        def apply_disruption(self, removed_edges, warm):
            self._phase = "recovery"
            self._i = 0  # keeps the pre-disruption best: the contract breach

    G = toy_graph()
    router = StaleRouter(script=[[0, 1, 2, 5]], recovery_script=[None])
    rec = C.run_and_collect(
        router, G, 0, 5, toy_snapshot(G), ("S1", [(1, 2, 0)]), budget=2, seed=1
    )
    S.validate(rec)
    assert rec.failed is True
    assert "removed edge" in rec.failure_reason
    assert rec.post_disruption_stretch is None


def test_run_id_is_deterministic_per_matrix_cell():
    kwargs = dict(
        algorithm="aco",
        od_source=1,
        od_target=2,
        scenario_id="S3",
        traffic_level="heavy",
        congestion_source="bpr",
        baseline_graph="sumo_speeds",
        warm_start=True,
        seed=4,
        config_hash="deadbeef",
    )
    assert C.make_run_id(**kwargs) == C.make_run_id(**kwargs)
    assert C.make_run_id(**{**kwargs, "seed": 5}) != C.make_run_id(**kwargs)
    assert C.make_run_id(**{**kwargs, "warm_start": False}) != C.make_run_id(**kwargs)


def test_as_scenario_spec_accepts_the_shapes_phase_7_might_hand_over():
    assert C.as_scenario_spec(None).is_control
    assert S.as_scenario_spec(("S1", [(1, 2, 0)])).removed_edges == [(1, 2, 0)]
    assert S.as_scenario_spec({"scenario_id": "S5", "removed_edges": []}).scenario_id == "S5"

    class Phase7Like:
        scenario_id = "S6"
        removed_edges = [(3, 4, 0)]

    spec = S.as_scenario_spec(Phase7Like())
    assert spec.scenario_id == "S6" and spec.removed_edges == [(3, 4, 0)]
    with pytest.raises(ValueError, match="control"):
        S.ScenarioSpec("S0", [(1, 2, 0)])


# ==========================================================================
# Parquet write path
# ==========================================================================


def test_write_shard_round_trips_values_and_nulls(tmp_path: Path):
    records = [minimal_record(run_id="a"), disruption_record(run_id="b")]
    shard = C.write_shard(records, tmp_path)
    assert shard.exists() and shard.name.startswith("shard_")
    table = C.read_shard(shard)
    assert table.schema == S.RESULT_SCHEMA
    assert table.num_rows == 2
    rows = table.to_pylist()
    assert rows[0]["recovery_iterations"] is None  # control: genuinely null
    assert rows[1]["recovery_iterations"] == 4
    assert rows[0]["stretch"] == pytest.approx(1.0)
    assert rows[0]["extraction_method"] is None
    # No temp files survive a successful write.
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_shard_validates_before_writing(tmp_path: Path):
    with pytest.raises(S.SchemaViolation):
        C.write_shard([minimal_record(stretch=0.5)], tmp_path)
    assert list(tmp_path.glob("*.parquet")) == []


def test_shard_writer_flushes_incrementally(tmp_path: Path):
    with C.ShardWriter(tmp_path, flush_every=2) as w:
        w.append(minimal_record(run_id="a"))
        assert not w.path.exists()  # buffered
        w.append(minimal_record(run_id="b"))
        assert C.read_shard(w.path).num_rows == 2  # flushed, complete on disk
        w.append(minimal_record(run_id="c"))
    assert C.read_shard(w.path).num_rows == 3


def test_merge_dedupes_on_run_id(tmp_path: Path):
    C.write_shard([minimal_record(run_id="dup"), minimal_record(run_id="x")], tmp_path, "0")
    C.write_shard([minimal_record(run_id="dup"), minimal_record(run_id="y")], tmp_path, "1")
    out = C.merge_shards(tmp_path, tmp_path / "final.parquet")
    ids = C.load_results(out).column("run_id").to_pylist()
    assert sorted(ids) == ["dup", "x", "y"]


def test_merge_empty_dir_yields_an_empty_table_with_the_canonical_schema(tmp_path: Path):
    out = C.merge_shards(tmp_path, tmp_path / "final.parquet")
    table = C.load_results(out)
    assert table.num_rows == 0 and table.schema == S.RESULT_SCHEMA


def test_merge_refuses_to_silently_drop_a_corrupt_shard(tmp_path: Path):
    C.write_shard([minimal_record(run_id="good")], tmp_path, "0")
    (tmp_path / "shard_broken.parquet").write_bytes(b"not parquet at all")
    with pytest.raises(ValueError, match="Unreadable results shard"):
        C.merge_shards(tmp_path, tmp_path / "final.parquet")
    out = C.merge_shards(tmp_path, tmp_path / "final.parquet", skip_corrupt=True)
    assert C.load_results(out).column("run_id").to_pylist() == ["good"]


# ==========================================================================
# Concurrency: real processes, because that is the only way this race shows
# ==========================================================================

WORKER_SRC = textwrap.dedent(
    '''
    """Shard-writing worker for the Phase 6 concurrency tests. Launched as a
    separate OS process, because the race Phase 4d found (shared temp file;
    Windows refusing os.replace while a handle is open) cannot be reproduced
    in-process."""
    import sys
    from pathlib import Path

    from bioroute.metrics.collectors import ShardWriter, write_table_atomic
    from bioroute.metrics.schema import ResultRecord, records_to_table


    def make_record(worker, i):
        return ResultRecord(
            run_id=f"w{worker}_{i}",
            algorithm="stub",
            od_source=worker,
            od_target=i,
            scenario_id="S1",
            traffic_level="heavy",
            congestion_source="bpr",
            baseline_graph="sumo_speeds",
            baseline_hash="h",
            warm_start=bool(i % 2),
            seed=i,
            config_hash="cfg",
            timestamp_utc="2026-10-01T00:00:00.000000Z",
            iterations=i + 1,
            iteration_budget=100,
            converged=True,
            wallclock_s=0.5,
            peak_memory_kb=10.0,
            failed=False,
            failure_reason="",
            n_removed_edges=1,
            path_cost=100.0 + i,
            stretch=1.0 + i / 100.0,
            path_hops=i + 1,
            initial_time_to_first_valid_path_iters=1,
            recovery_iterations=3,
            recovery_wallclock_s=0.1,
            recovery_converged=True,
            time_to_first_valid_path_iters=2,
            post_disruption_cost=200.0 + i,
            post_disruption_stretch=1.0,
        )


    def main():
        mode, out_dir, worker, n = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
        if mode == "shards":
            # One shard per worker (the design), flushed incrementally.
            with ShardWriter(out_dir, flush_every=3) as w:
                for i in range(n):
                    w.append(make_record(worker, i))
        elif mode == "contend":
            # Every worker hammers the SAME destination path. This is the
            # exact Phase 4d scenario and must never corrupt the file.
            dest = out_dir / "contended.parquet"
            for i in range(n):
                write_table_atomic(records_to_table([make_record(worker, i)]), dest)
        else:
            raise SystemExit(f"unknown mode {mode}")


    if __name__ == "__main__":
        main()
    '''
)


def _spawn_workers(worker_py: Path, mode: str, out_dir: Path, n_workers: int, n_rows: int):
    procs = [
        subprocess.Popen(
            [sys.executable, str(worker_py), mode, str(out_dir), str(w), str(n_rows)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for w in range(n_workers)
    ]
    results = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=180)
        results.append((p.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")))
    for rc, _out, err in results:
        assert rc == 0, f"worker failed (rc={rc}):\n{err}"
    return results


@pytest.fixture
def worker_py(tmp_path: Path) -> Path:
    path = tmp_path / "shard_worker.py"
    path.write_text(WORKER_SRC, encoding="utf-8")
    return path


def test_concurrent_processes_write_shards_that_merge_losslessly(
    tmp_path: Path, worker_py: Path
):
    """6 real processes, one shard each, written incrementally, then merged.

    Asserts row count AND values, because the failure this guards against
    (Phase 4d) was a spliced/truncated file that still *looked* like a file.
    """
    n_workers, n_rows = 6, 20
    out_dir = tmp_path / "shards"
    out_dir.mkdir()
    _spawn_workers(worker_py, "shards", out_dir, n_workers, n_rows)

    shards = sorted(out_dir.glob(C.SHARD_GLOB))
    assert len(shards) == n_workers, "each worker must own exactly one shard"
    assert list(out_dir.glob("*.tmp")) == [], "temp files leaked"

    out = C.merge_shards(out_dir, tmp_path / "final.parquet")
    table = C.load_results(out)
    assert table.schema == S.RESULT_SCHEMA
    assert table.num_rows == n_workers * n_rows

    rows = {r["run_id"]: r for r in table.to_pylist()}
    assert set(rows) == {f"w{w}_{i}" for w in range(n_workers) for i in range(n_rows)}
    # Values survived the round trip exactly, for every row.
    for w in range(n_workers):
        for i in range(n_rows):
            r = rows[f"w{w}_{i}"]
            assert r["od_source"] == w and r["od_target"] == i
            assert r["path_cost"] == pytest.approx(100.0 + i)
            assert r["stretch"] == pytest.approx(1.0 + i / 100.0)
            assert r["warm_start"] is bool(i % 2)
            assert r["recovery_iterations"] == 3
            assert r["extraction_method"] is None


def test_concurrent_processes_replacing_one_file_never_corrupt_it(
    tmp_path: Path, worker_py: Path
):
    """The Phase 4d race itself: many writers onto ONE destination path,
    with a reader hammering it at the same time.

    Before the fix, writers shared a temp path derived from the destination
    (so two could interleave inside one file) and `os.replace` raised
    PermissionError on Windows whenever any handle was open on the source
    or the destination -- including a reader's. Every read here must either
    find no file yet or find a COMPLETE, parseable one; a partial read is
    the failure.
    """
    n_workers, n_writes = 6, 40
    out_dir = tmp_path / "contended"
    out_dir.mkdir()
    dest = out_dir / "contended.parquet"

    procs = [
        subprocess.Popen(
            [sys.executable, str(worker_py), "contend", str(out_dir), str(w), str(n_writes)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for w in range(n_workers)
    ]
    reads, misses = 0, 0
    try:
        while any(p.poll() is None for p in procs):
            try:
                table = C.read_shard(dest)
            except FileNotFoundError:
                misses += 1
                continue
            assert table.schema == S.RESULT_SCHEMA
            assert table.num_rows == 1
            row = table.to_pylist()[0]
            assert row["run_id"].startswith("w")
            assert row["failed"] is False
            reads += 1
    finally:
        for p in procs:
            stdout, stderr = p.communicate(timeout=180)
            assert p.returncode == 0, (
                f"writer failed (rc={p.returncode}):\n{stderr.decode(errors='replace')}"
            )

    assert reads > 0, f"the reader never observed the file (misses={misses}); test proved nothing"
    assert C.read_shard(dest).num_rows == 1
    # Not one temp file left behind by 240 concurrent replaces.
    assert list(out_dir.glob("*.tmp")) == []
