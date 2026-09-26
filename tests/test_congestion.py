"""Phase 2a tests: TrafficSnapshot validation, BPR determinism and
congestion ordering, on both a toy hand-built graph and the real cached
network.
"""

from __future__ import annotations

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

CONFIG_PATH = ROOT / "configs" / "area_central_blr.yaml"
CONGESTION_CONFIG_PATH = ROOT / "configs" / "congestion.yaml"


# --------------------------------------------------------------------------
# Toy graph with hand-computed known shortest paths
# --------------------------------------------------------------------------


def _toy_graph() -> nx.MultiDiGraph:
    """A small MultiDiGraph with a known cheapest path A->D and a
    deliberate parallel edge between B and C (one slow, one fast) so
    cost_of_path's min-over-parallel-edges convention is exercised."""
    G = nx.MultiDiGraph()
    G.add_node("A", x=0.0, y=0.0)
    G.add_node("B", x=0.0, y=0.001)
    G.add_node("C", x=0.001, y=0.001)
    G.add_node("D", x=0.001, y=0.002)

    G.add_edge("A", "B", key=0, length=10.0, speed_kph=36.0, travel_time=1.0)
    G.add_edge("B", "C", key=0, length=10.0, speed_kph=36.0, travel_time=5.0)  # slow parallel
    G.add_edge("B", "C", key=1, length=10.0, speed_kph=36.0, travel_time=1.0)  # fast parallel
    G.add_edge("C", "D", key=0, length=10.0, speed_kph=36.0, travel_time=1.0)
    # A longer alternative A->D directly, deliberately worse than A-B-C-D
    G.add_edge("A", "D", key=0, length=100.0, speed_kph=36.0, travel_time=10.0)
    return G


def _toy_snapshot(G: nx.MultiDiGraph) -> TrafficSnapshot:
    tt = {(u, v, k): data["travel_time"] for u, v, k, data in G.edges(keys=True, data=True)}
    return TrafficSnapshot(level="freeflow", time_bin=0, source="freeflow", travel_time=tt)


def test_toy_dijkstra_known_optimal_cost():
    G = _toy_graph()
    snap = _toy_snapshot(G)
    w = snap.nx_weight()
    path = nx.dijkstra_path(G, "A", "D", weight=w)
    cost = snap.cost_of_path(G, path)
    # A-B (1.0) + B-C fast parallel (1.0) + C-D (1.0) = 3.0, cheaper than A-D (10.0)
    assert path == ["A", "B", "C", "D"]
    assert cost == pytest.approx(3.0)


def test_cost_of_path_uses_min_parallel_edge():
    G = _toy_graph()
    snap = _toy_snapshot(G)
    # Same node-list path, but cost must pick the fast (key=1) B->C edge,
    # not the slow (key=0) one, since the convention is min-cost parallel.
    cost = snap.cost_of_path(G, ["A", "B", "C", "D"])
    assert cost == pytest.approx(3.0)


def test_cost_of_path_agrees_with_dijkstra_reported_cost():
    G = _toy_graph()
    snap = _toy_snapshot(G)
    w = snap.nx_weight()
    path = nx.dijkstra_path(G, "A", "D", weight=w)
    dijkstra_cost = nx.dijkstra_path_length(G, "A", "D", weight=w)
    assert snap.cost_of_path(G, path) == pytest.approx(dijkstra_cost)


# --------------------------------------------------------------------------
# TrafficSnapshot validation
# --------------------------------------------------------------------------


def test_snapshot_rejects_non_positive_travel_time():
    with pytest.raises(ValueError):
        TrafficSnapshot(
            level="freeflow", time_bin=0, source="freeflow",
            travel_time={(1, 2, 0): 0.0},
        )
    with pytest.raises(ValueError):
        TrafficSnapshot(
            level="freeflow", time_bin=0, source="freeflow",
            travel_time={(1, 2, 0): -5.0},
        )


def test_snapshot_rejects_non_finite_travel_time():
    with pytest.raises(ValueError):
        TrafficSnapshot(
            level="freeflow", time_bin=0, source="freeflow",
            travel_time={(1, 2, 0): float("inf")},
        )
    with pytest.raises(ValueError):
        TrafficSnapshot(
            level="freeflow", time_bin=0, source="freeflow",
            travel_time={(1, 2, 0): float("nan")},
        )


def test_snapshot_rejects_empty_travel_time():
    with pytest.raises(ValueError):
        TrafficSnapshot(level="freeflow", time_bin=0, source="freeflow", travel_time={})


# --------------------------------------------------------------------------
# BPR on the real cached network
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


@pytest.fixture()
def bpr_cache_dir(tmp_path):
    d = tmp_path / "bpr_cache"
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_bpr_reproducible_same_seed(real_graph, area_cfg, bpr_cache_dir):
    p1 = BPRProvider(real_graph, area_cfg, CONGESTION_CONFIG_PATH, cache_dir=bpr_cache_dir / "a")
    p2 = BPRProvider(real_graph, area_cfg, CONGESTION_CONFIG_PATH, cache_dir=bpr_cache_dir / "b")
    s1 = p1.snapshot("heavy", time_bin=1)
    s2 = p2.snapshot("heavy", time_bin=1)
    assert s1.travel_time.keys() == s2.travel_time.keys()
    for k in s1.travel_time:
        assert s1.travel_time[k] == pytest.approx(s2.travel_time[k])


