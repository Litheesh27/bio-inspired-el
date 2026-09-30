"""Phase 1 tests: config hashing, GraphML round-trip, connectivity, and edge
attribute sanity on the cached study-area graph.

Phase 2d adds the shared-free-flow-baseline tests at the bottom: the
SUMO-speed re-annotation (its arithmetic, its coverage accounting, and that
unmatched edges keep their config-derived speed), the new artifact's cache
round-trip, and cache-key invalidation.
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import networkx as nx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bioroute.config import load_area_config, config_hash
from bioroute.network.graph import cache_path, load_cached_graph, save_graph, build_graph
from bioroute.network.acquire import fetch_osmnx_graph

CONFIG_PATH = ROOT / "configs" / "area_central_blr.yaml"


@pytest.fixture(scope="module")
def cfg():
    return load_area_config(CONFIG_PATH)


@pytest.fixture(scope="module")
def cached_graph_path(cfg):
    path = cache_path(cfg)
    if not path.exists():
        pytest.skip(
            f"Cached graph not found at {path}; run "
            f"scripts/01_build_network.py first."
        )
    return path


@pytest.fixture(scope="module")
def cached_graph(cached_graph_path):
    return load_cached_graph(cached_graph_path)


def test_config_hash_stable_across_processes(cfg):
    """config_hash must not depend on Python's per-process hash salt."""
    h_in_process = config_hash(cfg)

    script = (
        "import sys; sys.path.insert(0, r'{src}'); "
        "from bioroute.config import load_area_config, config_hash; "
        "cfg = load_area_config(r'{config_path}'); "
        "print(config_hash(cfg))"
    ).format(src=str(ROOT / "src"), config_path=str(CONFIG_PATH))

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    h_subprocess = result.stdout.strip()

    assert h_in_process == h_subprocess
    assert len(h_in_process) == 12


def test_config_hash_changes_with_config(cfg):
    """Sanity check: hash actually depends on content, not a constant."""
    from dataclasses import replace

    h1 = config_hash(cfg)
    cfg2 = replace(cfg, name="different_name")
    h2 = config_hash(cfg2)
    assert h1 != h2


def test_graphml_roundtrip_preserves_types(tmp_path, cached_graph):
    """Build -> save -> load must be a no-op on edge attribute types:
    osmid stays list[int], length/travel_time stay float."""
    G = cached_graph
    tmp_path_graphml = tmp_path / "roundtrip_test.graphml"
    save_graph(G, tmp_path_graphml)
    G2 = load_cached_graph(tmp_path_graphml)

    assert G2.number_of_nodes() == G.number_of_nodes()
    assert G2.number_of_edges() == G.number_of_edges()

    for u, v, k, data in G2.edges(keys=True, data=True):
        osmid = data["osmid"]
        assert isinstance(osmid, list), f"osmid not a list after roundtrip: {osmid!r}"
        assert all(isinstance(x, int) for x in osmid), f"osmid elements not int: {osmid!r}"

        assert isinstance(data["length"], float)
        assert isinstance(data["travel_time"], float)


def test_cached_graph_is_single_scc(cached_graph):
    G = cached_graph
    sccs = list(nx.strongly_connected_components(G))
    assert len(sccs) == 1, f"Expected 1 SCC, found {len(sccs)}"
    assert len(sccs[0]) == G.number_of_nodes()


def test_edges_have_finite_positive_length_and_travel_time(cached_graph):
    G = cached_graph
    assert G.number_of_edges() > 0
    for u, v, k, data in G.edges(keys=True, data=True):
        length = data["length"]
        tt = data["travel_time"]
        assert math.isfinite(length) and length > 0, f"bad length on ({u},{v},{k}): {length!r}"
        assert math.isfinite(tt) and tt > 0, f"bad travel_time on ({u},{v},{k}): {tt!r}"


def test_node_count_in_exit_criterion_range(cached_graph):
    n = cached_graph.number_of_nodes()
    assert 2000 <= n <= 8000, f"Node count {n} outside required 2000-8000 range"


