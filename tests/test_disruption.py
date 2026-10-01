"""Phase 7 tests: disruption scenarios S0-S6 and their application.

Structure mirrors the other phases' tests: hand-constructible toy graphs
carry the correctness assertions (every expected answer below is derivable
by eye from the graph built in `_toy_graph` / `_cut_graph`), and a small
number of real-network checks confirm the scenarios behave on the actual
2,856-node Bengaluru graph. The expensive rejection-rate survey is marked
slow.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import networkx as nx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bioroute.algorithms.base import run_to_convergence
from bioroute.algorithms.classical import DijkstraRouter
from bioroute.config import load_area_config
from bioroute.congestion.base import FreeFlowProvider
from bioroute.congestion.snapshot import TrafficSnapshot
from bioroute.disruption.apply import (
    apply_disruption,
    connectivity_preserved,
    path_violations,
    severed_pairs,
)
from bioroute.disruption.scenarios import (
    SCENARIO_IDS,
    _closure_edges,
    DisruptionConfig,
    DisruptionInfeasible,
    generate_disruption,
    optimal_path,
    pair_betweenness,
    path_pairs,
    way_index,
)
from bioroute.network.graph import cache_path, load_cached_graph

CONFIG_PATH = ROOT / "configs" / "area_central_blr.yaml"

STOCHASTIC = ("S1", "S3", "S4", "S5", "S6")
# S2 is deliberately excluded from "differs across seeds": it is the
# *worst-case* scenario, defined as the highest-betweenness path edge. Its
# only seed dependence is which sources Brandes samples, and the top-ranked
# edge is normally stable under that -- picking a different edge per seed
# would mean it was not selecting the worst case.


# ==========================================================================
# Toy graphs
# ==========================================================================


def _bidi(G: nx.MultiDiGraph, u, v, tt: float, osmid: list[int]) -> None:
    """Add a two-way road (both directed edges share the OSM way ids, as in
    the real OSMnx graph)."""
    for a, b in ((u, v), (v, u)):
        G.add_edge(a, b, key=0, length=tt * 10.0, travel_time=tt, osmid=list(osmid))


def _toy_graph() -> nx.MultiDiGraph:
    """Main corridor 0-1-2-3-4-5-6 (OSM way 100, 1 s per edge) plus a slow
    bypass 0-20-21-22-6 (OSM way 200, 5 s per edge).

    Optimal 0 -> 6 is the main corridor, 6 edges, cost 6.0. The bypass costs
    20.0, so every scenario that closes a main-corridor edge still leaves
    s-t connected -- no rejections here by construction.

    Node 3 additionally carries a parallel pair of edges to 4 (key 1, way
    101) so parallel-edge handling is exercised: closing (3, 4) must take
    BOTH keys, or the closure is a no-op.

    Way 101 ALSO runs through the disjoint stretch 50-51 (hung off node 5 by
    way 300). That is what makes S5's multi-way decision testable: the pair
    (3, 4) belongs to ways 100 and 101, so closing "all ways touching the
    seed edge" would reach 50-51, while closing ONE chosen way does not.
    """
    G = nx.MultiDiGraph()
    for i in range(6):
        _bidi(G, i, i + 1, 1.0, [100])
    G.add_edge(3, 4, key=1, length=10.0, travel_time=1.5, osmid=[101])
    G.add_edge(4, 3, key=1, length=10.0, travel_time=1.5, osmid=[101])
    _bidi(G, 0, 20, 5.0, [200])
    _bidi(G, 20, 21, 5.0, [200])
    _bidi(G, 21, 22, 5.0, [200])
    _bidi(G, 22, 6, 5.0, [200])
    _bidi(G, 5, 50, 3.0, [300])
    _bidi(G, 50, 51, 3.0, [101])
    return G


def _cut_graph() -> nx.MultiDiGraph:
    """Path s=0 -> 1 -> 2 -> t=3 in which the ONLY redundant link is the
    middle one.

    0-1 and 2-3 are bridges: closing either disconnects 0 from 3. 1-2 has a
    parallel detour 1-30-2. So:
      * S1 must reject any bridge it draws and settle on (1, 2).
      * S3 (3 path edges) has exactly one possible choice, which contains
        both bridges -- it must FAIL, with a rejection recorded, and return
        an empty edge set.
      * S4 (5 path edges) cannot even be attempted on a 3-edge path.
    """
    G = nx.MultiDiGraph()
    _bidi(G, 0, 1, 1.0, [10])
    _bidi(G, 1, 2, 1.0, [11])
    _bidi(G, 2, 3, 1.0, [12])
    _bidi(G, 1, 30, 4.0, [13])
    _bidi(G, 30, 2, 4.0, [13])
    return G


def _snap(G: nx.MultiDiGraph) -> TrafficSnapshot:
    return TrafficSnapshot(
        level="freeflow",
        time_bin=0,
        source="freeflow",
        travel_time={(u, v, k): d["travel_time"] for u, v, k, d in G.edges(keys=True, data=True)},
    )


@pytest.fixture()
def toy():
    G = _toy_graph()
    return G, _snap(G)


@pytest.fixture()
def cut():
    G = _cut_graph()
    return G, _snap(G)


# ==========================================================================
# The optimal path the scenarios are defined against
# ==========================================================================


def test_toy_optimal_path_is_the_main_corridor(toy):
    G, snap = toy
    assert optimal_path(G, 0, 6, snap) == [0, 1, 2, 3, 4, 5, 6]


# ==========================================================================
# S0 control
# ==========================================================================


def test_s0_removes_nothing(toy):
    G, snap = toy
    d = generate_disruption(G, 0, 6, snap, "S0", seed=1)
    assert d.ok
    assert d.removed_edges == []
    assert d.rejections == 0
    assert d.scenario_id == "S0"


def test_s0_leaves_the_graph_identical(toy):
    G, snap = toy
    d = generate_disruption(G, 0, 6, snap, "S0", seed=1)
    G2 = apply_disruption(G, d.removed_edges)
    assert set(G2.edges(keys=True)) == set(G.edges(keys=True))


# ==========================================================================
# Reproducibility and seed sensitivity
# ==========================================================================


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_reproducible_under_a_fixed_seed(toy, scenario_id):
    G, snap = toy
    a = generate_disruption(G, 0, 6, snap, scenario_id, seed=13)
    b = generate_disruption(G, 0, 6, snap, scenario_id, seed=13)
    assert a.removed_edges == b.removed_edges
    assert a.rejections == b.rejections
    assert a.ok == b.ok


@pytest.mark.parametrize("scenario_id", STOCHASTIC)
def test_differs_across_seeds(toy, scenario_id):
    G, snap = toy
    seen = {
        tuple(generate_disruption(G, 0, 6, snap, scenario_id, seed=s).removed_edges)
        for s in range(12)
    }
    assert len(seen) > 1, f"{scenario_id} produced one edge set across 12 seeds"


def test_different_scenarios_do_not_share_an_rng_stream(toy):
    """S1 and S3 on the same seed must not be correlated -- they would be if
    both simply drew from ``random.Random(seed)``."""
    G, snap = toy
    identical = 0
    for s in range(12):
        s1 = generate_disruption(G, 0, 6, snap, "S1", seed=s)
        s3 = generate_disruption(G, 0, 6, snap, "S3", seed=s)
        first_s3_pair = tuple(s3.meta["path_pairs_closed"][0])
        if tuple(s1.meta["path_pairs_closed"][0]) == first_s3_pair:
            identical += 1
    assert identical < 12


def test_seed_derivation_is_not_process_dependent(toy):
    """The RNG seed must come from a stable hash, not builtin ``hash()``
    (which is randomized per process by PYTHONHASHSEED)."""
    from bioroute.disruption.scenarios import _derive_seed

    assert _derive_seed(7, "S1", 101, 202) == _derive_seed(7, "S1", 101, 202)
    assert _derive_seed(7, "S1", 101, 202) != _derive_seed(7, "S3", 101, 202)
    assert _derive_seed(7, "S1", 101, 202) != _derive_seed(8, "S1", 101, 202)


# ==========================================================================
# Selection is relative to the optimal path
# ==========================================================================


@pytest.mark.parametrize("scenario_id", ["S1", "S2", "S3", "S4"])
def test_path_scenarios_close_only_path_edges_or_their_reverses(toy, scenario_id):
    """The exact guaranteed relationship documented in scenarios.py: with
    ``both_directions=True`` a removed edge is on the path OR is the reverse
    of a path edge, and the pairs closed are a subset of the path's pairs."""
    G, snap = toy
    path = optimal_path(G, 0, 6, snap)
    pairs = set(path_pairs(path))
    d = generate_disruption(G, 0, 6, snap, scenario_id, seed=5, path=path)
    assert d.ok
    closed = {tuple(p) for p in d.meta["path_pairs_closed"]}
    assert closed <= pairs
    for u, v, _ in d.removed_edges:
        assert (u, v) in pairs or (v, u) in pairs


