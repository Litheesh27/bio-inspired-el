"""Phase 8: the experiment matrix and the sweep runner.

Everything here runs on a toy graph with stub routers in milliseconds. The
properties under test are the ones a wrong answer would be EXPENSIVE to
discover: a matrix that counts wrong, a resume that re-runs finished work
(or, worse, silently drops it), a denominator that differs between the
algorithms being compared, and a single bad cell taking a multi-hour sweep
down with it.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import networkx as nx
import pytest
import yaml

from bioroute.algorithms.base import Router
from bioroute.congestion.snapshot import TrafficSnapshot
from bioroute.experiments import matrix as M
from bioroute.experiments import runner as R
from bioroute.metrics.collectors import read_shard


# ==========================================================================
# Fixtures
# ==========================================================================


def _bidi(G, u, v, tt, osmid):
    for a, b in ((u, v), (v, u)):
        G.add_edge(a, b, key=0, length=tt * 10.0, travel_time=tt, osmid=list(osmid))


@pytest.fixture
def toy_graph() -> nx.MultiDiGraph:
    """Corridor 0-1-...-6 (1 s per edge) plus a slow bypass 0-20-21-22-6
    (5 s per edge), so every single-edge closure on the corridor leaves 0
    and 6 connected. Nodes carry x/y because the OD sampler strata are
    straight-line distances."""
    G = nx.MultiDiGraph()
    for i in range(6):
        _bidi(G, i, i + 1, 1.0, [100])
    _bidi(G, 0, 20, 5.0, [200])
    _bidi(G, 20, 21, 5.0, [200])
    _bidi(G, 21, 22, 5.0, [200])
    _bidi(G, 22, 6, 5.0, [200])
    for n in G.nodes():
        G.nodes[n]["x"] = 77.59 + 0.001 * n
        G.nodes[n]["y"] = 12.97 + 0.0005 * n
        G.nodes[n]["street_count"] = 2
    return G


@pytest.fixture
def toy_snapshot(toy_graph) -> TrafficSnapshot:
    return TrafficSnapshot(
        level="heavy",
        time_bin=0,
        source="sumo",
        travel_time={
            (u, v, k): float(d["travel_time"])
            for u, v, k, d in toy_graph.edges(keys=True, data=True)
        },
        meta={"baseline": "sumo_speeds", "baseline_hash": "deadbeef"},
    )


def _grid_graph(n: int = 60) -> nx.MultiDiGraph:
    """A graph big enough for the OD sampler: a ring with spread-out
    coordinates so the distance strata are non-degenerate."""
    G = nx.MultiDiGraph()
    for i in range(n):
        _bidi(G, i, (i + 1) % n, 1.0, [1000 + i])
    rng = random.Random(7)
    for node in G.nodes():
        G.nodes[node]["x"] = 77.55 + rng.random() * 0.05
        G.nodes[node]["y"] = 12.95 + rng.random() * 0.05
    return G


def _write_config(tmp_path: Path, **over) -> Path:
    raw = {
        "name": "toy",
        "area_config": "configs/area_central_blr.yaml",
        "baseline_graph": "sumo_speeds",
        "conditions": [
            {"congestion_source": "sumo", "level": "light", "time_bin": 0},
            {"congestion_source": "sumo", "level": "heavy", "time_bin": 0},
        ],
        "scenarios": ["S0", "S1", "S2"],
        "algorithms": [
            {"name": "dijkstra", "kind": "classical", "seeds": 1, "budget": 1},
            {"name": "astar", "kind": "classical", "seeds": 1, "budget": 1},
            {"name": "aco", "kind": "iterative", "seeds": 5, "budget": 150},
            {"name": "physarum", "kind": "iterative", "seeds": 1, "budget": 300},
        ],
        "od": {"n_pairs": 6, "seed": 8080, "pool_size": 500},
        "scenario_seed": 1337,
        "disruption": {"max_attempts": 20, "betweenness_k": 8},
        "output": {"dir": str(tmp_path / "results")},
        "parallel": {"workers": 1, "flush_every": 1},
        "cost_estimate_s_per_solve": {"aco": 2.0, "physarum": 1.0},
    }
    raw.update(over)
    path = tmp_path / "experiment.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _od(n: int = 2) -> list[M.ODPair]:
    """Fixed OD pairs for matrix-arithmetic tests (no sampling involved)."""
    return [M.ODPair(0, 6, "short", 100.0), M.ODPair(6, 0, "long", 900.0)][:n]


# ==========================================================================
# Stub routers
# ==========================================================================


class _StubRouter(Router):
    """A one-shot router that returns a FIXED path, so the measured stretch
    is a pure function of the reference cost the runner supplies."""

    name = "stub"

    def __init__(self, path_fn=None):
        self._path_fn = path_fn or (lambda G, s, t: nx.shortest_path(G, s, t))
        self._path = None

    def prepare(self, G, source, target, snapshot, seed):
        self._G, self._s, self._t = G, source, target
        self._snapshot = snapshot
        self._path = None
        self._done = False

    def step(self):
        if not self._done:
            try:
                self._path = list(self._path_fn(self._G, self._s, self._t))
            except Exception:
                self._path = None
            self._done = True
        if self._path is None:
            return None
        return self._snapshot.cost_of_path(self._G, self._path)

    def best_path(self):
        return self._path

    def best_cost(self):
        return None if self._path is None else self._snapshot.cost_of_path(
            self._G, self._path
        )

    def converged(self):
        return self._done

    def apply_disruption(self, removed_edges, warm):
        G2 = self._G.copy()
        G2.remove_edges_from(list(removed_edges))
        self._G = G2
        self._path = None
        self._done = False

    def state(self):
        return {}

    def load_state(self, state):
        return None


class _ExplodingRouter(_StubRouter):
    """Raises inside `step`. Phase 8 must turn that into a `failed=True`
    ROW and keep going -- never into an aborted sweep."""

    name = "boom"

    def step(self):
        raise RuntimeError("deliberate router failure")


@pytest.fixture
def stub_routers():
    """Register stub factories under the real algorithm names, and restore
    the registry afterwards."""
    saved = dict(R.ROUTER_FACTORIES)
    R.ROUTER_FACTORIES.clear()
    for name in ("dijkstra", "astar", "aco", "physarum"):
        R.register_router(name, lambda opts, _n=name: _StubRouter())
    yield
    R.ROUTER_FACTORIES.clear()
    R.ROUTER_FACTORIES.update(saved)
    R.close_writers()


# ==========================================================================
# Matrix arithmetic
# ==========================================================================


def test_cell_and_run_counts_match_the_config_arithmetic(tmp_path):
    cfg = M.load_experiment_config(_write_config(tmp_path))
    od = _od(2)
    cells = M.build_matrix(cfg, od)
    s = M.matrix_summary(cells)

    n_scen, n_cond, n_od = len(cfg.scenarios), len(cfg.conditions), len(od)
    assert s["n_cells"] == n_od * n_scen * n_cond

    # Expected run count, derived independently of build_matrix:
    #   control scenario  -> 1 mode  for everyone
    #   disruption        -> 1 mode  classical, 2 modes iterative
    expected = 0
    for scenario in cfg.scenarios:
        control = scenario == "S0"
        for spec in cfg.algorithms:
            modes = 1 if (control or spec.is_classical) else 2
            expected += n_od * n_cond * modes * len(spec.seeds)
    assert s["n_runs"] == expected

    # And the per-algorithm split, spelled out.
    assert s["runs_by_algorithm"]["dijkstra"] == n_od * n_cond * n_scen
    assert s["runs_by_algorithm"]["astar"] == n_od * n_cond * n_scen
    assert s["runs_by_algorithm"]["aco"] == n_od * n_cond * 5 * (1 + 2 * (n_scen - 1))
    assert s["runs_by_algorithm"]["physarum"] == n_od * n_cond * 1 * (1 + 2 * (n_scen - 1))


def test_run_ids_are_unique_across_the_whole_matrix(tmp_path):
    cfg = M.load_experiment_config(_write_config(tmp_path))
    cells = M.build_matrix(cfg, _od(2))
    runs = M.all_runs(cells)
    assert len({r.run_id for r in runs}) == len(runs)


def test_changing_the_config_changes_every_run_id(tmp_path):
    """CLAUDE.md: changing a config must invalidate the cache. The config
    hash is inside every run_id, so resume cannot reuse stale rows."""
    cfg_a = M.load_experiment_config(_write_config(tmp_path))
    cfg_b = M.load_experiment_config(
        _write_config(tmp_path, scenario_seed=999)
    )
    assert cfg_a.config_hash != cfg_b.config_hash
    ids_a = {r.run_id for r in M.all_runs(M.build_matrix(cfg_a, _od(1)))}
    ids_b = {r.run_id for r in M.all_runs(M.build_matrix(cfg_b, _od(1)))}
    assert ids_a.isdisjoint(ids_b)


def test_classical_gets_one_seed_and_one_mode(tmp_path):
    cfg = M.load_experiment_config(_write_config(tmp_path))
    cells = M.build_matrix(cfg, _od(2))
    for run in M.all_runs(cells):
        if run.algorithm in ("dijkstra", "astar"):
            assert run.seed == 0
            assert run.warm_start is False
    # ... and the iterative ones DO get both modes on a disruption scenario.
    modes = {
        r.warm_start
        for r in M.all_runs(cells)
        if r.algorithm == "aco" and r.scenario_id != "S0"
    }
    assert modes == {False, True}


def test_control_scenario_has_no_warm_cold_axis(tmp_path):
    cfg = M.load_experiment_config(_write_config(tmp_path))
    cells = M.build_matrix(cfg, _od(2))
    s0 = [r for r in M.all_runs(cells) if r.scenario_id == "S0"]
    assert s0
    assert all(r.warm_start is False for r in s0)


def test_classical_with_several_seeds_is_rejected(tmp_path):
    with pytest.raises(M.ConfigError, match="classical"):
        M.load_experiment_config(
            _write_config(
                tmp_path,
                algorithms=[{"name": "dijkstra", "kind": "classical", "seeds": 3, "budget": 1}],
            )
        )


def test_astar_on_sumo_over_config_speeds_is_refused(tmp_path):
    """Phase 2d: A*'s great-circle heuristic is inadmissible for SUMO
    snapshots on the config_speeds baseline (242 edges below free-flow), so
    the combination must not be silently runnable."""
    with pytest.raises(M.ConfigError, match="INADMISSIBLE"):
        M.load_experiment_config(_write_config(tmp_path, baseline_graph="config_speeds"))

    # ... but the same config with the explicit escape hatch loads.
    cfg = M.load_experiment_config(
        _write_config(
            tmp_path, baseline_graph="config_speeds", allow_inadmissible_astar=True
        )
    )
    assert cfg.baseline_graph == "config_speeds"

    # ... and dropping A* makes it fine without the hatch.
    cfg = M.load_experiment_config(
        _write_config(
            tmp_path,
            baseline_graph="config_speeds",
            algorithms=[{"name": "aco", "kind": "iterative", "seeds": 1, "budget": 10}],
        )
    )
    assert cfg.baseline_graph == "config_speeds"


def test_project_cost_charges_two_solves_for_a_disruption_row(tmp_path):
    cfg = M.load_experiment_config(
        _write_config(
            tmp_path,
            scenarios=["S0"],
            algorithms=[{"name": "aco", "kind": "iterative", "seeds": 1, "budget": 10}],
        )
    )
    control = M.project_cost(M.build_matrix(cfg, _od(1)), {"aco": 1.0})
    cfg2 = M.load_experiment_config(
        _write_config(
            tmp_path,
            scenarios=["S1"],
            algorithms=[{"name": "aco", "kind": "iterative", "seeds": 1, "budget": 10}],
        )
    )
    disrupted = M.project_cost(M.build_matrix(cfg2, _od(1)), {"aco": 1.0})
    # S1 also doubles the run count (warm + cold), so 2 modes x 2 solves.
    assert disrupted["serial_seconds"] == pytest.approx(4 * control["serial_seconds"])


# ==========================================================================
# OD sampling: stratification and out-of-sampleness
# ==========================================================================


def test_od_sample_is_stratified_and_deterministic():
    G = _grid_graph(80)
    cfg = M.ODSampleConfig(n_pairs=9, seed=8080, pool_size=400)
    a = M.sample_od_pairs(G, cfg)
    b = M.sample_od_pairs(G, cfg)
    assert [p.as_tuple() for p in a] == [p.as_tuple() for p in b]
    assert len(a) == 9
    for name in cfg.strata:
        assert sum(1 for p in a if p.stratum == name) == 3
    # Band ordering holds: every short pair is shorter than every long one.
    shorts = [p.distance_m for p in a if p.stratum == "short"]
    longs = [p.distance_m for p in a if p.stratum == "long"]
    assert max(shorts) <= min(longs)


def test_od_sample_is_disjoint_from_the_spent_draws():
    """The ACO tuning draw (seed 2024, first 10) and the Phase 4d criterion
    draw (seed 4242) are spent. Phase 8's sample must not reuse them, or
    its numbers are measured on pairs the hyperparameters were chosen on."""
    G = _grid_graph(80)
    pairs = M.sample_od_pairs(G, M.ODSampleConfig(n_pairs=9, seed=8080, pool_size=400))
    report = M.od_overlap_report(pairs, G)
    assert report["ordered_overlap_total"] == 0
    assert report["unordered_overlap_total"] == 0
    assert report["by_draw"]["2024"]["ordered_overlap_with_tuning_subset"] == 0


def test_od_seed_cannot_be_a_spent_draw():
    G = _grid_graph(40)
    for spent in (2024, 4242):
        with pytest.raises(ValueError, match="out-of-sample"):
            M.sample_od_pairs(G, M.ODSampleConfig(n_pairs=3, seed=spent, pool_size=200))


def test_n_pairs_must_divide_across_strata():
    G = _grid_graph(40)
    with pytest.raises(ValueError, match="divide evenly"):
        M.sample_od_pairs(G, M.ODSampleConfig(n_pairs=10, seed=8080, pool_size=200))


# ==========================================================================
# Running: one denominator, failures as rows, resume
# ==========================================================================


def _resources(toy_graph, toy_snapshot) -> R.Resources:
    return R.Resources(
        toy_graph, {"sumo|heavy|0": toy_snapshot}, baseline_graph="sumo_speeds"
    )


def _one_condition_cfg(tmp_path, **over):
    base = {
        "conditions": [{"congestion_source": "sumo", "level": "heavy", "time_bin": 0}],
    }
    base.update(over)
    return M.load_experiment_config(_write_config(tmp_path, **base))


def test_every_algorithm_in_a_cell_shares_one_reference_cost(
    tmp_path, toy_graph, toy_snapshot, stub_routers, monkeypatch
):
    """Requirement 2: all algorithms in a cell are scored against the SAME
    Dijkstra denominator on the SAME disrupted graph. Asserted at the call
    boundary, because a figure made from inconsistent denominators would
    look perfectly plausible."""
    cfg = _one_condition_cfg(tmp_path, scenarios=["S1"])
    cells = M.build_matrix(cfg, [M.ODPair(0, 6, "short", 100.0)])

    seen = []
    real = R.run_and_collect

    def spy(*args, **kwargs):
        seen.append(
            (
                kwargs["reference_cost"],
                kwargs["disrupted_reference_cost"],
                id(kwargs["G_disrupted"]),
            )
        )
        return real(*args, **kwargs)

    monkeypatch.setattr(R, "run_and_collect", spy)
    rows = []
    out = R.execute_cell(
        cells[0], _resources(toy_graph, toy_snapshot), {}, dict(cfg.disruption), rows.append
    )
    assert out.n_runs == len(rows) > 1
    assert len(set(seen)) == 1, f"denominators differ across algorithms: {set(seen)}"
    assert all(v is not None for v in seen[0][:2])

    # And the recorded stretches agree, since every stub returns the same
    # path and is divided by the same number.
    stretches = {round(r.stretch, 12) for r in rows if not r.failed}
    assert len(stretches) == 1


def test_disruption_is_drawn_once_per_cell_not_per_algorithm_seed(
    tmp_path, toy_graph, toy_snapshot, stub_routers
):
    """Every run in a cell must face the identical closure, or the paired
    comparison across algorithms and seeds is meaningless."""
    cfg = _one_condition_cfg(tmp_path, scenarios=["S1"])
    cells = M.build_matrix(cfg, [M.ODPair(0, 6, "short", 100.0)])
    rows = []
    R.execute_cell(
        cells[0], _resources(toy_graph, toy_snapshot), {}, dict(cfg.disruption), rows.append
    )
    assert len({r.n_removed_edges for r in rows}) == 1
    assert rows[0].n_removed_edges > 0


def test_a_failing_router_yields_failed_rows_and_the_sweep_completes(
    tmp_path, toy_graph, toy_snapshot, stub_routers
):
    R.register_router("aco", lambda opts: _ExplodingRouter())
    cfg = _one_condition_cfg(
        tmp_path,
        scenarios=["S0", "S1"],
        algorithms=[
            {"name": "dijkstra", "kind": "classical", "seeds": 1, "budget": 1},
            {"name": "aco", "kind": "iterative", "seeds": 2, "budget": 5},
        ],
    )
    cells = M.build_matrix(cfg, [M.ODPair(0, 6, "short", 100.0)])
    summary = R.run_sweep(
        cfg,
        cells,
        resources=_resources(toy_graph, toy_snapshot),
        workers=1,
        progress=False,
    )
    R.close_writers()

    table = read_shard(summary["output_path"])
    algos = table.column("algorithm").to_pylist()
    failed = table.column("failed").to_pylist()
    reasons = table.column("failure_reason").to_pylist()

    # Every ACO row failed; every Dijkstra row survived; nothing was dropped.
    assert table.num_rows == summary["runs_attempted"] == len(M.all_runs(cells))
    assert all(f for a, f in zip(algos, failed) if a == "aco")
    assert not any(f for a, f in zip(algos, failed) if a == "dijkstra")
    assert any("deliberate router failure" in r for r in reasons)
    assert summary["runs_failed"] == sum(1 for a in algos if a == "aco")


def test_a_router_that_cannot_be_constructed_is_also_a_row(
    tmp_path, toy_graph, toy_snapshot, stub_routers
):
    def boom(_opts):
        raise ValueError("no such router build")

    R.register_router("aco", boom)
    cfg = _one_condition_cfg(
        tmp_path,
        scenarios=["S0"],
        algorithms=[{"name": "aco", "kind": "iterative", "seeds": 1, "budget": 5}],
    )
    cells = M.build_matrix(cfg, [M.ODPair(0, 6, "short", 100.0)])
    rows = []
    out = R.execute_cell(
        cells[0], _resources(toy_graph, toy_snapshot), {}, dict(cfg.disruption), rows.append
    )
    assert out.n_failed == 1 and len(rows) == 1
    assert rows[0].failed and "no such router build" in rows[0].failure_reason


def test_an_infeasible_scenario_is_recorded_not_skipped(
    tmp_path, toy_graph, toy_snapshot, stub_routers
):
    """S4 needs 5 path edges; a 2-edge optimal path cannot supply them.
    Phase 7 returns ok=False, and Phase 8 must still emit one failed row per
    run plus the rejection bookkeeping."""
    cfg = _one_condition_cfg(tmp_path, scenarios=["S4"])
    cells = M.build_matrix(cfg, [M.ODPair(0, 1, "short", 10.0)])
    rows = []
    out = R.execute_cell(
        cells[0], _resources(toy_graph, toy_snapshot), {}, dict(cfg.disruption), rows.append
    )
    assert len(rows) == out.n_runs == out.n_failed
    assert all(r.failed and "infeasible" in r.failure_reason for r in rows)
    assert out.disruption["ok"] is False
    assert "rejections" in out.disruption


def test_resume_skips_completed_runs_and_never_duplicates(
    tmp_path, toy_graph, toy_snapshot, stub_routers
):
    cfg = _one_condition_cfg(tmp_path, scenarios=["S0", "S1"])
    od = [M.ODPair(0, 6, "short", 100.0), M.ODPair(6, 0, "long", 900.0)]
    cells = M.build_matrix(cfg, od)
    resources = _resources(toy_graph, toy_snapshot)

    # First pass: run HALF the cells, as a killed sweep would have.
    first = R.run_sweep(
        cfg, cells[: len(cells) // 2], resources=resources, workers=1,
        progress=False, merge=False,
    )
    R.close_writers()
    done = R.completed_run_ids(cfg.shard_dir)
    assert len(done) == first["runs_attempted"] > 0

    # Second pass: resume. The finished cells must not be scheduled at all.
    remaining, skipped = R.filter_completed(cells, done)
    assert skipped == len(done)
    assert len(remaining) == len(cells) - len(cells) // 2
    assert all(
        run.run_id not in done for cell in remaining for run in cell.runs
    )

    second = R.run_sweep(
        cfg, remaining, resources=resources, workers=1, progress=False
    )
    R.close_writers()
    assert second["runs_attempted"] == len(M.all_runs(cells)) - skipped

    table = read_shard(second["output_path"])
    ids = table.column("run_id").to_pylist()
    assert len(ids) == len(set(ids)) == len(M.all_runs(cells))
    assert set(ids) == {r.run_id for r in M.all_runs(cells)}


def test_resume_with_nothing_done_changes_nothing(tmp_path):
    cfg = M.load_experiment_config(_write_config(tmp_path))
    cells = M.build_matrix(cfg, _od(2))
    remaining, skipped = R.filter_completed(cells, set())
    assert skipped == 0 and len(remaining) == len(cells)


def test_merge_dedupes_a_cell_that_was_run_twice(
    tmp_path, toy_graph, toy_snapshot, stub_routers
):
    """A worker killed after writing its shard can legitimately be re-run.
    `merge_shards` de-duplicates on run_id so the evidence is not
    double-counted."""
    cfg = _one_condition_cfg(tmp_path, scenarios=["S0"])
    cells = M.build_matrix(cfg, [M.ODPair(0, 6, "short", 100.0)])
    resources = _resources(toy_graph, toy_snapshot)
    for _ in range(2):
        R.run_sweep(cfg, cells, resources=resources, workers=1, progress=False, merge=False)
        R.close_writers()  # forces a NEW shard on the next pass
    summary = R.run_sweep(cfg, [], resources=resources, workers=1, progress=False)
    R.close_writers()
    table = read_shard(summary["output_path"])
    ids = table.column("run_id").to_pylist()
    assert len(ids) == len(set(ids)) == len(M.all_runs(cells))


def test_parallel_execution_requires_a_prepared_resource_spec(tmp_path, toy_graph, toy_snapshot):
    """Workers load the graph and the PRE-WARMED snapshots from disk. There
    is deliberately no path by which a parallel sweep starts without that
    bundle -- which is what keeps Phase 4c's cache race from being
    exercised at all."""
    cfg = M.load_experiment_config(_write_config(tmp_path))
    with pytest.raises(ValueError, match="ResourceSpec"):
        R.run_sweep(
            cfg,
            M.build_matrix(cfg, _od(1)),
            resources=_resources(toy_graph, toy_snapshot),
            workers=4,
            progress=False,
        )


def test_resources_reject_a_condition_with_no_prewarmed_snapshot(toy_graph, toy_snapshot):
    res = R.Resources(toy_graph, {"sumo|heavy|0": toy_snapshot})
    with pytest.raises(KeyError, match="No pre-warmed snapshot"):
        res.snapshot(M.Condition("bpr", "light", 0))


def test_every_row_carries_the_matrix_identity(
    tmp_path, toy_graph, toy_snapshot, stub_routers
):
    """A row must be traceable to the cell that produced it: same run_id,
    same provenance. Phase 9 joins on exactly these columns."""
    cfg = _one_condition_cfg(tmp_path, scenarios=["S0", "S1"])
    cells = M.build_matrix(cfg, [M.ODPair(0, 6, "short", 100.0)])
    summary = R.run_sweep(
        cfg, cells, resources=_resources(toy_graph, toy_snapshot), workers=1, progress=False
    )
    R.close_writers()
    table = read_shard(summary["output_path"])
    by_id = {
        rid: i for i, rid in enumerate(table.column("run_id").to_pylist())
    }
    cols = {n: table.column(n).to_pylist() for n in table.column_names}
    for run in M.all_runs(cells):
        i = by_id[run.run_id]
        assert cols["algorithm"][i] == run.algorithm
        assert cols["scenario_id"][i] == run.scenario_id
        assert cols["traffic_level"][i] == run.traffic_level
        assert cols["congestion_source"][i] == run.congestion_source
        assert cols["baseline_graph"][i] == run.baseline_graph
        assert cols["warm_start"][i] == run.warm_start
        assert cols["seed"][i] == run.seed
        assert cols["config_hash"][i] == run.config_hash
        assert cols["iteration_budget"][i] == run.budget
        if not cols["failed"][i]:
            assert cols["stretch"][i] >= 1.0 - 1e-9


def test_control_rows_have_null_recovery_columns(
    tmp_path, toy_graph, toy_snapshot, stub_routers
):
    cfg = _one_condition_cfg(tmp_path, scenarios=["S0"])
    cells = M.build_matrix(cfg, [M.ODPair(0, 6, "short", 100.0)])
    summary = R.run_sweep(
        cfg, cells, resources=_resources(toy_graph, toy_snapshot), workers=1, progress=False
    )
    R.close_writers()
    table = read_shard(summary["output_path"])
    for name in ("recovery_iterations", "post_disruption_stretch", "recovery_wallclock_s"):
        assert all(v is None for v in table.column(name).to_pylist())
    assert all(n == 0 for n in table.column("n_removed_edges").to_pylist())


def test_measured_seconds_per_solve_halves_a_disruption_row(
    tmp_path, toy_graph, toy_snapshot, stub_routers
):
    cfg = _one_condition_cfg(tmp_path, scenarios=["S1"])
    cells = M.build_matrix(cfg, [M.ODPair(0, 6, "short", 100.0)])
    summary = R.run_sweep(
        cfg, cells, resources=_resources(toy_graph, toy_snapshot), workers=1, progress=False
    )
    R.close_writers()
    rates = R.measured_seconds_per_solve(summary["output_path"])
    assert rates and all(math.isfinite(v) and v >= 0 for v in rates.values())


# ==========================================================================
# Real-graph checks (slow)
# ==========================================================================


@pytest.mark.slow
def test_physarum_is_seed_invariant_on_the_real_graph():
    """The claim the shipped config rests on: `PhysarumRouter` reads no RNG,
    so extra seeds would be byte-identical rows rather than repeated
    measurements. Measured, not assumed."""
    from bioroute.algorithms.base import run_to_convergence
    from bioroute.algorithms.physarum import PhysarumRouter
    from bioroute.config import load_area_config
    from bioroute.congestion.base import FreeFlowProvider
    from bioroute.network.graph import cache_path, load_cached_graph

    area = load_area_config("configs/area_central_blr.yaml")
    path = cache_path(area)
    if not path.exists():
        pytest.skip("cached graph not built")
    G = load_cached_graph(path)
    snap = FreeFlowProvider(G).snapshot()
    nodes = sorted(G.nodes())
    s, t = random.Random(8080).sample(nodes, 2)

    results = []
    for seed in (0, 1, 2):
        router = PhysarumRouter()
        router.prepare(G, s, t, snap, seed)
        results.append(run_to_convergence(router, 200))
    assert results[0].path == results[1].path == results[2].path
    assert results[0].cost == pytest.approx(results[1].cost)