# ==========================================================================
# PHASE 2D: the shared free-flow baseline
# ==========================================================================
#
# What these guard. Before Phase 2d the graph's free-flow `travel_time` came
# from configs/area_central_blr.yaml's conservative Bengaluru speeds while
# SUMO used its own (faster) OSM/netconvert speeds, so SUMO *congested*
# times could land BELOW the graph's *free-flow* times and no ratio was
# comparable across providers. `annotate_speeds_from_sumo` re-annotates the
# graph from the SUMO net; these tests pin the arithmetic (length-weighted
# HARMONIC mean, not arithmetic -- the difference is the whole point),
# the accounting of edges that keep config speeds, and the round-trip of
# the new artifact, including the list[int] `osmid` that bit us in Phase 1.

from bioroute.network.graph import (  # noqa: E402
    BASELINE_CONFIG_SPEEDS,
    BASELINE_SUMO_SPEEDS,
    annotate_speeds_from_sumo,
    baseline_hash_of,
    baseline_meta,
    baseline_of,
    load_baseline_graph,
    sumo_speeds_baseline_hash,
    sumo_speeds_cache_path,
)
from bioroute.network.ids import MatchResult, ParsedSumoEdge  # noqa: E402


def _speed_toy_graph() -> nx.MultiDiGraph:
    """Two graph edges: one that will be matched to SUMO edges, one that
    will not. Config free-flow is 36 km/h = 10 m/s throughout, so every
    expected number below is hand-computable."""
    G = nx.MultiDiGraph()
    for n in (1, 2, 3):
        G.add_node(n, x=0.0, y=0.0)
    G.add_edge(1, 2, key=0, osmid=[100], length=200.0, speed_kph=36.0, travel_time=20.0)
    G.add_edge(2, 3, key=0, osmid=[200], length=100.0, speed_kph=36.0, travel_time=10.0)
    return G


def _speed_toy_match() -> tuple[list, MatchResult]:
    """Graph edge (1,2,0) matches two SUMO segments -- 100 m at 10 m/s and
    100 m at 50 m/s. Graph edge (2,3,0) matches nothing."""
    sumo_edges = [
        ParsedSumoEdge("100#0", 100, 1, "1", "9", 100.0, speed_mps=10.0),
        ParsedSumoEdge("100#1", 100, 1, "9", "2", 100.0, speed_mps=50.0),
    ]
    match = MatchResult()
    match.edge_to_sumo = {(1, 2, 0): ["100#0", "100#1"], (2, 3, 0): []}
    match.unmatched_edges = {(2, 3, 0)}
    return sumo_edges, match


def test_annotate_speeds_from_sumo_uses_length_weighted_harmonic_mean():
    """100 m at 10 m/s plus 100 m at 50 m/s takes 10 + 2 = 12 s, so the
    effective speed over 200 m is 200/12 = 16.667 m/s = 60 km/h. An
    ARITHMETIC mean would have said (10+50)/2 = 30 m/s and halved the
    travel time -- which is exactly the error this test exists to catch."""
    G = _speed_toy_graph()
    sumo_edges, match = _speed_toy_match()
    G2, report = annotate_speeds_from_sumo(G, sumo_edges, match, baseline_hash="deadbeef")

    data = G2.edges[(1, 2, 0)]
    assert data["speed_source"] == "sumo"
    assert data["speed_kph"] == pytest.approx(60.0)
    # Graph length (200 m) over the effective speed (16.667 m/s) = 12 s.
    assert data["travel_time"] == pytest.approx(12.0)


def test_annotate_speeds_from_sumo_unmatched_edge_keeps_config_speed():
    """Edges with no SUMO match must keep their config-derived values
    untouched, be stamped, and be COUNTED -- never silently defaulted."""
    G = _speed_toy_graph()
    sumo_edges, match = _speed_toy_match()
    G2, report = annotate_speeds_from_sumo(G, sumo_edges, match, baseline_hash="deadbeef")

    kept = G2.edges[(2, 3, 0)]
    assert kept["speed_source"] == "config"
    assert kept["speed_kph"] == pytest.approx(36.0)
    assert kept["travel_time"] == pytest.approx(10.0)

    assert report["n_graph_edges"] == 2
    assert report["n_edges_sumo_speed"] == 1
    assert report["n_edges_kept_config_speed"] == 1
    assert report["n_edges_sumo_speed"] + report["n_edges_kept_config_speed"] == 2
    assert report["coverage_pct"] == pytest.approx(50.0)


