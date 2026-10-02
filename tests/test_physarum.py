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
import warnings
from pathlib import Path

import networkx as nx
import numpy as np
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
# PHASE 8B: provenance of the REPORTED path.
#
# `best_cost` is a min over every iteration AND every extraction method, so
# the path a run reports can come from a fallback -- and `full_graph_fallback`
# IS plain Dijkstra on the full graph, which makes a reported stretch of 1.0
# ambiguous unless the producing method is recorded. Phase 8a measured a
# fallback setting `best_cost` on 3 of 6 undisrupted real pairs (while the
# flux walk succeeded on 34-56 iterations), and measured the LAST iteration's
# method disagreeing with the winning one on 1 of 6.
# --------------------------------------------------------------------------


def _oneway_trap_graph() -> nx.MultiDiGraph:
    """A-B-C-E (slow, 3 hops) plus a fast A-D spur whose D->E leg exists ONLY
    as the one-way E->D.

    The undirected model therefore sees a cycle and pushes current down the
    cheap A-D branch, so the greedy max-flux walk prefers D -- and then dead
    ends, because no directed D->E edge exists and A is already visited. This
    is the real-network failure mode the fallback exists for (the undirected
    relaxation's preferred flow direction is not a legal directed move), so
    every iteration's path comes from a fallback, with no mocking.
    """
    G = nx.MultiDiGraph()
    coords = {
        "A": (0.0000, 0.0000),
        "B": (0.0010, 0.0000),
        "C": (0.0020, 0.0000),
        "E": (0.0030, 0.0000),
        "D": (0.0015, 0.0010),
    }
    for n, (y, x) in coords.items():
        G.add_node(n, y=y, x=x)

    def add(u, v, tt, both=True):
        G.add_edge(u, v, key=0, length=100.0, speed_kph=36.0, travel_time=tt)
        if both:
            G.add_edge(v, u, key=0, length=100.0, speed_kph=36.0, travel_time=tt)

    add("A", "B", 10.0)
    add("B", "C", 10.0)
    add("C", "E", 10.0)
    add("A", "D", 1.0)
    add("E", "D", 1.0, both=False)  # one-way: E->D exists, D->E does not
    return G


_TRAP_OPTIMAL_COST = 30.0  # A-B-C-E; the A-D branch is not a legal route at all


def test_best_path_extraction_method_is_max_flux_walk_when_the_walk_wins():
    """On the plain toy graph the walk succeeds on every iteration, so the
    reported path is a genuine flux-walk result -- not a fallback."""
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    assert router.best_path_extraction_method() is None, "null before any extraction"
    result = _run(router)

    assert result.path == ["A", "B", "C", "D", "E"]
    assert router.extraction_counts() == {"max_flux_walk": router.n_solves()}
    assert router.best_path_extraction_method() == "max_flux_walk"
    assert router.last_extraction_method() == "max_flux_walk"


def test_best_path_extraction_method_reports_the_fallback_that_won():
    """When a fallback produced the path that set `best_cost`, that is what is
    reported -- so a stretch of 1.0 here is attributable to Dijkstra-via-the-
    fallback rather than to the model's own walk."""
    G = _oneway_trap_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    result = _run(router)

    counts = router.extraction_counts()
    assert "max_flux_walk" not in counts, (
        f"the walk must dead-end on this graph; got {counts}"
    )
    assert result.cost == pytest.approx(_TRAP_OPTIMAL_COST, rel=1e-9)
    assert router.best_path_extraction_method() in (
        "theta_subgraph_fallback",
        "full_graph_fallback",
    )
    assert router.best_path_extraction_method() in counts