@pytest.mark.parametrize("scenario_id", ["S1", "S2", "S3", "S4"])
def test_one_way_mode_closes_only_forward_path_edges(toy, scenario_id):
    G, snap = toy
    path = optimal_path(G, 0, 6, snap)
    pairs = set(path_pairs(path))
    cfg = DisruptionConfig(both_directions=False)
    d = generate_disruption(G, 0, 6, snap, scenario_id, seed=5, path=path, config=cfg)
    assert d.ok
    for u, v, _ in d.removed_edges:
        assert (u, v) in pairs


def test_scenario_edge_counts(toy):
    G, snap = toy
    for scenario_id, n in (("S1", 1), ("S3", 3), ("S4", 5)):
        d = generate_disruption(G, 0, 6, snap, scenario_id, seed=5)
        assert d.ok
        assert len(d.meta["path_pairs_closed"]) == n


def test_closure_takes_every_parallel_edge(toy):
    """Node pair (3, 4) has two parallel edges (keys 0 and 1). Closing the
    road must take both -- otherwise the 'closure' leaves a lane open."""
    G, snap = toy
    cfg = DisruptionConfig(both_directions=False)
    # Force the (3, 4) pair by asking for all 6 path pairs via S4+S3 union;
    # simpler: assert the expansion directly through a scenario that happens
    # to include it, by scanning seeds.
    for seed in range(40):
        d = generate_disruption(G, 0, 6, snap, "S1", seed=seed, config=cfg)
        if d.meta["path_pairs_closed"] == [[3, 4]]:
            assert sorted(d.removed_edges) == [(3, 4, 0), (3, 4, 1)]
            return
    pytest.fail("S1 never selected the parallel-edge pair (3, 4) in 40 seeds")


