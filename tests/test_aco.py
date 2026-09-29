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


# The default (no-heuristic) mode's own suggested_budget is 150 iterations
# -- appropriate for the exit-criterion measurement, but too slow to use
# on every fast contract test (these check stretch/edge-validity, not path
# QUALITY, so a much smaller budget is enough and keeps the suite fast).
# Phase 4c lowered this 120 -> 50: n_ants doubled to 80, and with the
# dead-node memo a valid path is found on iteration 1 on every real OD
# pair measured, so these contract tests do not need more.
_FAST_TEST_BUDGET = 50


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
# Phase 4b: the construction hot loop inlines `_weighted_candidates` and
# `_pick_and_remove` for speed (see aco.py `_construct_ant`). Those two
# methods remain as the readable reference form of that inlined code, so
# they can silently drift apart -- these tests are what stops that.
# --------------------------------------------------------------------------


def _reference_construct_ant(router):
    """The Phase 4 construction loop, written against the reference helper
    methods instead of the inlined fast path. Deliberately a transcription
    of `_construct_ant`'s structure with `router._weighted_candidates(...)`
    / `router._pick_and_remove(...)` calls in place of the inlined bodies,
    and with `len(path_idx)` recomputed rather than tracked in `depth`.

    Phase 4c: also mirrors the per-ant dead-node memo
    (`router._use_dead_node_memo`), so the equivalence check covers both
    memo settings."""
    memo = router._use_dead_node_memo
    target_idx = router._target_idx
    path_idx = [router._source_idx]
    edge_path_ids = []
    visited = bytearray(router._n_nodes)
    visited[router._source_idx] = 1
    frames = []
    op_budget = router._max_steps * router._backtrack_multiplier
    ops = 0

    candidates = router._weighted_candidates(router._source_idx, visited)

    while True:
        ops += 1
        if ops > op_budget:
            return None
        if path_idx[-1] == target_idx:
            break
        if len(path_idx) - 1 >= router._max_steps:
            candidates = []
        if not candidates:
            if len(path_idx) == 1:
                return None
            if memo:
                path_idx.pop()
            else:
                visited[path_idx.pop()] = 0
            edge_path_ids.pop()
            candidates = frames.pop()
            if memo:
                candidates = [c for c in candidates if not visited[c[1]]]
            continue
        eid, v_idx = router._pick_and_remove(candidates)
        frames.append(candidates)
        edge_path_ids.append(eid)
        path_idx.append(v_idx)
        visited[v_idx] = 1
        candidates = router._weighted_candidates(v_idx, visited)

    path = [router._node_ids[i] for i in path_idx]
    edge_path = [router._edge_keys[e] for e in edge_path_ids]
    cost = router._snapshot.cost_of_path(router._G, path)
    return path, cost, edge_path


def _compare_inlined_vs_reference(router, n_ants):
    """Run `n_ants` ants both ways from the same RNG state; every ant must
    make identical choices (same path, same cost, same edges, same
    discard decisions)."""
    rng_state = router._rng.getstate()
    inlined = [router._construct_ant() for _ in range(n_ants)]
    router._rng.setstate(rng_state)
    reference = [_reference_construct_ant(router) for _ in range(n_ants)]
    assert len(inlined) == len(reference)
    for a, b in zip(inlined, reference):
        assert (a is None) == (b is None)
        if a is not None:
            assert a[0] == b[0]
            assert a[1] == b[1]
            assert a[2] == b[2]
    return inlined


@pytest.mark.parametrize("use_dead_node_memo", [True, False])
@pytest.mark.parametrize("use_goal_heuristic", [True, False])
def test_aco_hot_loop_matches_reference_helpers_toy(use_goal_heuristic, use_dead_node_memo):
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = ACORouter(
        config=_TOY_TEST_CONFIG,
        use_goal_heuristic=use_goal_heuristic,
        use_dead_node_memo=use_dead_node_memo,
    )
    router.prepare(G, "A", "E", snap, seed=3)
    router.step()  # get off the uniform-pheromone starting state
    results = _compare_inlined_vs_reference(router, n_ants=25)
    assert any(r is not None for r in results)


def test_aco_hot_loop_matches_reference_helpers_real(real_graph, small_od_pairs):
    """Same check on the real network, where backtracking actually fires
    (most ants here dead-end repeatedly, so this exercises the inlined
    backtrack/resume path, not just the happy path)."""
    snap = FreeFlowProvider(real_graph).snapshot()
    s, t = small_od_pairs[0]
    router = ACORouter()
    router.prepare(real_graph, s, t, snap, seed=0)
    router.step()
    _compare_inlined_vs_reference(router, n_ants=15)


# --------------------------------------------------------------------------
# Phase 4c: the per-ant dead-node memo. It is a LOSSY construction
# heuristic, not an optimization (see aco.py's module docstring and
# configs/aco.yaml). These tests pin down both halves of that claim: it is
# on by default and it really does shrink the ants' reachable search space.
# --------------------------------------------------------------------------