def test_bpr_light_gte_freeflow_every_edge(real_graph, area_cfg, bpr_cache_dir):
    """light >= freeflow elementwise IS guaranteed, unlike heavy vs. light:
    BPR travel time is t0 * (1 + alpha * (v/c)^beta) with v >= 0, alpha,
    beta > 0, so it is always >= t0 (free-flow) on every edge regardless of
    how the assignment converges. Any edge violating this would be a real
    bug in the BPR formula or capacity table, not route-switching."""
    bpr = BPRProvider(real_graph, area_cfg, CONGESTION_CONFIG_PATH, cache_dir=bpr_cache_dir)
    ff = FreeFlowProvider(real_graph).snapshot()
    light = bpr.snapshot("light", time_bin=1)

    assert light.travel_time.keys() == ff.travel_time.keys()
    for e in ff.travel_time:
        assert light.travel_time[e] >= ff.travel_time[e] - 1e-9


def test_bpr_aggregate_monotonic_freeflow_light_heavy(real_graph, area_cfg, bpr_cache_dir):
    """Light and heavy are independent per-level equilibria (see
    BPRProvider._assign), so elementwise heavy >= light does not hold on
    every edge -- that would require faking route-switching away (see
    module docstring). What DOES hold, and is asserted here, is aggregate
    monotonicity: mean edge travel time, and mean cost over a fixed sample
    of OD shortest paths, both strictly increase freeflow -> light ->
    heavy, because more total demand strictly increases total network-wide
    delay under BPR even when a few individual edges get relief from
    rerouting."""
    import random

    bpr = BPRProvider(real_graph, area_cfg, CONGESTION_CONFIG_PATH, cache_dir=bpr_cache_dir)
    ff = FreeFlowProvider(real_graph).snapshot()
    light = bpr.snapshot("light", time_bin=1)
    heavy = bpr.snapshot("heavy", time_bin=1)

    def mean_edge_tt(snap: TrafficSnapshot) -> float:
        return sum(snap.travel_time.values()) / len(snap.travel_time)

    assert mean_edge_tt(ff) < mean_edge_tt(light) < mean_edge_tt(heavy)

    rng = random.Random(7)
    nodes = sorted(real_graph.nodes())
    pairs = [tuple(rng.sample(nodes, 2)) for _ in range(100)]

    def mean_od_cost(snap: TrafficSnapshot) -> float:
        w = snap.nx_weight()
        costs = []
        for s, t in pairs:
            try:
                path = nx.shortest_path(real_graph, s, t, weight=w)
            except nx.NetworkXNoPath:
                continue
            costs.append(snap.cost_of_path(real_graph, path))
        return sum(costs) / len(costs)

    assert mean_od_cost(ff) < mean_od_cost(light) < mean_od_cost(heavy)


def test_bpr_heavy_lt_light_fraction_bounded(real_graph, area_cfg, bpr_cache_dir):
    """Independent per-level assignment means some edges genuinely see
    heavy < light (drivers reroute away from them once heavy demand makes
    other routes relatively better). That is real traffic behaviour, not a
    bug -- but it should stay a small minority of edges, not swamp the
    network. Measured fraction on the real cached graph (light=68,
    heavy=190 trips/pair, time_bin=1): ~0.30% of edges. Bound set at 1%,
    a defensible round number comfortably above the measured value, so this
    test catches a real regression (e.g. a demand/capacity misconfiguration
    causing widespread reversal) without being a tautology."""
    bpr = BPRProvider(real_graph, area_cfg, CONGESTION_CONFIG_PATH, cache_dir=bpr_cache_dir)
    light = bpr.snapshot("light", time_bin=1)
    heavy = bpr.snapshot("heavy", time_bin=1)

    n_edges = len(light.travel_time)
    n_reversed = sum(
        1 for e in light.travel_time if heavy.travel_time[e] < light.travel_time[e] - 1e-9
    )
    frac = n_reversed / n_edges
    assert frac < 0.01, f"fraction of edges with heavy < light = {frac:.4f}, expected < 1%"


def test_bpr_congestion_ratio_bands(real_graph, area_cfg, bpr_cache_dir):
    """Mean OD-path travel time under light/heavy BPR vs. free-flow, over
    100 random OD pairs, lands in the target bands (light ~1.1-1.3x,
    heavy ~1.8-2.5x). See Phase 2a report for the demand scale chosen."""
    import random

    bpr = BPRProvider(real_graph, area_cfg, CONGESTION_CONFIG_PATH, cache_dir=bpr_cache_dir)
    ff_snap = FreeFlowProvider(real_graph).snapshot()

    rng = random.Random(123)
    nodes = sorted(real_graph.nodes())
    pairs = [tuple(rng.sample(nodes, 2)) for _ in range(100)]

    def mean_cost(snap: TrafficSnapshot) -> float:
        w = snap.nx_weight()
        costs = []
        for s, t in pairs:
            try:
                path = nx.shortest_path(real_graph, s, t, weight=w)
            except nx.NetworkXNoPath:
                continue
            costs.append(snap.cost_of_path(real_graph, path))
        return sum(costs) / len(costs)

    ff_mean = mean_cost(ff_snap)
    light_ratio = mean_cost(bpr.snapshot("light", time_bin=1)) / ff_mean
    heavy_ratio = mean_cost(bpr.snapshot("heavy", time_bin=1)) / ff_mean

    assert 1.1 <= light_ratio <= 1.3, f"light ratio {light_ratio} outside target band"
    assert 1.8 <= heavy_ratio <= 2.5, f"heavy ratio {heavy_ratio} outside target band"


def test_bpr_lane_stats_reported(real_graph, area_cfg, bpr_cache_dir):
    bpr = BPRProvider(real_graph, area_cfg, CONGESTION_CONFIG_PATH, cache_dir=bpr_cache_dir)
    tagged, default = bpr.lane_stats()
    assert tagged + default == real_graph.number_of_edges()
    assert tagged > 0
    assert default > 0
