"""Phase 1 tests: config hashing, GraphML round-trip, connectivity, and edge
attribute sanity on the cached study-area graph.
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