def test_best_path_extraction_method_is_not_the_final_iterations_method():
    """The field tracks the method in force when `best_cost` LAST IMPROVED,
    not the method the run happened to end on. Measured divergence, no mocks:
    on the one-way-trap graph the winning path comes from the theta subgraph
    early (while D is still above theta) and the run ends on the full-graph
    fallback once the losing tubes have atrophied below theta."""
    G = _oneway_trap_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    _run(router)

    assert router.best_path_extraction_method() == "theta_subgraph_fallback"
    assert router.last_extraction_method() == "full_graph_fallback"
    assert router.best_path_extraction_method() != router.last_extraction_method()


def test_best_path_extraction_method_ignores_later_worse_walks():
    """The Phase 8a shape exactly: a fallback sets `best_cost` on iteration 1
    and the max-flux walk then succeeds on every later iteration but never
    beats it. The reported provenance must stay with the fallback, even though
    `extraction_counts()` is dominated by the walk and the run ENDS on it.

    Extraction is scripted here (the surrounding tests cover the unmocked
    case) so the ordering under test -- improvement early, success late -- is
    pinned rather than left to whatever the relaxation happens to do."""
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)

    good = (["A", "B", "C", "D", "E"], 1.0, "theta_subgraph_fallback")
    worse = (["A", "E"], 2.0, "max_flux_walk")
    calls = {"n": 0}

    def scripted(_p, _w):
        calls["n"] += 1
        return good if calls["n"] == 1 else worse

    router._extract_path = scripted  # instance-level override, test only
    for _ in range(6):
        router.step()

    assert router.best_cost() == pytest.approx(1.0)
    assert router.best_path() == ["A", "B", "C", "D", "E"]
    assert router.last_extraction_method() == "max_flux_walk"
    assert router.extraction_counts() == {
        "theta_subgraph_fallback": 1,
        "max_flux_walk": 5,
    }
    assert router.best_path_extraction_method() == "theta_subgraph_fallback"


def test_best_path_extraction_method_resets_with_the_best_path_tracker():
    """A disruption resets `best_path`/`best_cost` (a pre-disruption best may
    contain a severed edge), so the provenance must reset with them -- a
    retained method would attribute the new run's answer to the old run's
    extraction."""
    G = _toy_graph_with_spur(1)
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    _run(router)
    assert router.best_path_extraction_method() is not None

    router.apply_disruption([("D", "F", 0), ("F", "D", 0)], warm=True)
    assert router.best_path() is None
    assert router.best_path_extraction_method() is None, (
        "provenance must not survive the reset of the path it describes"
    )

    _run(router)
    assert router.best_path_extraction_method() in router.extraction_counts()


def test_best_path_extraction_method_is_carried_in_state():
    """`state()` already carries `best_path`/`best_cost`, so it carries their
    provenance too -- a checkpoint that restored the path without the method
    would report an answer whose origin is unknown. (`load_state` is exercised
    with int node ids elsewhere; it parses its `D_by_pair` keys as ints, so it
    cannot be round-tripped on this string-labelled toy graph.)"""
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    _run(router)

    saved = router.state()
    assert saved["best_path_extraction_method"] == "max_flux_walk"
    assert saved["best_path"] == router.best_path()

    other = _make_router("tero")
    other.prepare(G, "A", "E", snap, seed=0)
    other.load_state({k: v for k, v in saved.items() if k != "D_by_pair"})
    assert other.best_path_extraction_method() == "max_flux_walk"


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


# --------------------------------------------------------------------------
# Phase 8a regression: a closure that strands part of the graph must not make
# the Laplacian solve singular, and a solve that IS degenerate must surface as
# a failure instead of being absorbed by the extraction fallback.
#
# The degenerate condition reproduced here is the real one: Phase 7 guarantees
# source-target connectivity only, nodes are never deleted (only edges), so a
# closure can leave nodes the source cannot reach. Grounding `p_sink = 0`
# grounds ONE component, so every such node contributed a zero row (isolated)
# or an ungrounded floating block, `spsolve` returned all-NaN pressures, and
# path extraction fell through to `full_graph_fallback` while the run still
# reported `failed=False`. Measured over the full Phase 8 matrix: 82 of 360
# disrupted cells, S6 on 100% of its 60 cells.
# --------------------------------------------------------------------------