# ==========================================================================
# S2: betweenness
# ==========================================================================


def test_s2_picks_the_highest_betweenness_path_edge(toy):
    G, snap = toy
    path = optimal_path(G, 0, 6, snap)
    scores = pair_betweenness(G, snap, k=None, seed=0)
    best = max(path_pairs(path), key=lambda p: scores.get(p, 0.0))
    d = generate_disruption(G, 0, 6, snap, "S2", seed=0, path=path)
    assert d.ok
    assert tuple(d.meta["path_pairs_closed"][0]) == best


def test_s2_records_k_for_reproducibility(toy):
    G, snap = toy
    cfg = DisruptionConfig(betweenness_k=4)
    d = generate_disruption(G, 0, 6, snap, "S2", seed=3, config=cfg)
    assert d.meta["betweenness_k"] == 4
    assert d.meta["betweenness_seed"] == 3
    assert "betweenness" in d.meta


def test_betweenness_cache_is_keyed_on_snapshot_values(toy):
    """A different snapshot must not be served the cached ranking."""
    G, snap = toy
    a = pair_betweenness(G, snap, k=None, seed=0)
    slow = TrafficSnapshot(
        level="freeflow",
        time_bin=0,
        source="freeflow",
        travel_time={e: (50.0 if e[0] == 0 else v) for e, v in snap.travel_time.items()},
    )
    b = pair_betweenness(G, slow, k=None, seed=0)
    assert a is not b


# ==========================================================================
# S5: corridor closure
# ==========================================================================


def test_s5_closes_every_edge_of_the_chosen_way(toy):
    G, snap = toy
    d = generate_disruption(G, 0, 6, snap, "S5", seed=2)
    assert d.ok
    way = d.meta["way_id"]
    index = way_index(G)
    removed = set(d.removed_edges)
    for edge in index[way]:
        assert edge in removed, f"edge {edge} of way {way} was left open"
    assert d.meta["n_corridor_edges"] == len(index[way])