def _grid_with_culdesacs(n: int = 4) -> nx.MultiDiGraph:
    """A 4x4 bidirectional grid plus two-node cul-de-sac spurs -- the
    smallest structure that reproduces what makes the real Bengaluru graph
    hard for feed-forward ants (a spur's only way out is back to the node
    the ant came from, i.e. a genuine dead end under a node tabu)."""
    G = nx.MultiDiGraph()
    for i in range(n):
        for j in range(n):
            G.add_node((i, j), y=0.001 * i, x=0.001 * j)

    def link(a, b):
        G.add_edge(a, b, key=0, length=100.0, speed_kph=36.0, travel_time=10.0)

    for i in range(n):
        for j in range(n):
            for di, dj in ((0, 1), (1, 0), (0, -1), (-1, 0)):
                a, b = i + di, j + dj
                if 0 <= a < n and 0 <= b < n:
                    link((i, j), (a, b))
    for idx, anchor in enumerate([(1, 1), (2, 2), (1, 2), (2, 1), (0, 2), (3, 1)]):
        c1, c2 = ("spur", idx, 0), ("spur", idx, 1)
        G.add_node(c1, y=0.0005 * idx, x=0.005)
        G.add_node(c2, y=0.0005 * idx, x=0.006)
        link(anchor, c1)
        link(c1, anchor)
        link(c1, c2)
        link(c2, c1)
    return G


def test_aco_dead_node_memo_is_on_by_default():
    """The shipped default is memo ON (configs/aco.yaml). If this ever
    flips silently, every Phase 4c number in docs/tuning/aco_sweep.md
    stops describing the shipped code."""
    assert DEFAULT_CONFIG["use_dead_node_memo"] is True
    assert ACORouter().config["use_dead_node_memo"] is True
    assert ACORouter(use_dead_node_memo=False).config["use_dead_node_memo"] is False


def test_aco_dead_node_memo_shrinks_the_reachable_search_space():
    """The memo's documented cost, made concrete and reproducible.

    Deadness is context-dependent on an ant's visited set, so memoising it
    prunes branches that are genuinely still open. Running the SAME ant
    construction from the SAME seed with the memo on and off, the set of
    source->target paths the memo-on ants can produce must be a STRICT
    SUBSET of what the memo-off ants can produce -- the memo never opens a
    path, it only closes them. (On the real graph roughly 66% of memo-prune
    events refuse a node that could still have reached the target; see
    docs/tuning/aco_sweep.md Phase 4c.)"""
    G = _grid_with_culdesacs()
    snap = TrafficSnapshot(
        level="freeflow",
        time_bin=0,
        source="freeflow",
        travel_time={
            (u, v, k): d["travel_time"] for u, v, k, d in G.edges(keys=True, data=True)
        },
    )
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({"n_ants": 1, "use_goal_heuristic": False, "beta_no_heuristic": 0.5})

    found = {}
    for memo in (False, True):
        router = ACORouter(config=cfg, use_dead_node_memo=memo)
        router.prepare(G, (0, 0), (3, 3), snap, seed=11)
        paths = set()
        for _ in range(4000):
            res = router._construct_ant()
            if res is not None:
                paths.add(tuple(res[0]))
        found[memo] = paths

    assert found[True] < found[False], (
        "memo-on ants should reach a strict subset of the paths memo-off ants "
        f"reach; got {len(found[True])} vs {len(found[False])}"
    )
    assert not (found[True] - found[False])


def test_aco_dead_node_memo_off_still_routes(real_graph, small_od_pairs):
    """memo=False must remain a working, reproducible mode -- it is the
    Phase 4/4b search and the paper's ablation baseline."""
    snap = FreeFlowProvider(real_graph).snapshot()
    s, t = small_od_pairs[0]
    r = ACORouter(use_dead_node_memo=False)
    r.prepare(real_graph, s, t, snap, seed=0)
    assert r._use_dead_node_memo is False
    res = run_to_convergence(r, budget=_FAST_TEST_BUDGET)
    if res.path is not None:
        assert res.path[0] == s and res.path[-1] == t
        for a, b in zip(res.path, res.path[1:]):
            assert real_graph.has_edge(a, b)


@pytest.mark.parametrize("use_dead_node_memo", [True, False])
def test_aco_determinism_holds_in_both_memo_modes(use_dead_node_memo):
    G = _toy_graph()
    snap = _toy_snapshot(G)
    out = []
    for _ in range(2):
        r = ACORouter(config=_TOY_TEST_CONFIG, use_dead_node_memo=use_dead_node_memo)
        r.prepare(G, "A", "E", snap, seed=7)
        out.append(run_to_convergence(r, budget=60))
    assert out[0].path == out[1].path
    assert out[0].cost == out[1].cost
    assert out[0].trace == out[1].trace


# --------------------------------------------------------------------------
# Slow: the actual exit-criterion measurement (30 OD pairs x 2 congestion
# levels). Not run by default `pytest tests/ -v`.
# --------------------------------------------------------------------------