def _toy_graph_with_spur(spur_len: int = 1) -> nx.MultiDiGraph:
    """The A-B-C-D-E toy graph plus a dead-end spur hanging off D.

    Closing the D-F link strands the whole spur: with `spur_len == 1` node F
    becomes ISOLATED (an exactly-zero Laplacian row, the S6/S5 shape); with
    `spur_len == 2` the stranded region is a 2-node component F-G whose
    internal tube survives (an ungrounded floating block, the S3/S4/S5 shape).
    Both shapes were measured in the real sweep and both went singular.
    """
    G = _toy_graph()
    seg_len_m = 111.19
    speed_kph = 36.0
    tt = seg_len_m / (speed_kph * 1000.0 / 3600.0)
    spur = ["F", "G", "H"][:spur_len]
    chain = ["D", *spur]
    for i, name in enumerate(spur, start=1):
        G.add_node(name, y=0.0030, x=0.0001 * i)
    for u, v in zip(chain, chain[1:]):
        G.add_edge(u, v, key=0, length=seg_len_m, speed_kph=speed_kph, travel_time=tt)
        G.add_edge(v, u, key=0, length=seg_len_m, speed_kph=speed_kph, travel_time=tt)
    return G


@pytest.mark.parametrize("spur_len", [1, 2])
@pytest.mark.parametrize("warm", [True, False])
def test_stranded_region_does_not_make_the_solve_singular(spur_len, warm):
    import scipy.sparse.linalg as spla

    G = _toy_graph_with_spur(spur_len)
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    assert router.active_component() == (5 + spur_len, 5 + spur_len)
    _run(router)

    # Sever the spur in both directions -> the spur is now unreachable from A.
    router.apply_disruption([("D", "F", 0), ("F", "D", 0)], warm=warm)

    n_active, n_total = router.active_component()
    assert n_total == 5 + spur_len
    assert n_active == 5, "the Laplacian must be restricted to A-B-C-D-E"

    with warnings.catch_warnings():
        # The exact failure this test exists to prevent.
        warnings.simplefilter("error", spla.MatrixRankWarning)
        result = _run(router)

    assert np.all(np.isfinite(router._last_p)), "pressures must stay finite"
    assert np.all(np.isfinite(router._D)), "conductivities must stay finite"
    # Pressure is 0 (and flux 0) everywhere outside the active component.
    for name in ["F", "G"][:spur_len]:
        assert router._last_p[router._node_idx[name]] == 0.0
    assert result.path == ["A", "B", "C", "D", "E"]
    assert result.cost == pytest.approx(_CHAIN_COST, rel=1e-6)
    assert "no_path_found" not in router.extraction_counts()


@pytest.mark.parametrize("spur_len", [1, 2])
def test_kirchhoff_still_holds_with_a_stranded_region(spur_len):
    G = _toy_graph_with_spur(spur_len)
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    router.apply_disruption([("D", "F", 0), ("F", "D", 0)], warm=True)
    router.step()

    net = router.last_flux_by_node()
    assert net["A"] == pytest.approx(-router._I0, abs=1e-6)
    assert net["E"] == pytest.approx(router._I0, abs=1e-6)
    for node in ("B", "C", "D", *["F", "G"][:spur_len]):
        assert net[node] == pytest.approx(0.0, abs=1e-6)


def test_degenerate_solve_raises_instead_of_being_silently_scored():
    """A solve the component restriction does NOT cover must raise, not warn.

    Zeroing every conductivity reproduces an exactly-singular reduced system
    inside the active component -- the general shape of any residual
    degeneracy. The contract is that it propagates out of `step()` so Phase
    6 records `failed=True`, rather than returning NaN pressures that the
    extraction fallback would quietly convert into a plausible stretch.
    """
    from bioroute.algorithms.physarum import PhysarumDegenerateSolve

    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    router._D[:] = 0.0

    with pytest.raises(PhysarumDegenerateSolve):
        router.step()