def test_s5_chosen_way_is_carried_by_a_path_edge(toy):
    G, snap = toy
    path = optimal_path(G, 0, 6, snap)
    pairs = set(path_pairs(path))
    d = generate_disruption(G, 0, 6, snap, "S5", seed=2)
    way = d.meta["way_id"]
    seed_pair = tuple(d.meta["seed_pair"])
    assert seed_pair in pairs
    ways_on_pair = set()
    for a, b in (seed_pair, (seed_pair[1], seed_pair[0])):
        for attrs in (G.get_edge_data(a, b) or {}).values():
            ways_on_pair.update(attrs["osmid"])
    assert way in ways_on_pair


def test_s5_removes_exactly_the_chosen_ways_corridor(toy):
    """The precise S5 contract: the removal set is EXACTLY one OSM way's
    edges, expanded by the documented parallel-edge and reverse-direction
    rules -- and nothing else.

    This is the assertion that rules out the union-of-all-ways reading: pair
    (3, 4) carries ways 100 and 101, and way 101 also covers the disjoint
    stretch 50-51, so a union would drag 50-51 into every closure seeded on
    (3, 4). The expected set below never contains it unless 101 is itself
    the chosen way.

    Note the asymmetry, which is correct and deliberate: choosing way 100
    DOES take edge (3, 4, 1) (way 101) with it, because closing a node pair
    closes every parallel lane of that pair -- a barricade across 3->4 stops
    the co-located service road however OSM tagged it. That is the
    parallel-edge rule, not way-union leakage, and the exact-equality check
    distinguishes the two.
    """
    G, snap = toy
    index = way_index(G)
    far = {(50, 51, 0), (51, 50, 0)}
    ways_seen = set()
    for seed in range(20):
        d = generate_disruption(G, 0, 6, snap, "S5", seed=seed)
        assert d.ok
        way = d.meta["way_id"]
        ways_seen.add(way)
        corridor_pairs = {(u, v) for u, v, _ in index[way]}
        expected = set(_closure_edges(G, corridor_pairs, DisruptionConfig()))
        assert set(d.removed_edges) == expected, f"seed {seed}, way {way}"
        if way != 101:
            assert not (far & set(d.removed_edges)), (
                f"way {way}'s closure leaked into way 101's disjoint stretch"
            )
    assert len(ways_seen) > 1, "S5 never varied its chosen way across 20 seeds"


# ==========================================================================
# S6: intersection closure
# ==========================================================================


def test_s6_closes_every_edge_incident_to_the_chosen_node(toy):
    G, snap = toy
    d = generate_disruption(G, 0, 6, snap, "S6", seed=4)
    assert d.ok
    node = d.meta["node"]
    expected = {(u, v, k) for u, v, k in G.in_edges(node, keys=True)}
    expected |= {(u, v, k) for u, v, k in G.out_edges(node, keys=True)}
    assert set(d.removed_edges) == expected
    G2 = apply_disruption(G, d.removed_edges)
    assert G2.degree(node) == 0
    assert node in G2  # isolated, not deleted


def test_s6_never_closes_source_or_target(toy):
    G, snap = toy
    for seed in range(20):
        d = generate_disruption(G, 0, 6, snap, "S6", seed=seed)
        assert d.ok
        assert d.meta["node"] not in (0, 6)


def test_s6_fails_cleanly_on_a_single_edge_path(toy):
    G, snap = toy
    d = generate_disruption(G, 0, 1, snap, "S6", seed=1)
    assert not d.ok
    assert d.removed_edges == []
    assert "interior" in d.reason


# ==========================================================================
# Connectivity is preserved, rejections are surfaced
# ==========================================================================


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_connectivity_preserved_on_toy(toy, scenario_id):
    G, snap = toy
    d = generate_disruption(G, 0, 6, snap, scenario_id, seed=9)
    assert d.ok
    G2 = apply_disruption(G, d.removed_edges)
    assert connectivity_preserved(G2, 0, 6)