def test_annotate_speeds_from_sumo_ignores_nonpositive_speeds():
    """A SUMO edge reporting speed <= 0 is not usable data; if it is the
    only match, the graph edge must fall back to its config speed rather
    than acquire an infinite travel time."""
    G = _speed_toy_graph()
    sumo_edges = [ParsedSumoEdge("100#0", 100, 1, "1", "2", 200.0, speed_mps=0.0)]
    match = MatchResult()
    match.edge_to_sumo = {(1, 2, 0): ["100#0"], (2, 3, 0): []}
    G2, report = annotate_speeds_from_sumo(G, sumo_edges, match)

    assert G2.edges[(1, 2, 0)]["speed_source"] == "config"
    assert G2.edges[(1, 2, 0)]["travel_time"] == pytest.approx(20.0)
    assert report["n_edges_kept_config_speed"] == 2


def test_annotate_speeds_from_sumo_does_not_mutate_input_graph():
    """The config-speeds graph must stay reproducible: annotation returns a
    COPY and leaves the Phase 1 artifact's in-memory graph alone."""
    G = _speed_toy_graph()
    sumo_edges, match = _speed_toy_match()
    annotate_speeds_from_sumo(G, sumo_edges, match)

    assert G.edges[(1, 2, 0)]["travel_time"] == pytest.approx(20.0)
    assert "speed_source" not in G.edges[(1, 2, 0)]
    assert baseline_of(G) == BASELINE_CONFIG_SPEEDS


def test_annotate_speeds_from_sumo_stamps_baseline_on_graph():
    G = _speed_toy_graph()
    sumo_edges, match = _speed_toy_match()
    G2, _ = annotate_speeds_from_sumo(G, sumo_edges, match, baseline_hash="abc123abc123")

    assert baseline_of(G2) == BASELINE_SUMO_SPEEDS
    assert baseline_hash_of(G2) == "abc123abc123"
    assert baseline_meta(G2) == {
        "baseline": BASELINE_SUMO_SPEEDS,
        "baseline_hash": "abc123abc123",
    }
    # An un-annotated graph must report the config baseline, not blank.
    assert baseline_meta(G) == {"baseline": BASELINE_CONFIG_SPEEDS, "baseline_hash": ""}


def test_sumo_speeds_baseline_hash_changes_with_net_content(cfg, tmp_path):
    """Cache-key invariant: the artifact is keyed on the CONTENT of the net
    it was derived from, so regenerating the net invalidates it."""
    net_a = tmp_path / "a.net.xml"
    net_b = tmp_path / "b.net.xml"
    net_a.write_text("<net>alpha</net>", encoding="utf-8")
    net_b.write_text("<net>beta</net>", encoding="utf-8")

    flags = ["--junctions.join"]
    h_a = sumo_speeds_baseline_hash(cfg, net_a, flags)
    h_b = sumo_speeds_baseline_hash(cfg, net_b, flags)
    h_a_other_flags = sumo_speeds_baseline_hash(cfg, net_a, ["--ramps.guess"])

    assert h_a != h_b, "net content change did not invalidate the baseline hash"
    assert h_a != h_a_other_flags, "netconvert flag change did not invalidate the hash"
    assert len(h_a) == 12
    # Deterministic across calls (no per-process salt).
    assert h_a == sumo_speeds_baseline_hash(cfg, net_a, flags)