def test_degenerate_solve_is_recorded_as_a_failed_run():
    """End to end: the exception becomes a recorded failure, never a silent
    `failed=False` row with a fallback-derived stretch."""
    from bioroute.algorithms.physarum import PhysarumDegenerateSolve
    from bioroute.metrics.collectors import run_and_collect

    # Phase 6's schema requires integer node ids, so this is the toy graph
    # relabelled A..E -> 1..5.
    G = nx.relabel_nodes(_toy_graph(), {c: i for i, c in enumerate("ABCDE", start=1)})
    snap = _toy_snapshot(G)

    class _AlwaysDegenerate(PhysarumRouter):
        def step(self):
            raise PhysarumDegenerateSolve("forced degeneracy for the test")

    record = run_and_collect(
        _AlwaysDegenerate(PHYSARUM_CONFIG_PATH),
        G,
        1,
        5,
        snap,
        budget=5,
        seed=0,
        algorithm="physarum",
    )
    assert record.failed is True
    assert "PhysarumDegenerateSolve" in record.failure_reason
    assert record.stretch is None


def test_unreachable_target_is_a_recorded_failure_not_a_singular_solve():
    """If the target itself is stranded there is no pressure field to solve
    for; that must be a stated failure, not a singular matrix."""
    from bioroute.algorithms.physarum import PhysarumUnreachableTarget

    G = _toy_graph_with_spur(1)
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "F", snap, seed=0)
    _run(router)
    with pytest.raises(PhysarumUnreachableTarget):
        router.apply_disruption([("D", "F", 0), ("F", "D", 0)], warm=True)


def test_active_component_is_whole_graph_when_nothing_is_stranded():
    """The restriction must be a no-op on a connected graph -- this is what
    makes Phase 5's measurements still valid after the Phase 8a fix."""
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    n_active, n_total = router.active_component()
    assert n_active == n_total == 5
    assert router._reduced_n == 4
    # Severing an interior link leaves every node still reachable.
    router.apply_disruption([("B", "C", 0), ("C", "B", 0)], warm=True)
    assert router.active_component() == (5, 5)


# --------------------------------------------------------------------------
# PHASE 8C: how good is the MAX-FLUX WALK on its own?
#
# Phase 8b's provenance figure (33% of reported paths from the walk) is a
# true statement about provenance and a misleading one about accuracy, for two
# bookkeeping reasons: the fallbacks are Dijkstra-exact by construction and
# fire while every D is still above `theta` (so the theta subgraph is ~the
# whole graph), and `cost < best_cost` is STRICT -- so once the optimum is in,
# the walk can only TIE and a tie is scored as a loss. These tests pin the
# read-only walk-only tracker that measures the counterfactual "what would
# Physarum report if the walk were its only extraction method?".
# `scripts/measure_physarum_walk.py` is the harness;
# docs/tuning/physarum_extraction_provenance.md has the measured verdict.
# --------------------------------------------------------------------------


def test_walk_tracker_matches_the_pooled_best_when_the_walk_always_wins():
    """On the plain toy graph the walk succeeds on every iteration, so the
    walk-only best IS the pooled best and nothing was taken from it."""
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    assert router.walk_best_cost() is None, "null before any extraction"
    assert router.walk_success_count() == 0
    result = _run(router)

    assert router.walk_best_cost() == pytest.approx(result.cost, rel=1e-12)
    assert router.walk_best_path() == result.path
    assert router.walk_success_count() == router.extraction_counts()["max_flux_walk"]
    assert router.walk_first_success_iteration() == 1
    assert router.walk_best_iteration() is not None