def test_s1_rejects_cut_edges_and_reports_the_count(cut):
    """0-1 and 2-3 are bridges; 1-2 is not. S1 must always end on (1, 2),
    and at least one seed must have paid a rejection getting there."""
    G, snap = cut
    seen_rejections = 0
    for seed in range(12):
        d = generate_disruption(G, 0, 3, snap, "S1", seed=seed)
        assert d.ok
        assert d.meta["path_pairs_closed"] == [[1, 2]]
        assert connectivity_preserved(apply_disruption(G, d.removed_edges), 0, 3)
        seen_rejections += d.rejections
    assert seen_rejections > 0, "rejections were never surfaced on a graph full of bridges"


def test_rejections_are_not_swallowed_on_failure(cut):
    """S3 on this graph has exactly one possible selection and it
    disconnects. The result must be ok=False with an EMPTY edge set, a
    stated reason, and the rejection still counted."""
    G, snap = cut
    d = generate_disruption(G, 0, 3, snap, "S3", seed=1)
    assert not d.ok
    assert d.removed_edges == []
    assert d.rejections >= 1
    assert d.attempts >= 1
    assert "connectivity" in d.reason
    assert d.meta["rejections"] == d.rejections


def test_infeasible_never_returns_a_disconnecting_set(cut):
    """The failure mode that would silently poison Phase 8: returning the
    rejected set anyway. Assert the empty set is genuinely connectivity-
    safe."""
    G, snap = cut
    d = generate_disruption(G, 0, 3, snap, "S3", seed=1)
    assert connectivity_preserved(apply_disruption(G, d.removed_edges), 0, 3)


def test_path_too_short_for_scenario(cut):
    G, snap = cut
    d = generate_disruption(G, 0, 3, snap, "S4", seed=1)
    assert not d.ok
    assert d.removed_edges == []
    assert "needs 5" in d.reason


def test_strict_mode_raises(cut):
    G, snap = cut
    cfg = DisruptionConfig(strict=True)
    with pytest.raises(DisruptionInfeasible):
        generate_disruption(G, 0, 3, snap, "S4", seed=1, config=cfg)


def test_unreachable_od_pair_fails_cleanly(toy):
    G, snap = toy
    G.add_node(999, y=0.0, x=0.0)
    G.add_edge(999, 0, key=0, length=1.0, travel_time=1.0, osmid=[999])
    snap2 = _snap(G)
    d = generate_disruption(G, 0, 999, snap2, "S1", seed=1)
    assert not d.ok
    assert d.removed_edges == []
    assert "no optimal path" in d.reason


def test_unknown_scenario_id_raises(toy):
    G, snap = toy
    with pytest.raises(ValueError):
        generate_disruption(G, 0, 6, snap, "S9", seed=1)


# ==========================================================================
# apply.py: non-mutation is the load-bearing property
# ==========================================================================


@pytest.mark.parametrize("copy", [False, True])
def test_apply_disruption_does_not_mutate_the_input_graph(toy, copy):
    G, snap = toy
    before_edges = set(G.edges(keys=True))
    before_nodes = set(G.nodes())
    removed = [(1, 2, 0), (2, 1, 0), (3, 4, 0), (3, 4, 1)]
    G2 = apply_disruption(G, removed, copy=copy)
    assert G2.number_of_edges() == len(before_edges) - len(removed)
    for edge in removed:
        assert not G2.has_edge(*edge)
        # THE assertion: the original still has every removed edge.
        assert G.has_edge(*edge)
    assert set(G.edges(keys=True)) == before_edges
    assert set(G.nodes()) == before_nodes


def test_apply_disruption_repeated_use_leaves_base_graph_pristine(toy):
    """Phase 8 reuses one base graph across thousands of runs."""
    G, snap = toy
    before = set(G.edges(keys=True))
    for seed in range(10):
        for scenario_id in SCENARIO_IDS:
            d = generate_disruption(G, 0, 6, snap, scenario_id, seed=seed)
            apply_disruption(G, d.removed_edges)
    assert set(G.edges(keys=True)) == before


def test_mutating_a_copy_does_not_touch_the_base(toy):
    G, snap = toy
    G2 = apply_disruption(G, [(1, 2, 0)], copy=True)
    G2.remove_edge(0, 1, 0)
    assert G.has_edge(0, 1, 0)


