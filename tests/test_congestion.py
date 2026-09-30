"""Phase 2a tests: TrafficSnapshot validation, BPR determinism and
congestion ordering, on both a toy hand-built graph and the real cached
network.

Phase 4d adds the snapshot-cache concurrency tests at the bottom: real
spawned processes and real threads hitting a COLD cache at once, plus
corrupt-file handling and the serial pre-warm helper. See the comment block
above them for the race they regression-test.
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
from bioroute.congestion.sumo import SUMOProvider
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


# --------------------------------------------------------------------------
# Phase 4d: concurrency-safe snapshot cache
#
# The bug these cover: BPRProvider/SUMOProvider used to write their cache to
# `path.with_suffix(".json.tmp")` -- a temp name derived *deterministically*
# from the cache key, so every concurrent writer of the same snapshot shared
# ONE temp file. Phase 4c's 30-pair measurement ran 12 joblib workers against
# a cold cache and died halfway with PermissionError out of `tmp.replace()`:
# on Windows `os.replace` fails while any process holds a handle on the
# source or the destination, which POSIX tolerates.
#
# A happy-path cache test (write, read back, compare) passes cleanly against
# the OLD code and would never have caught this, so the tests below spawn
# real concurrent writers and readers against a COLD cache dir.
# --------------------------------------------------------------------------

_RACE_N_EDGES = 3000


def _race_snapshot() -> TrafficSnapshot:
    """A deterministic synthetic snapshot big enough (~3k edges, ~90 KB of
    JSON) that a writer is genuinely mid-write for a measurable window --
    the interleaving the old shared temp file allowed."""
    tt = {(i, i + 1, 0): 1.0 + (i % 97) * 0.01 for i in range(_RACE_N_EDGES)}
    return TrafficSnapshot(
        level="heavy", time_bin=1, source="bpr", travel_time=tt,
        meta={"synthetic": True, "n_edges": _RACE_N_EDGES},
    )


def _cache_race_worker(role, path_str, n_rounds, barrier, out_q):
    """Runs in a separate SPAWNED process (module-level so it is importable
    there). Writers hammer the same cache path; readers hammer the same path
    concurrently -- readers are what made the old code raise, since an open
    read handle blocks the writer's replace on Windows."""
    import traceback
    from pathlib import Path as _Path

    from bioroute.congestion.bpr import BPRProvider

    path = _Path(path_str)
    expected = _race_snapshot()
    errors: list[str] = []
    reads_valid = 0
    reads_missing = 0
    writes = 0
    try:
        barrier.wait(timeout=120)  # all workers hit a COLD cache together
        for _ in range(n_rounds):
            if role == "writer":
                BPRProvider._save_cache(path, expected)
                writes += 1
            got = BPRProvider._load_cache(path)
            if got is None:
                # Legitimate only before any writer has landed a file.
                reads_missing += 1
            elif got.travel_time != expected.travel_time or got.level != expected.level:
                errors.append(
                    "read a cache file that does not match what was written "
                    f"({len(got.travel_time)} edges, expected {len(expected.travel_time)})"
                )
            else:
                reads_valid += 1
    except Exception:  # noqa: BLE001 -- the whole point is to report it
        errors.append(traceback.format_exc())
    out_q.put(
        {
            "role": role,
            "errors": errors,
            "reads_valid": reads_valid,
            "reads_missing": reads_missing,
            "writes": writes,
        }
    )