def test_net_content_hash_ignores_netconvert_generation_header(cfg, tmp_path):
    """netconvert writes a generation TIMESTAMP and the output path into a
    leading XML comment, so two byte-identical networks produced by two runs
    differ as files. Hashing that would invalidate the baseline artifact --
    and every BPR/SUMO snapshot keyed off it -- on every pipeline re-run for
    no reason. Verified against real netconvert output: regenerating from
    the same OSM extract and flags differs ONLY inside this comment."""
    from bioroute.network.graph import file_sha256, sumo_net_content_sha256

    header = (
        '<?xml version="1.0" encoding="UTF-8"?>\n\n'
        "<!-- generated on {stamp} by Eclipse SUMO netconvert 1.27.1\n"
        "<netconvertConfiguration>\n"
        "    <output><output-file value=\"{out}\"/></output>\n"
        "</netconvertConfiguration>\n-->\n\n"
    )
    body = '<net version="1.20">\n  <edge id="1" speed="13.89"/>\n</net>\n'

    a = tmp_path / "a.net.xml"
    b = tmp_path / "b.net.xml"
    a.write_text(header.format(stamp="2026-09-27T14:46:42", out="a") + body, encoding="utf-8")
    b.write_text(header.format(stamp="2026-09-30T19:41:19", out="b") + body, encoding="utf-8")

    assert file_sha256(a) != file_sha256(b), "fixture does not actually differ as bytes"
    assert sumo_net_content_sha256(a) == sumo_net_content_sha256(b)

    # ... but a real change to the network body must still be caught.
    c = tmp_path / "c.net.xml"
    c.write_text(
        header.format(stamp="2026-09-27T14:46:42", out="a")
        + body.replace("13.89", "27.78"),
        encoding="utf-8",
    )
    assert sumo_net_content_sha256(a) != sumo_net_content_sha256(c)

    # A file with no such header hashes whole.
    d = tmp_path / "d.net.xml"
    d.write_text(body, encoding="utf-8")
    assert sumo_net_content_sha256(d) == file_sha256(d)


def test_sumo_speeds_cache_path_is_separate_from_phase1_artifact(cfg):
    p_cfg = cache_path(cfg)
    p_sumo = sumo_speeds_cache_path(cfg, "abc123abc123")
    assert p_sumo != p_cfg
    assert p_sumo.name.endswith("_sumospeeds.graphml")
    assert config_hash(cfg) in p_sumo.name


def test_load_baseline_graph_rejects_unknown_baseline(cfg):
    with pytest.raises(ValueError, match="Unknown baseline"):
        load_baseline_graph(cfg, baseline="something_else")


def test_load_baseline_graph_config_speeds_stamps_baseline(cfg, cached_graph_path):
    G, info = load_baseline_graph(cfg, baseline=BASELINE_CONFIG_SPEEDS)
    assert info["baseline"] == BASELINE_CONFIG_SPEEDS
    assert baseline_of(G) == BASELINE_CONFIG_SPEEDS
    assert Path(info["path"]) == cached_graph_path


# -- the real artifact -----------------------------------------------------


@pytest.fixture(scope="module")
def sumo_speeds_graph(cfg):
    """The real re-annotated artifact, built by scripts/02_run_sumo.py."""
    import yaml

    with (ROOT / "configs" / "sumo.yaml").open("r", encoding="utf-8") as f:
        sumo_cfg = yaml.safe_load(f)
    net_path = ROOT / "data" / "sumo" / "net.net.xml"
    if not net_path.exists():
        pytest.skip(f"{net_path} not found; run scripts/02_run_sumo.py first.")
    h = sumo_speeds_baseline_hash(cfg, net_path, list(sumo_cfg["netconvert_flags"]))
    path = sumo_speeds_cache_path(cfg, h)
    if not path.exists():
        pytest.skip(f"Sumo-speeds baseline graph not found at {path}; run scripts/02_run_sumo.py.")
    return load_cached_graph(path)


