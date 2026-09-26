"""Phase 3 tests: Router contract sanity + Dijkstra/A* correctness, on a toy
graph with hand-computed optimal paths and on the real network (A*-vs-
Dijkstra cost equality on 100 random OD pairs, under freeflow and heavy
BPR).
"""

from __future__ import annotations

import random
import shutil
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
from bioroute.algorithms.classical import DijkstraRouter, AStarRouter

CONFIG_PATH = ROOT / "configs" / "area_central_blr.yaml"
CONGESTION_CONFIG_PATH = ROOT / "configs" / "congestion.yaml"


def _toy_graph() -> nx.MultiDiGraph:
    """Small graph with node coordinates (needed by A*'s heuristic).
    Nodes are colinear along a meridian, 0.001 deg latitude apart
    (~111 m each), so travel_time is physically consistent with
    length/speed on every edge -- required for A*'s great-circle/max-speed
    heuristic to actually be admissible (it estimates seconds from real
    distance and the graph's fastest edge speed, so a toy graph whose
    travel_time values are decoupled from real distance would make the
    heuristic overestimate and this test meaningless).

    Chain A-B-C-D-E at 36 kph (10 m/s): 4 * 111 m / 10 m/s = 44.4 s.
    Direct A-E "shortcut" at the same physical distance (colinear nodes)
    but a slower 18 kph (5 m/s): 444 m / 5 m/s = 88.8 s -- deliberately
    worse, so the chain is the known optimum.
    """
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

    seg_len_m = 111.19  # ~ 0.001 deg latitude
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


# --------------------------------------------------------------------------
# Toy graph: known optimal path / cost
# --------------------------------------------------------------------------


_CHAIN_COST = 4 * 111.19 / (36.0 * 1000.0 / 3600.0)
_DIRECT_COST = (4 * 111.19) / (18.0 * 1000.0 / 3600.0)


def test_dijkstra_toy_known_optimal():
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = DijkstraRouter()
    router.prepare(G, "A", "E", snap, seed=0)
    result = run_to_convergence(router, budget=1)
    assert result.path == ["A", "B", "C", "D", "E"]
    assert result.cost == pytest.approx(_CHAIN_COST)
    assert result.iterations == 1
    assert result.converged
    assert result.time_to_first_valid_path_iters == 1


def test_astar_toy_matches_dijkstra_cost():
    G = _toy_graph()
    snap = _toy_snapshot(G)

    d_router = DijkstraRouter()
    d_router.prepare(G, "A", "E", snap, seed=0)
    d_result = run_to_convergence(d_router, budget=1)

    a_router = AStarRouter()
    a_router.prepare(G, "A", "E", snap, seed=0)
    a_result = run_to_convergence(a_router, budget=1)

    assert a_result.cost == pytest.approx(d_result.cost)
    assert a_result.path == ["A", "B", "C", "D", "E"]


def test_one_shot_router_does_not_inflate_iterations():
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = DijkstraRouter()
    router.prepare(G, "A", "E", snap, seed=0)
    result = run_to_convergence(router, budget=10)
    # Budget was 10, but a one-shot router must converge (and stop) at
    # iteration 1 -- never fake extra iterations.
    assert result.iterations == 1


def test_disruption_recompute_avoids_severed_edge():
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = DijkstraRouter()
    router.prepare(G, "A", "E", snap, seed=0)
    run_to_convergence(router, budget=1)

    router.apply_disruption([("B", "C", 0)], warm=False)
    result = run_to_convergence(router, budget=1)
    assert result.path is not None
    for u, v in zip(result.path, result.path[1:]):
        assert (u, v) != ("B", "C")
    # Must fall back to the direct A-E edge now that B-C is severed.
    assert result.path == ["A", "E"]
    assert result.cost == pytest.approx(_DIRECT_COST)


# --------------------------------------------------------------------------
# Real network: A* cost == Dijkstra cost on 100 random OD pairs, under
# freeflow AND heavy BPR.
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
def od_pairs(real_graph):
    rng = random.Random(2024)
    nodes = sorted(real_graph.nodes())
    return [tuple(rng.sample(nodes, 2)) for _ in range(100)]


def _assert_astar_matches_dijkstra(G, snap, od_pairs):
    mismatches = []
    for s, t in od_pairs:
        d_router = DijkstraRouter()
        d_router.prepare(G, s, t, snap, seed=0)
        d_result = run_to_convergence(d_router, budget=1)

        a_router = AStarRouter()
        a_router.prepare(G, s, t, snap, seed=0)
        a_result = run_to_convergence(a_router, budget=1)

        if d_result.cost is None and a_result.cost is None:
            continue
        if d_result.cost is None or a_result.cost is None:
            mismatches.append((s, t, d_result.cost, a_result.cost))
            continue
        if abs(d_result.cost - a_result.cost) > 1e-6 * max(1.0, d_result.cost):
            mismatches.append((s, t, d_result.cost, a_result.cost))
    assert not mismatches, f"A* != Dijkstra on {len(mismatches)} pairs: {mismatches[:5]}"


def test_astar_matches_dijkstra_freeflow(real_graph, od_pairs):
    snap = FreeFlowProvider(real_graph).snapshot()
    _assert_astar_matches_dijkstra(real_graph, snap, od_pairs)


def test_astar_matches_dijkstra_heavy_bpr(real_graph, area_cfg, od_pairs, tmp_path):
    bpr = BPRProvider(
        real_graph, area_cfg, CONGESTION_CONFIG_PATH, cache_dir=tmp_path / "bpr_cache"
    )
    snap = bpr.snapshot("heavy", time_bin=1)
    _assert_astar_matches_dijkstra(real_graph, snap, od_pairs)
    shutil.rmtree(tmp_path / "bpr_cache", ignore_errors=True)