def test_snapshot_cache_survives_concurrent_processes_on_cold_cache(tmp_path):
    """6 writer processes + 4 reader processes, released by a barrier onto
    the SAME cold cache path. Asserts: nobody raised (the old code raised
    PermissionError here), every successful read matched the written
    snapshot exactly, the final file is valid, and no temp files leaked.

    This is the regression test for the Phase 4c crash. Reverting
    write_json_atomic to the old shared-temp-file `tmp.replace(path)` makes
    it fail."""
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    cache_dir = tmp_path / "race_cache"
    cache_dir.mkdir()
    path = cache_dir / "bpr_race_heavy_bin1.json"
    assert not path.exists(), "cache must be cold for this test to mean anything"

    n_writers, n_readers, n_rounds = 6, 4, 8
    barrier = ctx.Barrier(n_writers + n_readers)
    out_q = ctx.Queue()
    procs = []
    for i in range(n_writers + n_readers):
        role = "writer" if i < n_writers else "reader"
        p = ctx.Process(
            target=_cache_race_worker,
            args=(role, str(path), n_rounds, barrier, out_q),
        )
        p.start()
        procs.append(p)

    reports = [out_q.get(timeout=300) for _ in range(len(procs))]
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker exited with {p.exitcode}"

    failures = [e for r in reports for e in r["errors"]]
    assert not failures, "concurrent cache access raised:\n" + "\n---\n".join(failures)

    total_valid = sum(r["reads_valid"] for r in reports)
    assert total_valid > 0, "no worker ever read a valid cache file"
    # Writers read immediately after their own write, so every writer read
    # must have hit a complete file.
    writer_missing = sum(r["reads_missing"] for r in reports if r["role"] == "writer")
    assert writer_missing == 0, (
        f"{writer_missing} reads returned None immediately after a successful "
        "write -- the file at the cache path was not complete/valid"
    )

    final = BPRProvider._load_cache(path)
    assert final is not None and final.travel_time == _race_snapshot().travel_time

    leftovers = [p.name for p in cache_dir.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"temp files leaked into the cache dir: {leftovers}"


def _int_node_toy_graph() -> nx.MultiDiGraph:
    """Same shape as `_toy_graph` but with INTEGER node ids. The on-disk
    snapshot cache serializes edge keys as "u_v_k" and parses them back as
    ints (real OSM node ids), so a string-node graph can be written but
    never read back -- which would make a cache test silently vacuous."""
    G = nx.MultiDiGraph()
    for n, (x, y) in {1: (0.0, 0.0), 2: (0.0, 0.001), 3: (0.001, 0.001), 4: (0.001, 0.002)}.items():
        G.add_node(n, x=x, y=y)
    G.add_edge(1, 2, key=0, length=10.0, speed_kph=36.0, travel_time=1.0, highway="residential")
    G.add_edge(2, 3, key=0, length=10.0, speed_kph=36.0, travel_time=5.0, highway="residential")
    G.add_edge(2, 3, key=1, length=10.0, speed_kph=36.0, travel_time=1.0, highway="primary")
    G.add_edge(3, 4, key=0, length=10.0, speed_kph=36.0, travel_time=1.0, highway="residential")
    G.add_edge(1, 4, key=0, length=100.0, speed_kph=36.0, travel_time=10.0, highway="secondary")
    G.add_edge(4, 1, key=0, length=100.0, speed_kph=36.0, travel_time=10.0, highway="secondary")
    G.add_edge(3, 1, key=0, length=20.0, speed_kph=36.0, travel_time=2.0, highway="residential")
    return G


def _toy_congestion_config(tmp_path) -> Path:
    """The real congestion config with the OD sample shrunk, so a toy-graph
    BPR assignment is fast enough to run many times in a test."""
    import yaml as _yaml

    with CONGESTION_CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = _yaml.safe_load(f)
    cfg["demand"]["n_od_pairs"] = 8
    cfg["demand"]["n_increments"] = 2
    out = tmp_path / "congestion_small.yaml"
    with out.open("w", encoding="utf-8") as f:
        _yaml.safe_dump(cfg, f)
    return out


def test_bpr_snapshot_concurrent_threads_cold_cache_identical_results(tmp_path, area_cfg):
    """The full provider path (compute -> save -> load), hit by 8 threads on
    a cold cache at once. Every thread must get the same snapshot back and
    none may raise. Threads complement the process test above: they share
    one interpreter, so they also cover two writers inside one process."""
    import threading

    G = _int_node_toy_graph()
    cache_dir = tmp_path / "thread_cache"
    cfg_path = _toy_congestion_config(tmp_path)

    n_threads = 8
    start = threading.Barrier(n_threads)
    results: list = [None] * n_threads
    errors: list = [None] * n_threads

    def work(i):
        try:
            provider = BPRProvider(G, area_cfg, cfg_path, cache_dir=cache_dir)
            start.wait(timeout=60)
            results[i] = provider.snapshot("heavy", time_bin=1)
        except Exception as exc:  # noqa: BLE001
            errors[i] = f"{type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive()

    assert not [e for e in errors if e], f"concurrent snapshot() raised: {errors}"
    assert all(r is not None for r in results)
    first = results[0]
    for r in results[1:]:
        assert r.travel_time == first.travel_time
        assert r.level == first.level and r.time_bin == first.time_bin

    leftovers = [p.name for p in cache_dir.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"temp files leaked: {leftovers}"


@pytest.mark.parametrize(
    "corrupt",
    [
        "",                                  # zero-length (interrupted write)
        "{\"level\": \"heavy\", \"travel",   # truncated mid-document
        "{}",                                 # parseable but structurally empty
        "{\"level\": \"heavy\", \"time_bin\": 1, \"source\": \"bpr\", "
        "\"meta\": {}, \"travel_time\": {}}",  # valid shape, no edges
        "not json at all",
    ],
)
def test_corrupt_cache_file_is_never_silently_reused(tmp_path, corrupt):
    """A partial or garbage cache file must read as a MISS (recompute), not
    as an exception and not as a partially-populated snapshot."""
    path = tmp_path / "corrupt.json"
    path.write_text(corrupt, encoding="utf-8")
    assert BPRProvider._load_cache(path) is None
    assert SUMOProvider._load_cache(path) is None


def test_cache_write_then_read_roundtrip_and_no_tmp_left(tmp_path):
    from bioroute.congestion.base import load_snapshot_cache, save_snapshot_cache

    path = tmp_path / "rt.json"
    snap = _race_snapshot()
    assert save_snapshot_cache(path, snap) is True
    got = load_snapshot_cache(path)
    assert got is not None and got.travel_time == snap.travel_time
    assert list(tmp_path.glob("*.tmp")) == []


def test_prewarm_snapshots_materialises_every_cache_file(tmp_path, area_cfg):
    """The serial pre-warm Phase 8 must call before any parallel section:
    after it returns, every needed cache file exists, so parallel workers
    only ever READ."""
    from bioroute.congestion.base import prewarm_snapshots

    G = _int_node_toy_graph()
    cache_dir = tmp_path / "prewarm_cache"
    cfg_path = _toy_congestion_config(tmp_path)
    provider = BPRProvider(G, area_cfg, cfg_path, cache_dir=cache_dir)

    out = prewarm_snapshots(provider, time_bins=[0, 1])
    assert set(out) == {("light", 0), ("light", 1), ("heavy", 0), ("heavy", 1)}

    files = sorted(p.name for p in cache_dir.glob("*.json"))
    assert len(files) == 4, files

    # Every one of those files must be READABLE (not just present), so a
    # parallel worker is served from cache instead of recomputing.
    provider2 = BPRProvider(G, area_cfg, cfg_path, cache_dir=cache_dir)
    for (level, time_bin), snap in out.items():
        cached = BPRProvider._load_cache(provider2._cache_path(level, time_bin))
        assert cached is not None, f"pre-warmed file for {level}/{time_bin} does not load back"
        assert cached.travel_time == snap.travel_time
        assert provider2.snapshot(level, time_bin).travel_time == snap.travel_time


# ==========================================================================
# PHASE 2D: snapshots record which free-flow baseline produced them
# ==========================================================================
#
# The failure these close. Both baseline graphs (config_speeds and
# sumo_speeds) describe the SAME study area and therefore share an
# area-config hash, while carrying different `travel_time` on every edge.
# BPR's t0 IS that attribute and SUMO falls back to it on unmatched edges,
# so a snapshot is a function of the baseline -- but the cache key was not.
# Without the baseline folded into the key, switching baselines would read
# back the other baseline's numbers from disk and report them as the new
# one's, which is the worst class of bug this project can have: silently
# wrong, fully reproducible.

from bioroute.network.graph import (  # noqa: E402
    BASELINE_CONFIG_SPEEDS,
    BASELINE_SUMO_SPEEDS,
)


def _stamped(G: nx.MultiDiGraph, baseline: str, baseline_hash: str) -> nx.MultiDiGraph:
    """A copy of G stamped as if it came from `load_baseline_graph`."""
    G2 = G.copy()
    G2.graph["baseline"] = baseline
    G2.graph["baseline_hash"] = baseline_hash
    return G2


def test_freeflow_snapshot_meta_records_baseline():
    G = _int_node_toy_graph()
    unstamped = FreeFlowProvider(G).snapshot()
    assert unstamped.meta["baseline"] == BASELINE_CONFIG_SPEEDS
    assert unstamped.meta["baseline_hash"] == ""

    G2 = _stamped(G, BASELINE_SUMO_SPEEDS, "cafebabe0001")
    stamped = FreeFlowProvider(G2).snapshot()
    assert stamped.meta["baseline"] == BASELINE_SUMO_SPEEDS
    assert stamped.meta["baseline_hash"] == "cafebabe0001"


def test_bpr_snapshot_meta_records_baseline(tmp_path, area_cfg):
    cfg_path = _toy_congestion_config(tmp_path)
    G = _stamped(_int_node_toy_graph(), BASELINE_SUMO_SPEEDS, "cafebabe0001")
    provider = BPRProvider(G, area_cfg, cfg_path, cache_dir=tmp_path / "c")
    snap = provider.snapshot("heavy", time_bin=1)
    assert snap.meta["baseline"] == BASELINE_SUMO_SPEEDS
    assert snap.meta["baseline_hash"] == "cafebabe0001"


def test_bpr_cache_key_differs_across_baselines(tmp_path, area_cfg):
    """Different baseline -> different cache file. Same baseline -> same
    file (otherwise the cache would never hit)."""
    cfg_path = _toy_congestion_config(tmp_path)
    G_cfg = _stamped(_int_node_toy_graph(), BASELINE_CONFIG_SPEEDS, "")
    G_sumo = _stamped(_int_node_toy_graph(), BASELINE_SUMO_SPEEDS, "cafebabe0001")
    cache = tmp_path / "shared_cache"

    p_cfg = BPRProvider(G_cfg, area_cfg, cfg_path, cache_dir=cache)
    p_sumo = BPRProvider(G_sumo, area_cfg, cfg_path, cache_dir=cache)
    p_sumo_again = BPRProvider(
        _stamped(_int_node_toy_graph(), BASELINE_SUMO_SPEEDS, "cafebabe0001"),
        area_cfg, cfg_path, cache_dir=cache,
    )

    assert p_cfg._cache_path("heavy", 1) != p_sumo._cache_path("heavy", 1)
    assert p_sumo._cache_path("heavy", 1) == p_sumo_again._cache_path("heavy", 1)


def test_bpr_does_not_reuse_other_baselines_cached_snapshot(tmp_path, area_cfg):
    """The real regression: compute and cache a snapshot on one baseline
    graph, then ask for the same (level, time_bin) on a DIFFERENT baseline
    whose free-flow times are doubled, sharing one cache dir. The second
    answer must be recomputed, not the first one read back."""
    cfg_path = _toy_congestion_config(tmp_path)
    cache = tmp_path / "shared_cache"

    G_cfg = _stamped(_int_node_toy_graph(), BASELINE_CONFIG_SPEEDS, "")
    G_sumo = _stamped(_int_node_toy_graph(), BASELINE_SUMO_SPEEDS, "cafebabe0001")
    for _, _, data in G_sumo.edges(data=True):
        data["travel_time"] = float(data["travel_time"]) * 2.0

    snap_cfg = BPRProvider(G_cfg, area_cfg, cfg_path, cache_dir=cache).snapshot("heavy", 1)
    snap_sumo = BPRProvider(G_sumo, area_cfg, cfg_path, cache_dir=cache).snapshot("heavy", 1)

    assert snap_cfg.travel_time != snap_sumo.travel_time
    assert snap_sumo.meta["baseline"] == BASELINE_SUMO_SPEEDS
    # Doubling every free-flow time must show up in the congested times.
    for edge, value in snap_cfg.travel_time.items():
        assert snap_sumo.travel_time[edge] > value