def test_sumo_speeds_graph_roundtrip_preserves_types(tmp_path, sumo_speeds_graph):
    """Same round-trip guarantee as the Phase 1 artifact -- osmid must come
    back as list[int] (this is the one that bit us in Phase 1) and the
    baseline stamp must survive GraphML, since the providers key their
    snapshot cache on it."""
    G = sumo_speeds_graph
    out = tmp_path / "sumospeeds_roundtrip.graphml"
    save_graph(G, out)
    G2 = load_cached_graph(out)

    assert G2.number_of_nodes() == G.number_of_nodes()
    assert G2.number_of_edges() == G.number_of_edges()

    for u, v, k, data in G2.edges(keys=True, data=True):
        osmid = data["osmid"]
        assert isinstance(osmid, list), f"osmid not a list after roundtrip: {osmid!r}"
        assert all(isinstance(x, int) for x in osmid), f"osmid elements not int: {osmid!r}"
        assert isinstance(data["length"], float)
        assert isinstance(data["travel_time"], float)
        assert isinstance(data["speed_kph"], float)
        assert data["speed_source"] in ("sumo", "config")

    assert baseline_of(G2) == BASELINE_SUMO_SPEEDS
    assert baseline_hash_of(G2) == baseline_hash_of(G)


def test_sumo_speeds_graph_reannotation_coverage(sumo_speeds_graph):
    """The re-annotation rides on the Phase 2c edge-ID join (97.53%
    coverage), so the great majority of edges must carry SUMO speeds -- and
    the remainder must be explicitly config-sourced, not missing."""
    G = sumo_speeds_graph
    n = G.number_of_edges()
    n_sumo = sum(1 for _, _, d in G.edges(data=True) if d.get("speed_source") == "sumo")
    n_cfg = sum(1 for _, _, d in G.edges(data=True) if d.get("speed_source") == "config")

    assert n_sumo + n_cfg == n, "every edge must declare where its speed came from"
    assert 100.0 * n_sumo / n >= 85.0, f"only {100.0 * n_sumo / n:.2f}% of edges re-annotated"


def test_sumo_speeds_graph_keeps_graph_invariants(sumo_speeds_graph, cached_graph):
    """Re-annotation changes speeds, never topology, geometry, or the
    finite-positive-travel_time invariant."""
    G = sumo_speeds_graph
    assert G.number_of_nodes() == cached_graph.number_of_nodes()
    assert G.number_of_edges() == cached_graph.number_of_edges()
    assert set(G.edges(keys=True)) == set(cached_graph.edges(keys=True))

    for u, v, k, data in G.edges(keys=True, data=True):
        tt = data["travel_time"]
        assert math.isfinite(tt) and tt > 0, f"bad travel_time on ({u},{v},{k}): {tt!r}"
        assert data["length"] == pytest.approx(cached_graph.edges[(u, v, k)]["length"])


# -- the congested-time aggregation (Phase 2d correction) ------------------
#
# `compute_edge_travel_times` used to average the matched SUMO segments'
# TRAVEL TIMES, which answers "how long to cross one of my matched
# segments", not "how long to cross this graph edge". Because netconvert
# splits ways and OSMnx merges them, a graph edge matches a median 1.44x
# its own length in SUMO road (p90: 8.9x), so that rule understated 26% of
# this network's edges into implying speeds faster than the fastest speed
# limit in the network -- which silently broke A*'s optimality guarantee.


def test_compute_edge_travel_times_scales_to_graph_edge_length():
    """A 200 m graph edge matching 10 segments of 20 m at 2 s each takes
    20 s, not 2 s. Averaging the segment TIMES gives 2 s -- the exact
    tenfold understatement the pace formulation removes."""
    from bioroute.network.ids import compute_edge_travel_times

    G = nx.MultiDiGraph()
    G.add_node(1, x=0.0, y=0.0)
    G.add_node(2, x=0.0, y=0.0)
    G.add_edge(1, 2, key=0, osmid=[100], length=200.0, speed_kph=36.0, travel_time=20.0)

    sids = [f"100#{i}" for i in range(10)]
    match = MatchResult()
    match.edge_to_sumo = {(1, 2, 0): sids}
    tt_map, n_fallback = compute_edge_travel_times(
        G, match, {s: 2.0 for s in sids}, {s: 20.0 for s in sids}
    )

    assert n_fallback == 0
    assert tt_map[(1, 2, 0)] == pytest.approx(20.0)


