"""Phase 5 tests: Physarum tube-reinforcement router.

Toy-graph correctness (Kirchhoff conservation, known-optimal-path exactness,
determinism, warm-start state retention, both adaptation variants) plus the
real-network exit criterion (>=80% exact Dijkstra match under freeflow, 30
OD pairs) as a `@pytest.mark.slow` test.
"""

from __future__ import annotations

import random
import statistics
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
from bioroute.algorithms.physarum import PhysarumRouter, load_physarum_config

CONFIG_PATH = ROOT / "configs" / "area_central_blr.yaml"
CONGESTION_CONFIG_PATH = ROOT / "configs" / "congestion.yaml"
PHYSARUM_CONFIG_PATH = ROOT / "configs" / "physarum.yaml"


def _make_router(variant: str | None = None) -> PhysarumRouter:
    router = PhysarumRouter(PHYSARUM_CONFIG_PATH)
    if variant is not None:
        router._variant = variant  # test-only override of the config switch
    return router


# --------------------------------------------------------------------------
# Toy graph with a known optimal path (chain A-B-C-D-E beats the direct
# A-E "shortcut", same construction/reasoning as tests/test_classical.py).
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
        # Bidirectional (physical roads carry traffic both ways).
        G.add_edge(v, u, key=0, length=seg_len_m, speed_kph=speed_kph, travel_time=tt)

    direct_len_m = 4 * seg_len_m
    direct_speed_kph = 18.0
    direct_tt = direct_len_m / (direct_speed_kph * 1000.0 / 3600.0)
    G.add_edge("A", "E", key=0, length=direct_len_m, speed_kph=direct_speed_kph, travel_time=direct_tt)
    G.add_edge("E", "A", key=0, length=direct_len_m, speed_kph=direct_speed_kph, travel_time=direct_tt)
    return G


def _toy_snapshot(G: nx.MultiDiGraph) -> TrafficSnapshot:
    tt = {(u, v, k): data["travel_time"] for u, v, k, data in G.edges(keys=True, data=True)}
    return TrafficSnapshot(level="freeflow", time_bin=0, source="freeflow", travel_time=tt)


_CHAIN_COST = 4 * 111.19 / (36.0 * 1000.0 / 3600.0)
_DIRECT_COST = (4 * 111.19) / (18.0 * 1000.0 / 3600.0)


def _run(router, budget=300):
    return run_to_convergence(router, budget=budget)


# --------------------------------------------------------------------------
# Known optimal path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["tero", "bonifaci"])
def test_physarum_toy_finds_known_optimal(variant):
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = _make_router(variant)
    router.prepare(G, "A", "E", snap, seed=0)
    result = _run(router)
    assert result.path == ["A", "B", "C", "D", "E"]
    assert result.cost == pytest.approx(_CHAIN_COST, rel=1e-6)


def test_physarum_stretch_ge_one_on_toy():
    G = _toy_graph()
    snap = _toy_snapshot(G)

    d_router = DijkstraRouter()
    d_router.prepare(G, "A", "E", snap, seed=0)
    d_result = _run(d_router, budget=1)

    p_router = _make_router()
    p_router.prepare(G, "A", "E", snap, seed=0)
    p_result = _run(p_router)

    stretch = p_result.cost / d_result.cost
    assert stretch >= 1.0 - 1e-9


# --------------------------------------------------------------------------
# Kirchhoff conservation
# --------------------------------------------------------------------------


def test_kirchhoff_conservation():
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    router.step()

    net = router.last_flux_by_node()
    I0 = router._I0

    assert net["A"] == pytest.approx(-I0, abs=1e-6)
    assert net["E"] == pytest.approx(I0, abs=1e-6)
    for node in ("B", "C", "D"):
        assert net[node] == pytest.approx(0.0, abs=1e-6)


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_determinism_same_seed_identical_result():
    G = _toy_graph()
    snap = _toy_snapshot(G)

    def run_once():
        router = _make_router("tero")
        router.prepare(G, "A", "E", snap, seed=7)
        return _run(router)

    r1 = run_once()
    r2 = run_once()
    assert r1.path == r2.path
    assert r1.cost == pytest.approx(r2.cost)
    assert r1.iterations == r2.iterations
    assert r1.trace == pytest.approx(r2.trace)


# --------------------------------------------------------------------------
# Warm-start disruption: severed edges never reappear, surviving
# conductivities are genuinely retained (not silently reset).
# --------------------------------------------------------------------------