def _measure_exit_criterion(
    real_graph, od_pairs, snap, use_goal_heuristic, budget, use_dead_node_memo=None
):
    """Run ACO on every OD pair and report everything the Phase 4 exit
    criterion is graded on: fraction at stretch<=1.10, median/worst
    stretch, median iterations, ant completion rate and wall-clock per
    pair. Returns a dict; `frac` is the criterion itself.

    Per CLAUDE.md, `stretch < 1.0` is a bug, so it is asserted here rather
    than merely reported."""
    import statistics
    import time

    stretches = []
    iters = []
    walls = []
    ants_total = 0
    ants_done = 0

    for s, t in od_pairs:
        d_router = DijkstraRouter()
        d_router.prepare(real_graph, s, t, snap, seed=0)
        d_result = run_to_convergence(d_router, budget=1)
        if d_result.cost is None:
            continue

        a_router = ACORouter(
            use_goal_heuristic=use_goal_heuristic,
            use_dead_node_memo=use_dead_node_memo,
        )
        a_router.prepare(real_graph, s, t, snap, seed=0)
        t0 = time.perf_counter()
        a_result = run_to_convergence(a_router, budget=budget)
        walls.append(time.perf_counter() - t0)
        iters.append(a_result.iterations)
        ants_total += a_router._ants_total
        ants_done += a_router._ants_completed

        if a_result.cost is None:
            stretches.append(float("inf"))
            continue

        stretch = a_result.cost / d_result.cost
        assert stretch >= 1.0 - 1e-9, (
            f"stretch {stretch} < 1.0 for OD ({s},{t}) -- ACO beat Dijkstra on "
            "the same weighted graph, which is a bug per CLAUDE.md's invariant."
        )
        stretches.append(stretch)

    finite = [x for x in stretches if x != float("inf")]
    ok = sum(1 for x in stretches if x <= 1.10)
    return {
        "frac": ok / len(stretches) if stretches else 0.0,
        "ok": ok,
        "n": len(stretches),
        "median": statistics.median(finite) if finite else float("inf"),
        "worst": max(stretches) if stretches else float("inf"),
        "median_iters": statistics.median(iters) if iters else 0,
        "completion": ants_done / ants_total if ants_total else float("nan"),
        "wall_per_pair": statistics.fmean(walls) if walls else 0.0,
    }


def _report(tag, r):
    line = (
        f"[{tag}] {r['ok']}/{r['n']} pairs at stretch<=1.10 ({r['frac']:.0%}) | "
        f"median {r['median']:.3f} | worst {r['worst']:.3f} | "
        f"median iters {r['median_iters']:.0f} | ant completion {r['completion']:.0%} | "
        f"{r['wall_per_pair']:.1f} s/pair"
    )
    print(line)
    return line


@pytest.mark.slow
def test_aco_exit_criterion_30_od_pairs_no_heuristic(real_graph, area_cfg, tmp_path):
    """The headline mode (configs/aco.yaml default: use_goal_heuristic=false,
    classical MMAS eta=1/travel_time only). This IS the exit criterion this
    project reports against -- an honest miss here is a real result, not a
    reason to weaken the assertion (see docs/tuning/aco_sweep.md).

    MEASURED IN PHASE 4C, AND IT CURRENTLY FAILS ON FREEFLOW:

        freeflow    26/30 = 86.7% at stretch<=1.10  (misses the 90% bar)
        bpr_heavy   29/30 = 96.7%                   (passes)

    at `suggested_budget=150`, seed 0, median stretch 1.000 / 1.009, worst
    1.289 / 1.171, ant completion 82% / 81%. All four freeflow failures are
    long-hop pairs (46-83 hops) and three of them have already CONVERGED,
    so they are settled local optima rather than unfinished searches;
    doubling the budget to 300 changes nothing.

    This assertion is deliberately left at 0.90. A failing slow test that
    states the real number is the honest record of a missed exit criterion;
    relaxing it, or switching the default to the goal-directed mode (which
    measures no better -- 86.7% freeflow, 90.0% BPR heavy), would be moving
    the goalpost. See docs/tuning/aco_sweep.md's Phase 4c section."""
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
    results = {}

    for level, snap in snapshots.items():
        r = _measure_exit_criterion(
            real_graph, od_pairs, snap, use_goal_heuristic=False, budget=budget
        )
        _report(f"no-heuristic/{level}", r)
        results[level] = r

    for level, r in results.items():
        assert r["frac"] >= 0.90, (
            f"{level}: only {r['frac']:.0%} of pairs met stretch<=1.10 "
            f"(median {r['median']:.3f}, worst {r['worst']:.3f}). This is the "
            "measured result -- see docs/tuning/aco_sweep.md Phase 4c. Do NOT "
            "weaken this assertion to make it pass."
        )


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
        r = _measure_exit_criterion(
            real_graph, od_pairs, snap, use_goal_heuristic=True, budget=budget
        )
        _report(f"goal-heuristic/{level}", r)
        assert r["frac"] > 0.0, f"{level}: goal-heuristic ablation found 0 valid results"


def load_aco_config_helper():
    from bioroute.algorithms.aco import load_aco_config

    return load_aco_config(str(ACO_CONFIG_PATH))