def test_compute_edge_travel_times_length_weights_unequal_segments():
    """200 m graph edge over 100 m at 10 s plus 100 m at 2 s = 12 s. The
    old length-weighted mean of times gave 6 s."""
    from bioroute.network.ids import compute_edge_travel_times

    G = nx.MultiDiGraph()
    G.add_node(1, x=0.0, y=0.0)
    G.add_node(2, x=0.0, y=0.0)
    G.add_edge(1, 2, key=0, osmid=[100], length=200.0, speed_kph=36.0, travel_time=20.0)

    match = MatchResult()
    match.edge_to_sumo = {(1, 2, 0): ["100#0", "100#1"]}
    tt_map, _ = compute_edge_travel_times(
        G, match, {"100#0": 10.0, "100#1": 2.0}, {"100#0": 100.0, "100#1": 100.0}
    )
    assert tt_map[(1, 2, 0)] == pytest.approx(12.0)


def test_compute_edge_travel_times_falls_back_and_counts():
    """No match, or matches with no traveltime observation, must fall back
    to the graph's own free-flow time AND be counted -- never dropped, and
    never quietly reported as data."""
    from bioroute.network.ids import compute_edge_travel_times

    G = nx.MultiDiGraph()
    for n in (1, 2, 3):
        G.add_node(n, x=0.0, y=0.0)
    G.add_edge(1, 2, key=0, osmid=[100], length=200.0, speed_kph=36.0, travel_time=20.0)
    G.add_edge(2, 3, key=0, osmid=[200], length=100.0, speed_kph=36.0, travel_time=10.0)

    match = MatchResult()
    match.edge_to_sumo = {(1, 2, 0): [], (2, 3, 0): ["200#0"]}
    # "200#0" is matched but never observed carrying traffic.
    tt_map, n_fallback = compute_edge_travel_times(G, match, {}, {"200#0": 100.0})

    assert n_fallback == 2
    assert tt_map[(1, 2, 0)] == pytest.approx(20.0)
    assert tt_map[(2, 3, 0)] == pytest.approx(10.0)


def test_sumo_congested_speeds_stay_within_the_networks_speed_limits(sumo_speeds_graph):
    """The invariant the old aggregation broke, checked end to end on the
    real artifacts: no graph edge may come out of the SUMO join implying a
    speed meaningfully above the fastest limit anywhere in the network,
    because A*'s heuristic (great-circle distance over the global max edge
    speed) is only admissible if that holds. Under the old rule 1,794 of
    7,015 edges violated it, the worst by 266x."""
    import yaml

    from bioroute.config import load_area_config as _lac
    from bioroute.congestion.sumo import SUMOProvider

    net_path = ROOT / "data" / "sumo" / "net.net.xml"
    edgedata = ROOT / "data" / "sumo" / "edgedata_heavy.xml"
    if not (net_path.exists() and edgedata.exists()):
        pytest.skip("SUMO pipeline artifacts not found; run scripts/02_run_sumo.py first.")

    G = sumo_speeds_graph
    area_cfg = _lac(CONFIG_PATH)
    with (ROOT / "configs" / "sumo.yaml").open("r", encoding="utf-8") as f:
        yaml.safe_load(f)
    provider = SUMOProvider(G, area_cfg, sumo_cfg_path=ROOT / "configs" / "sumo.yaml")

    max_mps = max(float(d["speed_kph"]) for _, _, d in G.edges(data=True)) / 3.6
    for level in provider.levels:
        snap = provider.snapshot(level)
        offenders = []
        for (u, v, k), tt in snap.travel_time.items():
            implied = float(G.edges[(u, v, k)]["length"]) / tt
            if implied > max_mps * 1.10:
                offenders.append((u, v, k, implied / max_mps))
        assert not offenders, (
            f"{level}: {len(offenders)} edges imply >1.10x the global max speed; "
            f"worst {max(o[3] for o in offenders):.2f}x"
        )


def test_config_speeds_graph_untouched_by_reannotation(cached_graph):
    """The Phase 1 artifact must remain exactly what Phase 1 produced --
    no `speed_source` stamp, no baseline attribute."""
    assert baseline_of(cached_graph) == BASELINE_CONFIG_SPEEDS
    assert all("speed_source" not in d for _, _, d in cached_graph.edges(data=True))