def test_warm_start_avoids_severed_edges_and_retains_state():
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    _run(router)

    D_before = dict(zip(router._pairs, router._D.copy()))

    # Sever the chain's B-C link (both directions) -- the direct A-E
    # shortcut must now be found instead.
    router.apply_disruption([("B", "C", 0), ("C", "B", 0)], warm=True)

    # The severed pair's D must be forced to 0...
    assert router._D[router._pair_index[("B", "C")]] == 0.0
    # ...but every surviving pair's conductivity must be untouched.
    for pair, d_val in D_before.items():
        if pair == ("B", "C"):
            continue
        i = router._pair_index[pair]
        assert router._D[i] == pytest.approx(d_val)

    result = _run(router)
    assert result.path is not None
    for u, v in zip(result.path, result.path[1:]):
        assert (u, v) not in {("B", "C"), ("C", "B")}
    assert result.path == ["A", "E"]
    assert result.cost == pytest.approx(_DIRECT_COST, rel=1e-6)


def test_cold_start_reinitializes_conductivity():
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    _run(router)

    router.apply_disruption([("B", "C", 0), ("C", "B", 0)], warm=False)
    for pair in router._pairs:
        i = router._pair_index[pair]
        assert router._D[i] == pytest.approx(router._D_init)


# --------------------------------------------------------------------------
# Extraction method is always recorded
# --------------------------------------------------------------------------


def test_extraction_method_recorded_every_step():
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    for _ in range(5):
        router.step()
        assert router.last_extraction_method() in (
            "max_flux_walk",
            "theta_subgraph_fallback",
            "full_graph_fallback",
            "no_path_found",
        )
    counts = router.extraction_counts()
    assert sum(counts.values()) == 5


# --------------------------------------------------------------------------
# Real network: exit criterion (>=80% exact Dijkstra match, freeflow, 30 OD
# pairs) + BPR-heavy stretch (measured, no target).
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
def od_pairs_30(real_graph):
    rng = random.Random(2024)
    nodes = sorted(real_graph.nodes())
    pairs = []
    attempts = 0
    while len(pairs) < 30 and attempts < 30 * 50:
        attempts += 1
        s, t = rng.sample(nodes, 2)
        if nx.has_path(real_graph, s, t):
            pairs.append((s, t))
    return pairs


@pytest.mark.slow
def test_physarum_exit_criterion_freeflow(real_graph, od_pairs_30):
    snap = FreeFlowProvider(real_graph).snapshot()
    stretches = []
    exact = 0
    solve_times = []
    for s, t in od_pairs_30:
        d_router = DijkstraRouter()
        d_router.prepare(real_graph, s, t, snap, seed=0)
        d_result = _run(d_router, budget=1)
        if d_result.cost is None:
            continue

        p_router = _make_router("tero")
        p_router.prepare(real_graph, s, t, snap, seed=0)
        p_result = _run(p_router, budget=int(load_physarum_config(PHYSARUM_CONFIG_PATH)["default_iteration_budget"]))
        if p_result.cost is None:
            stretches.append(float("inf"))
            continue

        stretch = p_result.cost / d_result.cost
        assert stretch >= 1.0 - 1e-6, f"stretch < 1.0 for OD ({s},{t}): {stretch}"
        stretches.append(stretch)
        if abs(stretch - 1.0) < 1e-6:
            exact += 1
        solve_times.extend([p_router.mean_solve_time_s()])

    frac_exact = exact / len(stretches)
    print(f"\n[physarum exit criterion] freeflow: {exact}/{len(stretches)} exact "
          f"({frac_exact:.0%}), median stretch={statistics.median(stretches):.4f}, "
          f"worst stretch={max(stretches):.4f}, "
          f"mean solve time={statistics.mean(solve_times)*1000:.2f} ms")
    assert frac_exact >= 0.80, (
        f"Physarum exit criterion FAILED: only {frac_exact:.0%} exact matches "
        f"(need >=80%). Stretches: {stretches}"
    )


@pytest.mark.slow
def test_physarum_bpr_heavy_measured(real_graph, area_cfg, od_pairs_30, tmp_path):
    bpr = BPRProvider(
        real_graph, area_cfg, CONGESTION_CONFIG_PATH, cache_dir=tmp_path / "bpr_cache"
    )
    snap = bpr.snapshot("heavy", time_bin=1)
    stretches = []
    for s, t in od_pairs_30:
        d_router = DijkstraRouter()
        d_router.prepare(real_graph, s, t, snap, seed=0)
        d_result = _run(d_router, budget=1)
        if d_result.cost is None:
            continue
        p_router = _make_router("tero")
        p_router.prepare(real_graph, s, t, snap, seed=0)
        p_result = _run(p_router, budget=int(load_physarum_config(PHYSARUM_CONFIG_PATH)["default_iteration_budget"]))
        if p_result.cost is None:
            continue
        stretch = p_result.cost / d_result.cost
        assert stretch >= 1.0 - 1e-6
        stretches.append(stretch)

    print(f"\n[physarum BPR heavy] median stretch={statistics.median(stretches):.4f}, "
          f"worst stretch={max(stretches):.4f} (no target -- measured only)")
