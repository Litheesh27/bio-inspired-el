"""Phase 2b/2c tests: SUMO edge-ID parsing, SUMOProvider snapshot
contract, join coverage, A*-vs-Dijkstra cost equality under SUMO weights,
and snapshot reproducibility.

Skips cleanly if SUMO is not installed (no $SUMO_HOME/bin/sumo.exe
resolvable) or if the SUMO pipeline artifacts (net.net.xml, edgedata_*.xml)
are not present on disk, so the suite still passes on a machine without
SUMO.
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
from bioroute.congestion.snapshot import TrafficSnapshot
from bioroute.congestion.sumo import (
    resolve_sumo_home,
    sumo_bin,
)
from bioroute.network.ids import parse_sumo_edge_id

CONFIG_PATH = ROOT / "configs" / "area_central_blr.yaml"
SUMO_CONFIG_PATH = ROOT / "configs" / "sumo.yaml"
NET_PATH = ROOT / "data" / "sumo" / "net.net.xml"
EDGEDATA_LIGHT = ROOT / "data" / "sumo" / "edgedata_light.xml"
EDGEDATA_HEAVY = ROOT / "data" / "sumo" / "edgedata_heavy.xml"


def _sumo_available() -> bool:
    home = resolve_sumo_home()
    return Path(sumo_bin("sumo", home)).exists()


SUMO_UNAVAILABLE = not _sumo_available()
PIPELINE_ARTIFACTS_MISSING = not (NET_PATH.exists() and EDGEDATA_LIGHT.exists() and EDGEDATA_HEAVY.exists())

pytestmark = pytest.mark.skipif(
    SUMO_UNAVAILABLE, reason="SUMO is not installed/resolvable on this machine"
)


# --------------------------------------------------------------------------
# SUMO edge ID <-> way ID parsing (pure function, no SUMO artifacts needed)
# --------------------------------------------------------------------------


def test_parse_sumo_edge_id_plain():
    assert parse_sumo_edge_id("123456") == (123456, 1)


def test_parse_sumo_edge_id_reverse():
    assert parse_sumo_edge_id("-123456") == (123456, -1)


def test_parse_sumo_edge_id_split_segment():
    assert parse_sumo_edge_id("123456#2") == (123456, 1)


def test_parse_sumo_edge_id_reverse_split():
    assert parse_sumo_edge_id("-123456#2") == (123456, -1)


def test_parse_sumo_edge_id_reverse_split_multidigit():
    assert parse_sumo_edge_id("-1032505195#13") == (1032505195, -1)


def test_parse_sumo_edge_id_excludes_internal_junction_edges():
    assert parse_sumo_edge_id(":cluster_1232686162_246397923_0") is None
    assert parse_sumo_edge_id(":junction_1_0") is None


def test_parse_sumo_edge_id_ramp_suffix():
    # netconvert's --ramps.guess appends -Added{On,Off}RampEdge to an
    # otherwise-normal ID (observed on the real generated net; see
    # network/ids.py module docstring).
    assert parse_sumo_edge_id("36853150-AddedOnRampEdge") == (36853150, 1)
    assert parse_sumo_edge_id("72099280#4-AddedOffRampEdge") == (72099280, 1)
    assert parse_sumo_edge_id("-1109914955-AddedOnRampEdge") == (1109914955, -1)


def test_parse_sumo_edge_id_unparseable_returns_none():
    assert parse_sumo_edge_id("not_a_way_id") is None


# --------------------------------------------------------------------------
# Fixtures requiring the real cached graph + generated SUMO pipeline
# artifacts
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
def provider(real_graph, area_cfg):
    if PIPELINE_ARTIFACTS_MISSING:
        pytest.skip(
            "SUMO pipeline artifacts not found under data/sumo/; "
            "run scripts/02_run_sumo.py first."
        )
    from bioroute.congestion.sumo import SUMOProvider

    return SUMOProvider(real_graph, area_cfg, sumo_cfg_path=SUMO_CONFIG_PATH)


# --------------------------------------------------------------------------
# SUMOProvider snapshot contract
# --------------------------------------------------------------------------


def test_sumo_provider_levels_and_time_bins(provider):
    assert set(provider.levels) == {"light", "heavy"}
    assert provider.n_time_bins == 1


def test_sumo_provider_snapshot_satisfies_traffic_snapshot_contract(provider, real_graph):
    for level in provider.levels:
        snap = provider.snapshot(level)
        assert isinstance(snap, TrafficSnapshot)
        assert snap.source == "sumo"
        assert snap.level == level
        # Contract already enforced by TrafficSnapshot.__post_init__ (finite,
        # >0) at construction time; re-check explicitly here too.
        assert snap.travel_time.keys() == set(real_graph.edges(keys=True))
        for v in snap.travel_time.values():
            assert v > 0
            import math

            assert math.isfinite(v)


def test_sumo_provider_rejects_bad_level(provider):
    with pytest.raises(ValueError):
        provider.snapshot("nonexistent_level")


def test_sumo_provider_rejects_nonzero_time_bin(provider):
    with pytest.raises(ValueError):
        provider.snapshot("light", time_bin=1)


# --------------------------------------------------------------------------
# Coverage exit criterion
# --------------------------------------------------------------------------


def test_sumo_join_coverage_at_least_85_percent(provider):
    cov = provider.coverage_report()
    assert cov["coverage_pct"] >= 85.0, (
        f"SUMO<->graph edge-ID join coverage {cov['coverage_pct']:.2f}% "
        f"is below the 85% exit criterion"
    )


def test_sumo_join_never_counts_fallback_as_matched(provider, real_graph):
    """A graph edge whose travel_time came from the free-flow fallback
    (no matched SUMO edge, or matched edges with zero traffic observed)
    must not be double-counted as 'matched' in the coverage report."""
    cov = provider.coverage_report()
    assert cov["n_matched_edges"] + cov["n_unmatched_edges"] == cov["n_graph_edges"]
    assert cov["n_graph_edges"] == real_graph.number_of_edges()


# --------------------------------------------------------------------------
# A*-vs-Dijkstra cost equality under SUMO weights (same property already
# tested for BPR in test_classical.py)
# --------------------------------------------------------------------------


def test_astar_matches_dijkstra_cost_under_sumo_weights(provider, real_graph):
    import random

    from bioroute.algorithms.base import run_to_convergence
    from bioroute.algorithms.classical import AStarRouter, DijkstraRouter

    snap = provider.snapshot("heavy")
    rng = random.Random(99)
    nodes = sorted(real_graph.nodes())
    pairs = [tuple(rng.sample(nodes, 2)) for _ in range(30)]

    n_compared = 0
    for s, t in pairs:
        d_router = DijkstraRouter()
        d_router.prepare(real_graph, s, t, snap, seed=0)
        d_result = run_to_convergence(d_router, budget=1)

        a_router = AStarRouter()
        a_router.prepare(real_graph, s, t, snap, seed=0)
        a_result = run_to_convergence(a_router, budget=1)

        if d_result.path is None or a_result.path is None:
            continue
        n_compared += 1
        assert a_result.cost == pytest.approx(d_result.cost, rel=1e-6), (
            f"A* cost {a_result.cost} != Dijkstra cost {d_result.cost} for ({s},{t}) "
            f"under SUMO weights"
        )
    assert n_compared >= 20, f"only {n_compared}/30 OD pairs produced a comparable path"


# --------------------------------------------------------------------------
# Reproducibility from the cached edgedata
# --------------------------------------------------------------------------


def test_sumo_provider_reproducible_from_cached_edgedata(real_graph, area_cfg, tmp_path):
    from bioroute.congestion.sumo import SUMOProvider

    cache_a = tmp_path / "cache_a"
    cache_b = tmp_path / "cache_b"
    p1 = SUMOProvider(real_graph, area_cfg, sumo_cfg_path=SUMO_CONFIG_PATH, cache_dir=cache_a)
    p2 = SUMOProvider(real_graph, area_cfg, sumo_cfg_path=SUMO_CONFIG_PATH, cache_dir=cache_b)
    s1 = p1.snapshot("light")
    s2 = p2.snapshot("light")
    assert s1.travel_time.keys() == s2.travel_time.keys()
    for k in s1.travel_time:
        assert s1.travel_time[k] == pytest.approx(s2.travel_time[k])
    shutil.rmtree(cache_a, ignore_errors=True)
    shutil.rmtree(cache_b, ignore_errors=True)