def test_apply_disruption_ignores_absent_edges(toy):
    G, snap = toy
    G2 = apply_disruption(G, [(1, 2, 0), (77, 88, 0)])
    assert not G2.has_edge(1, 2, 0)
    assert G2.number_of_edges() == G.number_of_edges() - 1


def test_severed_pairs_requires_all_parallel_edges(toy):
    G, snap = toy
    assert severed_pairs(G, [(3, 4, 0)]) == set()
    assert severed_pairs(G, [(3, 4, 0), (3, 4, 1)]) == {(3, 4)}


def test_path_violations_detects_a_severed_hop(toy):
    G, snap = toy
    removed = [(1, 2, 0)]
    assert path_violations(G, [0, 1, 2, 3], removed) == [(1, 2)]
    assert path_violations(G, [0, 20, 21, 22, 6], removed) == []
    assert path_violations(G, None, removed) == []


def test_connectivity_preserved_directed_not_weak():
    """A surviving wrong-way link must not count as connectivity."""
    G = nx.MultiDiGraph()
    G.add_edge(0, 1, key=0, travel_time=1.0, length=1.0, osmid=[1])
    G.add_edge(1, 0, key=0, travel_time=1.0, length=1.0, osmid=[1])
    G2 = apply_disruption(G, [(0, 1, 0)])
    assert not connectivity_preserved(G2, 0, 1)
    assert connectivity_preserved(G2, 1, 0)


def test_post_disruption_route_contains_zero_severed_edges(toy):
    G, snap = toy
    for scenario_id in ("S1", "S2", "S3", "S4", "S5", "S6"):
        d = generate_disruption(G, 0, 6, snap, scenario_id, seed=6)
        assert d.ok
        G2 = apply_disruption(G, d.removed_edges)
        router = DijkstraRouter()
        router.prepare(G2, 0, 6, snap, seed=0)
        result = run_to_convergence(router, budget=1)
        assert result.path is not None
        assert path_violations(G, result.path, d.removed_edges) == []


# ==========================================================================
# Real network
# ==========================================================================


@pytest.fixture(scope="module")
def area_cfg():
    return load_area_config(CONFIG_PATH)


@pytest.fixture(scope="module")
def real_graph(area_cfg):
    path = cache_path(area_cfg, ROOT / "data" / "network")
    if not path.exists():
        pytest.skip(f"Cached graph not found at {path}; run scripts/01_build_network.py first.")
    return load_cached_graph(path)


@pytest.fixture(scope="module")
def real_snapshot(real_graph):
    return FreeFlowProvider(real_graph).snapshot()


def _real_od_pairs(G, snap, n, min_edges=8, seed=4242):
    rng = random.Random(seed)
    nodes = sorted(G.nodes())
    out = []
    while len(out) < n:
        s, t = rng.sample(nodes, 2)
        p = optimal_path(G, s, t, snap)
        if p is not None and len(p) - 1 >= min_edges:
            out.append((s, t, p))
    return out


@pytest.fixture(scope="module")
def real_od(real_graph, real_snapshot):
    return _real_od_pairs(real_graph, real_snapshot, 10)


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_real_graph_scenarios_are_valid_and_reproducible(
    real_graph, real_snapshot, real_od, scenario_id
):
    G, snap = real_graph, real_snapshot
    for s, t, p in real_od:
        d = generate_disruption(G, s, t, snap, scenario_id, seed=11, path=p)
        again = generate_disruption(G, s, t, snap, scenario_id, seed=11, path=p)
        assert d.removed_edges == again.removed_edges
        assert d.ok, f"{scenario_id} failed on ({s}, {t}): {d.reason}"
        assert d.rejections >= 0
        G2 = apply_disruption(G, d.removed_edges)
        assert connectivity_preserved(G2, s, t)
        router = DijkstraRouter()
        router.prepare(G2, s, t, snap, seed=0)
        result = run_to_convergence(router, budget=1)
        assert result.path is not None
        assert path_violations(G, result.path, d.removed_edges) == []