def test_walk_tracker_is_null_when_the_walk_never_completes_a_path():
    """The one-way trap graph dead-ends the walk on every iteration. The
    router still REPORTS a path (via the fallback, unchanged), but the
    walk-only measurement must say plainly that the model's own extraction
    produced nothing -- that distinction is the whole point."""
    G = _oneway_trap_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    result = _run(router)

    assert "max_flux_walk" not in router.extraction_counts()
    assert result.cost == pytest.approx(_TRAP_OPTIMAL_COST, rel=1e-9)
    assert router.walk_best_cost() is None
    assert router.walk_best_path() is None
    assert router.walk_best_iteration() is None
    assert router.walk_first_success_iteration() is None
    assert router.walk_success_count() == 0


def test_walk_tracker_records_the_tie_the_strict_comparison_denies():
    """THE CRUX. A fallback sets `best_cost` on iteration 1; the walk then
    reaches the IDENTICAL cost on every later iteration and is recorded as
    having contributed nothing, because `cost < best_cost` is strict. The
    provenance field must keep saying `theta_subgraph_fallback` (it is not a
    lie -- that method is what set the value) while the walk-only tracker
    shows the walk found the same optimum.

    Extraction is scripted so the ordering under test is pinned rather than
    left to whatever the relaxation happens to do; the unmocked cases are
    covered by the two tests above and measured at scale by the script."""
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)

    optimum = (["A", "B", "C", "D", "E"], 1.0, "theta_subgraph_fallback")
    walk_tie = (["A", "B", "C", "D", "E"], 1.0, "max_flux_walk")
    calls = {"n": 0}

    def scripted(_p, _w):
        calls["n"] += 1
        return optimum if calls["n"] == 1 else walk_tie

    router._extract_path = scripted  # instance-level override, test only
    for _ in range(8):
        router.step()

    assert router.best_cost() == pytest.approx(1.0)
    assert router.best_path_extraction_method() == "theta_subgraph_fallback"
    # ... and yet the walk found exactly the same cost, 7 times.
    assert router.walk_best_cost() == pytest.approx(1.0)
    assert router.walk_success_count() == 7
    assert router.walk_first_success_iteration() == 2
    assert router.walk_best_iteration() == 2


@pytest.mark.parametrize("graph_fn", [_toy_graph, _oneway_trap_graph])
def test_walk_best_is_never_better_than_the_pooled_best(graph_fn):
    """Structural invariant: the pooled best is a minimum over a SUPERSET of
    the walk's results, so the walk can never come out ahead. A violation
    would mean the instrumentation is reading something other than the costs
    the pooled tracker sees."""
    G = graph_fn()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    _run(router)

    if router.walk_best_cost() is not None:
        assert router.walk_best_cost() >= router.best_cost() - 1e-12


def test_walk_tracker_resets_with_the_best_path_tracker():
    """A pre-disruption walk path can contain a severed edge, so the walk-only
    measurement must reset with `best_path`/`best_cost` -- otherwise a
    recovery row would report the walk's PRE-closure accuracy."""
    G = _toy_graph_with_spur(1)
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    _run(router)
    assert router.walk_best_cost() is not None

    router.apply_disruption([("D", "F", 0), ("F", "D", 0)], warm=True)
    assert router.walk_best_cost() is None
    assert router.walk_best_path() is None
    assert router.walk_best_iteration() is None
    assert router.walk_first_success_iteration() is None
    assert router.walk_success_count() == 0


def test_walk_best_path_is_a_copy_not_an_alias():
    """The tracker is measurement: handing out the live list would let a
    reader mutate state the model holds. Pinned because the whole claim of
    Phase 8c is that the instrumentation cannot change what Physarum does."""
    G = _toy_graph()
    snap = _toy_snapshot(G)
    router = _make_router("tero")
    router.prepare(G, "A", "E", snap, seed=0)
    _run(router)

    got = router.walk_best_path()
    got.append("Z")
    assert router.walk_best_path() == ["A", "B", "C", "D", "E"]
    assert router.best_path() == ["A", "B", "C", "D", "E"]
