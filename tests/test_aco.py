"""Phase 4 tests: MMAS ACORouter contract sanity, toy-graph correctness,
determinism, the stretch invariant, and both disruption-recovery
strategies. The 30-OD-pair exit-criterion measurement (>=90% of pairs at
stretch <= 1.10, under freeflow AND heavy BPR) lives in the slow test at
the bottom, marked @pytest.mark.slow.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import networkx as nx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bioroute.config import load_area_config
from bioroute.network.graph import cache_path, load_cached_graph
from bioroute.congestion.base import FreeFlowProvider
from bioroute.congestion.bpr import BPRProvider
from bioroute.congestion.snapshot import TrafficSnapshot
from bioroute.algorithms.base import run_to_convergence
from bioroute.algorithms.classical import DijkstraRouter
from bioroute.algorithms.aco import ACORouter, DEFAULT_CONFIG

CONFIG_PATH = ROOT / "configs" / "area_central_blr.yaml"
CONGESTION_CONFIG_PATH = ROOT / "configs" / "congestion.yaml"
ACO_CONFIG_PATH = ROOT / "configs" / "aco.yaml"


# --------------------------------------------------------------------------
# Toy graph: known optimal path (reused from test_classical's chain-vs-
# shortcut construction so the "known optimum" reasoning transfers).
# --------------------------------------------------------------------------


def _toy_graph() -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()
    coords = {
        "A": (0.0000, 0.0),
        "B": (0.0010, 0.0),
        "C": (0.0020, 0.0),
        "D": (0.0030, 0.0),
        "E": (0.0040, 0.0),
    }
    for n, (y, x) in coords.items():
        G.add_node(n, y=y, x=x)

    seg_len_m = 111.19
    for u, v in [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")]:
        speed_kph = 36.0
        tt = seg_len_m / (speed_kph * 1000.0 / 3600.0)
        G.add_edge(u, v, key=0, length=seg_len_m, speed_kph=speed_kph, travel_time=tt)

    direct_len_m = 4 * seg_len_m
    direct_speed_kph = 18.0
    direct_tt = direct_len_m / (direct_speed_kph * 1000.0 / 3600.0)
    G.add_edge("A", "E", key=0, length=direct_len_m, speed_kph=direct_speed_kph, travel_time=direct_tt)
    return G


def _toy_snapshot(G: nx.MultiDiGraph) -> TrafficSnapshot:
    tt = {(u, v, k): data["travel_time"] for u, v, k, data in G.edges(keys=True, data=True)}
    return TrafficSnapshot(level="freeflow", time_bin=0, source="freeflow", travel_time=tt)


_CHAIN_COST = 4 * 111.19 / (36.0 * 1000.0 / 3600.0)


_TOY_TEST_CONFIG = dict(DEFAULT_CONFIG)
_TOY_TEST_CONFIG.update(
    {
        "n_ants": 20,
        "k_stall": 10,
        "beta": 4.0,
        "beta_no_heuristic": 4.0,
        "rho": 0.15,
    }
)


@pytest.mark.parametrize("use_goal_heuristic", [True, False])
def test_aco_toy_finds_known_optimal(use_goal_heuristic):
    """Known-optimum test in BOTH eta modes -- the goal heuristic is a
    config switch (configs/aco.yaml `use_goal_heuristic`), not a hardcoded
    default, and both modes must work."""
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = ACORouter(config=_TOY_TEST_CONFIG, use_goal_heuristic=use_goal_heuristic)
    router.prepare(G, "A", "E", snap, seed=0)
    result = run_to_convergence(router, budget=200)
    assert result.path == ["A", "B", "C", "D", "E"]
    assert result.cost == pytest.approx(_CHAIN_COST, rel=1e-6)


def test_aco_determinism_same_seed():
    G = _toy_graph()
    snap = _toy_snapshot(G)

    router1 = ACORouter(config=_TOY_TEST_CONFIG)
    router1.prepare(G, "A", "E", snap, seed=7)
    result1 = run_to_convergence(router1, budget=60)

    router2 = ACORouter(config=_TOY_TEST_CONFIG)
    router2.prepare(G, "A", "E", snap, seed=7)
    result2 = run_to_convergence(router2, budget=60)

    assert result1.path == result2.path
    assert result1.cost == result2.cost
    assert result1.trace == result2.trace
    assert result1.converged == result2.converged


def test_aco_no_valid_ant_does_not_crash_or_falsely_converge():
    """A target unreachable from the source: every ant dead-ends every
    iteration. step() must keep returning None, and converged() must never
    fire."""
    G = nx.MultiDiGraph()
    G.add_node("A", y=0.0, x=0.0)
    G.add_node("B", y=0.001, x=0.0)
    # no edges at all between A and B -- isolated nodes.
    snap = TrafficSnapshot(
        level="freeflow", time_bin=0, source="freeflow", travel_time={("A", "A", 0): 1.0}
    )
    # Give A a self-loop so travel_time is non-empty and adjacency-building
    # doesn't choke, but there is still no path A -> B.
    G.add_edge("A", "A", key=0, length=1.0, speed_kph=10.0, travel_time=1.0)

    router = ACORouter(config=_TOY_TEST_CONFIG)
    router.prepare(G, "A", "B", snap, seed=1)
    result = run_to_convergence(router, budget=30)
    assert result.path is None
    assert result.cost is None
    assert result.converged is False
    assert all(c == float("inf") for c in result.trace)


# --------------------------------------------------------------------------
# Real network fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def area_cfg():
    return load_area_config(CONFIG_PATH)


@pytest.fixture(scope="module")
def real_graph(area_cfg):
    path = cache_path(area_cfg)
    if not path.exists():
        pytest.skip(f"Cached graph not found at {path}; run scripts/01_build_network.py first.")
    return load_cached_graph(path)


@pytest.fixture(scope="module")
def small_od_pairs(real_graph):
    rng = random.Random(99)
    nodes = sorted(real_graph.nodes())
    return [tuple(rng.sample(nodes, 2)) for _ in range(3)]


# The default (no-heuristic) mode's own suggested_budget is 600 iterations
# -- appropriate for the exit-criterion measurement, but too slow to use
# on every fast contract test (these check stretch/edge-validity, not path
# QUALITY, so a much smaller budget is enough and keeps the suite fast).
_FAST_TEST_BUDGET = 120


def test_aco_stretch_invariant_never_below_one(real_graph, small_od_pairs):
    snap = FreeFlowProvider(real_graph).snapshot()
    for s, t in small_od_pairs:
        d_router = DijkstraRouter()
        d_router.prepare(real_graph, s, t, snap, seed=0)
        d_result = run_to_convergence(d_router, budget=1)
        if d_result.cost is None:
            continue

        a_router = ACORouter()
        a_router.prepare(real_graph, s, t, snap, seed=0)
        a_result = run_to_convergence(a_router, budget=_FAST_TEST_BUDGET)
        if a_result.cost is None:
            continue

        stretch = a_result.cost / d_result.cost
        assert stretch >= 1.0 - 1e-9, (
            f"stretch {stretch} < 1.0 for OD ({s},{t}) -- ACO beat Dijkstra, "
            "which is a bug per CLAUDE.md's invariant."
        )


def test_aco_warm_disruption_avoids_severed_edges(real_graph, small_od_pairs):
    snap = FreeFlowProvider(real_graph).snapshot()
    s, t = small_od_pairs[0]

    router = ACORouter(disruption_strategy="zero")
    router.prepare(real_graph, s, t, snap, seed=0)
    result = run_to_convergence(router, budget=_FAST_TEST_BUDGET)
    if result.path is None or len(result.path) < 3:
        pytest.skip(f"No usable initial path found for OD ({s},{t}) to disrupt.")

    # Sever an edge actually used by the found path.
    u, v = result.path[0], result.path[1]
    edge_data = real_graph.get_edge_data(u, v)
    k = next(iter(edge_data.keys()))
    severed = [(u, v, k)]

    router.apply_disruption(severed, warm=True)
    result2 = run_to_convergence(router, budget=_FAST_TEST_BUDGET)

    if result2.path is not None:
        edges_used = set(zip(result2.path, result2.path[1:]))
        for su, sv, _sk in severed:
            assert (su, sv) not in edges_used


@pytest.mark.parametrize("strategy", ["zero", "local_reset"])
def test_aco_both_disruption_strategies_produce_valid_paths(real_graph, small_od_pairs, strategy):
    snap = FreeFlowProvider(real_graph).snapshot()
    s, t = small_od_pairs[1] if len(small_od_pairs) > 1 else small_od_pairs[0]

    router = ACORouter(disruption_strategy=strategy)
    router.prepare(real_graph, s, t, snap, seed=0)
    result = run_to_convergence(router, budget=_FAST_TEST_BUDGET)
    if result.path is None or len(result.path) < 3:
        pytest.skip(f"No usable initial path found for OD ({s},{t}) to disrupt.")

    u, v = result.path[0], result.path[1]
    edge_data = real_graph.get_edge_data(u, v)
    k = next(iter(edge_data.keys()))
    severed = [(u, v, k)]

    router.apply_disruption(severed, warm=True)
    result2 = run_to_convergence(router, budget=_FAST_TEST_BUDGET)

    if result2.path is not None:
        assert result2.path[0] == s
        assert result2.path[-1] == t
        # Every consecutive pair must be a real edge in the disrupted graph.
        for a, b in zip(result2.path, result2.path[1:]):
            assert real_graph.has_edge(a, b)


def test_aco_goal_heuristic_switch_changes_behavior(real_graph, small_od_pairs):
    """The config switch actually does something: with the heuristic off,
    eta has no h(v) term (so beta_no_heuristic, not beta, governs it); with
    it on, ACORouter behaves like the ablation mode. Both must produce a
    valid path on the same OD pair."""
    snap = FreeFlowProvider(real_graph).snapshot()
    s, t = small_od_pairs[0]

    r_local = ACORouter(use_goal_heuristic=False)
    assert r_local.config["use_goal_heuristic"] is False
    r_local.prepare(real_graph, s, t, snap, seed=0)
    assert r_local._use_goal_heuristic is False
    assert r_local._beta == pytest.approx(r_local.config["beta_no_heuristic"])
    res_local = run_to_convergence(r_local, budget=_FAST_TEST_BUDGET)

    r_goal = ACORouter(use_goal_heuristic=True)
    assert r_goal.config["use_goal_heuristic"] is True
    r_goal.prepare(real_graph, s, t, snap, seed=0)
    assert r_goal._use_goal_heuristic is True
    assert r_goal._beta == pytest.approx(r_goal.config["beta"])
    res_goal = run_to_convergence(r_goal, budget=_FAST_TEST_BUDGET)

    assert res_local.path is not None
    assert res_goal.path is not None
    assert res_local.path[0] == s and res_local.path[-1] == t
    assert res_goal.path[0] == s and res_goal.path[-1] == t


# --------------------------------------------------------------------------
# Slow: the actual exit-criterion measurement (30 OD pairs x 2 congestion
# levels). Not run by default `pytest tests/ -v`.
# --------------------------------------------------------------------------


def _measure_exit_criterion(real_graph, od_pairs, snap, use_goal_heuristic, budget):
    stretches = []
    for s, t in od_pairs:
        d_router = DijkstraRouter()
        d_router.prepare(real_graph, s, t, snap, seed=0)
        d_result = run_to_convergence(d_router, budget=1)
        if d_result.cost is None:
            continue

        a_router = ACORouter(use_goal_heuristic=use_goal_heuristic)
        a_router.prepare(real_graph, s, t, snap, seed=0)
        a_result = run_to_convergence(a_router, budget=budget)
        if a_result.cost is None:
            stretches.append(float("inf"))
            continue

        stretch = a_result.cost / d_result.cost
        assert stretch >= 1.0 - 1e-9
        stretches.append(stretch)

    ok = sum(1 for x in stretches if x <= 1.10)
    frac = ok / len(stretches) if stretches else 0.0
    return frac, ok, len(stretches)


@pytest.mark.slow
def test_aco_exit_criterion_30_od_pairs_no_heuristic(real_graph, area_cfg, tmp_path):
    """The headline mode (configs/aco.yaml default: use_goal_heuristic=false,
    classical MMAS eta=1/travel_time only). This IS the exit criterion this
    project reports against -- an honest miss here is a real result, not a
    reason to weaken the assertion (see docs/tuning/aco_sweep.md)."""
    rng = random.Random(2024)
    nodes = sorted(real_graph.nodes())
    od_pairs = [tuple(rng.sample(nodes, 2)) for _ in range(30)]

    bpr = BPRProvider(
        real_graph, area_cfg, CONGESTION_CONFIG_PATH, cache_dir=tmp_path / "bpr_cache"
    )
    snapshots = {
        "freeflow": FreeFlowProvider(real_graph).snapshot(),
        "bpr_heavy": bpr.snapshot("heavy", time_bin=1),
    }
    budget = load_aco_config_helper()["suggested_budget"]

    for level, snap in snapshots.items():
        frac, ok, n = _measure_exit_criterion(
            real_graph, od_pairs, snap, use_goal_heuristic=False, budget=budget
        )
        print(f"[no-heuristic/{level}] {ok}/{n} pairs at stretch<=1.10 ({frac:.0%})")
        assert frac >= 0.90, f"{level}: only {frac:.0%} of pairs met stretch<=1.10"


@pytest.mark.slow
def test_aco_exit_criterion_30_od_pairs_goal_heuristic_ablation(real_graph, area_cfg, tmp_path):
    """The goal-heuristic ablation (use_goal_heuristic=true). Measured and
    reported for comparison -- NOT separately re-tuned, and not the
    project's chosen headline mode, so this does not hard-assert the 90%
    bar the same way the no-heuristic test does; it still asserts a much
    weaker sanity floor (>0) so a real regression (e.g. every ant failing)
    is still caught."""
    rng = random.Random(2024)
    nodes = sorted(real_graph.nodes())
    od_pairs = [tuple(rng.sample(nodes, 2)) for _ in range(30)]

    bpr = BPRProvider(
        real_graph, area_cfg, CONGESTION_CONFIG_PATH, cache_dir=tmp_path / "bpr_cache"
    )
    snapshots = {
        "freeflow": FreeFlowProvider(real_graph).snapshot(),
        "bpr_heavy": bpr.snapshot("heavy", time_bin=1),
    }
    budget = load_aco_config_helper()["suggested_budget_goal_heuristic"]

    for level, snap in snapshots.items():
        frac, ok, n = _measure_exit_criterion(
            real_graph, od_pairs, snap, use_goal_heuristic=True, budget=budget
        )
        print(f"[goal-heuristic/{level}] {ok}/{n} pairs at stretch<=1.10 ({frac:.0%})")
        assert frac > 0.0, f"{level}: goal-heuristic ablation found 0 valid results"


def load_aco_config_helper():
    from bioroute.algorithms.aco import load_aco_config

    return load_aco_config(str(ACO_CONFIG_PATH))
