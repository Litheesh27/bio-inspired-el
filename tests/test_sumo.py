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


# --------------------------------------------------------------------------
# PHASE 2D: shared free-flow baseline + the implausible-value clipping census
# --------------------------------------------------------------------------


def test_sumo_snapshot_meta_records_baseline(provider):
    """A result row must be able to say which free-flow baseline produced
    it; the `provider` fixture is built on the Phase 1 config-speeds graph."""
    from bioroute.network.graph import BASELINE_CONFIG_SPEEDS

    snap = provider.snapshot("light")
    assert snap.meta["baseline"] == BASELINE_CONFIG_SPEEDS
    assert "baseline_hash" in snap.meta


def test_sumo_cache_key_differs_across_baselines(real_graph, area_cfg, tmp_path):
    """Unmatched graph edges fall back to the GRAPH's own free-flow
    travel_time, so a SUMO snapshot is a function of the baseline too. Both
    baseline graphs share an area-config hash, so without the baseline in
    the key the sumo-speeds run would silently read back config-speeds
    numbers."""
    from bioroute.congestion.sumo import SUMOProvider
    from bioroute.network.graph import BASELINE_SUMO_SPEEDS

    G2 = real_graph.copy()
    G2.graph["baseline"] = BASELINE_SUMO_SPEEDS
    G2.graph["baseline_hash"] = "cafebabe0001"

    p_cfg = SUMOProvider(real_graph, area_cfg, sumo_cfg_path=SUMO_CONFIG_PATH, cache_dir=tmp_path)
    p_sumo = SUMOProvider(G2, area_cfg, sumo_cfg_path=SUMO_CONFIG_PATH, cache_dir=tmp_path)
    assert p_cfg._cache_path("heavy") != p_sumo._cache_path("heavy")
    assert p_sumo.snapshot("heavy").meta["baseline"] == BASELINE_SUMO_SPEEDS


def test_edgedata_report_includes_clipping_census(provider):
    """`implausible_cap_factor` must be observable, not invisible: the
    report says how many interval samples the cap actually fired on, so
    "the clip is a no-op now" is a measurable claim rather than a guess."""
    for level in provider.levels:
        rep = provider.edgedata_report(level)
        for key in (
            "n_interval_samples",
            "n_samples_clipped",
            "frac_samples_clipped",
            "max_sample_ratio_to_freeflow",
            "cap_factor",
        ):
            assert key in rep, f"{key} missing from edgedata report for {level}"
        assert rep["n_interval_samples"] > 0
        assert 0 <= rep["n_samples_clipped"] <= rep["n_interval_samples"]
        assert rep["frac_samples_clipped"] == pytest.approx(
            rep["n_samples_clipped"] / rep["n_interval_samples"]
        )


def test_clip_implausible_traveltime_is_a_noop_below_the_cap():
    """The guard must never touch plausible values -- if it did, every
    congestion ratio would be biased downward."""
    from bioroute.congestion.sumo import _clip_implausible_traveltime

    assert _clip_implausible_traveltime(30.0, 10.0, 20.0) == 30.0
    assert _clip_implausible_traveltime(199.0, 10.0, 20.0) == 199.0
    # ... and must still bite on a genuinely implausible sample.
    assert _clip_implausible_traveltime(100000.0, 10.0, 20.0) == 200.0


_SUMO_LOG_SAMPLE = """Simulation ended at time: 5000.00.
Reason: The final simulation step has been reached.
Performance:
 Duration: 55.55s
 Real time factor: 90.0025
 UPS: 44009.900277
Vehicles:
 Inserted: 3000
 Running: 367
 Waiting: 0
 Teleports: 154 (Jam: 108, Yield: 41, Wrong Lane: 5)
Statistics (avg of 2633):
 RouteLength: 3330.14
 Speed: 7.90
 Duration: 612.59
 WaitingTime: 320.54
 TimeLoss: 422.02
 DepartDelay: 0.65
"""


def test_parse_sumo_stats_reads_teleport_categories_and_trip_statistics():
    """Two traps in sumo's report, both live in this fixture.

    1. `Duration:` appears TWICE -- once in `Performance:` as WALL-CLOCK
       seconds (55.55), once in `Statistics` as the mean trip duration
       (612.59). A plain search finds the wall-clock one first and reports
       it as a trip time.
    2. The teleport total alone does not say whether the run gridlocked;
       only the Jam/Yield/Wrong Lane split does, and Jam is the category
       that means "demand exceeded what the network can carry".
    """
    from bioroute.congestion.sumo import _parse_sumo_stats

    stats = _parse_sumo_stats(_SUMO_LOG_SAMPLE)
    assert stats["inserted"] == 3000
    assert stats["running_at_end"] == 367
    assert stats["teleports"] == 154
    assert stats["teleport_categories"] == {"Jam": 108, "Yield": 41, "Wrong Lane": 5}
    assert stats["teleport_pct_of_inserted"] == pytest.approx(100.0 * 154 / 3000)
    assert stats["running_pct_of_inserted"] == pytest.approx(100.0 * 367 / 3000)
    assert stats["n_trips_completed"] == 2633
    assert stats["mean_duration_s"] == pytest.approx(612.59), "picked up the wall-clock Duration"
    assert stats["mean_speed_mps"] == pytest.approx(7.90)
    assert stats["mean_timeloss_s"] == pytest.approx(422.02)


def test_parse_sumo_stats_tolerates_teleports_without_categories():
    from bioroute.congestion.sumo import _parse_sumo_stats

    stats = _parse_sumo_stats("Inserted: 10\n Running: 0\n Teleports: 0\n")
    assert stats["teleports"] == 0
    assert stats["teleport_categories"] == {}


def test_load_sumo_edges_carries_speeds(area_cfg):
    """Phase 2d re-annotates graph speeds from these; a record with no
    speed would silently make every edge fall back to config speeds."""
    if PIPELINE_ARTIFACTS_MISSING:
        pytest.skip("SUMO pipeline artifacts not found under data/sumo/.")
    from bioroute.congestion.sumo import resolve_sumo_home as _rsh
    from bioroute.network.ids import load_sumo_edges

    sys.path.insert(0, str(Path(_rsh()) / "tools"))
    import sumolib

    edges = load_sumo_edges(str(NET_PATH), sumolib_module=sumolib)
    assert edges
    assert all(e.speed_mps > 0 for e in edges), "some SUMO edges reported no usable speed"