def test_real_graph_disruptions_actually_change_the_route(real_graph, real_snapshot, real_od):
    """The design constraint the whole phase rests on: a scenario that left
    the optimal path intact would produce a null result."""
    G, snap = real_graph, real_snapshot
    unchanged = 0
    total = 0
    for scenario_id in ("S1", "S2", "S3", "S4", "S5", "S6"):
        for s, t, p in real_od:
            d = generate_disruption(G, s, t, snap, scenario_id, seed=11, path=p)
            if not d.ok:
                continue
            total += 1
            G2 = apply_disruption(G, d.removed_edges)
            new_path = optimal_path(G2, s, t, snap)
            if new_path == list(p):
                unchanged += 1
    assert total > 0
    assert unchanged == 0, f"{unchanged}/{total} disruptions left the optimal path unchanged"


def test_real_graph_base_is_never_mutated(real_graph, real_snapshot, real_od):
    G, snap = real_graph, real_snapshot
    n_edges = G.number_of_edges()
    n_nodes = G.number_of_nodes()
    for scenario_id in SCENARIO_IDS:
        for s, t, p in real_od:
            d = generate_disruption(G, s, t, snap, scenario_id, seed=11, path=p)
            apply_disruption(G, d.removed_edges)
    assert G.number_of_edges() == n_edges
    assert G.number_of_nodes() == n_nodes


@pytest.mark.slow
def test_real_graph_rejection_rate_survey(real_graph, real_snapshot):
    """The fragility measurement itself: 60 OD pairs x 7 scenarios, with
    rejection counts. Slow because it is a measurement, not a check.

    MEASURED PHASE 7 BASELINE (recorded here so the numbers in the paper
    have a reproducible origin). 100 random OD pairs x 3 seeds = 300 runs
    per scenario, on the sumo_speeds baseline graph (2,856 nodes / 7,015
    edges), median optimal path 42 edges. "Rejection rate" = candidate edge
    sets discarded for disconnecting s-t, as a share of candidates tried.

        scenario   freeflow            BPR heavy
        S0           0.0% (no edges)     0.0%
        S1           2.6%                3.2%
        S2           1.0%                5.4%
        S3          14.5%               15.3%
        S4          31.7%               31.2%
        S5           7.4%                6.8%
        S6           4.5%                5.7%

    Zero runs were infeasible in all 4,200 generations: every scenario found
    a connectivity-preserving closure within its attempt budget.

    Reading: the network is NOT fragile to single closures -- only ~1 path
    edge in 40 is a cut edge for its own OD pair (S1 2.6%), which is what a
    dense irregular grid should look like. Fragility is SUPER-ADDITIVE in
    the number of simultaneous closures: 1 -> 3 -> 5 edges takes the
    rejection rate 2.6% -> 14.5% -> 31.7%, far faster than 3x and 5x, so by
    S4 roughly a third of random 5-edge path closures sever the OD pair
    outright. S5's corridor closure removes a median 4 and up to 60 edges
    yet is rejected only 7.4% of the time -- closing a whole named road is
    much *safer* for connectivity than closing 5 scattered path edges,
    because a corridor's edges are consecutive and one detour routes round
    all of them. Congestion barely moves any of this (the two columns agree
    within ~1pp except S2), which is expected: connectivity is topological,
    and the snapshot only changes WHICH path is optimal, not what is
    reachable. S2 is the exception precisely because its selection is
    weight-dependent.
    """
    G, snap = real_graph, real_snapshot
    od = _real_od_pairs(G, snap, 60)
    for scenario_id in SCENARIO_IDS:
        rejections = 0
        attempts = 0
        failures = 0
        for s, t, p in od:
            d = generate_disruption(G, s, t, snap, scenario_id, seed=7, path=p)
            rejections += d.rejections
            attempts += d.attempts
            failures += 0 if d.ok else 1
            if d.ok:
                assert connectivity_preserved(apply_disruption(G, d.removed_edges), s, t)
        rate = 100.0 * rejections / attempts if attempts else 0.0
        print(
            f"{scenario_id}: {rejections} rejections / {attempts} attempts "
            f"({rate:.1f}%), {failures} infeasible of {len(od)}"
        )
        assert failures == 0, f"{scenario_id} was infeasible on {failures} of {len(od)} pairs"
