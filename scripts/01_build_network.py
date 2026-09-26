"""Phase 1 entrypoint: acquire, normalize, and cache the study-area graph.

Usage:
    .venv/Scripts/python.exe scripts/01_build_network.py \
        --config configs/area_central_blr.yaml [--force]

Prints a network report: node count, edge count, total road km, mean/median
edge length, degree distribution, number of SCCs before pruning and how many
nodes were dropped, and count of edges using default vs. tagged speeds.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

# Allow running directly from a checkout without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import networkx as nx

from bioroute.config import load_area_config, config_hash
from bioroute.network.acquire import fetch_raw_osm_xml, fetch_osmnx_graph
from bioroute.network.graph import (
    cache_path,
    build_graph,
    save_graph,
    load_cached_graph,
)


def print_report(G: nx.MultiDiGraph, report_stats: dict | None, cfg, h: str, was_cached: bool) -> None:
    n_nodes = G.number_of_nodes()
    n_edges = G.number_of_edges()
    lengths = [d["length"] for _, _, d in G.edges(data=True)]
    total_km = sum(lengths) / 1000.0
    mean_len = statistics.mean(lengths)
    median_len = statistics.median(lengths)
    degrees = [deg for _, deg in G.degree()]
    mean_deg = statistics.mean(degrees)
    median_deg = statistics.median(degrees)
    deg_min, deg_max = min(degrees), max(degrees)

    print("=" * 70)
    print(f"Network report: {cfg.name}  (config_hash={h})")
    print(f"Source: {'cache' if was_cached else 'fresh build'}  -> {cache_path(cfg)}")
    print("-" * 70)
    print(f"Nodes: {n_nodes}")
    print(f"Edges: {n_edges}")
    print(f"Total road length: {total_km:.2f} km")
    print(f"Edge length (m): mean={mean_len:.1f}  median={median_len:.1f}")
    print(f"Degree: mean={mean_deg:.2f}  median={median_deg:.1f}  min={deg_min}  max={deg_max}")
    if report_stats is not None:
        print("-" * 70)
        print(f"Raw fetch: {report_stats['n_nodes_raw']} nodes, {report_stats['n_edges_raw']} edges")
        print(f"Strongly connected components found: {report_stats['n_sccs']}")
        print(f"Nodes dropped (not in largest SCC): {report_stats['n_nodes_dropped']}")
        print(
            f"Edge speeds: {report_stats['n_edges_tagged_speed']} tagged (OSM maxspeed), "
            f"{report_stats['n_edges_default_speed']} default (config fallback)"
        )
    else:
        print("-" * 70)
        print("(Loaded from cache: raw-fetch/SCC-pruning stats not recomputed.)")

    in_range = 2000 <= n_nodes <= 8000
    print("-" * 70)
    print(f"Exit criterion (2,000-8,000 nodes): {'PASS' if in_range else 'FAIL'} ({n_nodes} nodes)")
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build/cache the Phase 1 study-area graph.")
    parser.add_argument("--config", default="configs/area_central_blr.yaml")
    parser.add_argument("--force", action="store_true", help="Ignore cache; refetch and rebuild.")
    args = parser.parse_args()

    cfg = load_area_config(args.config)
    h = config_hash(cfg)
    graphml_path = cache_path(cfg)

    if graphml_path.exists() and not args.force:
        print(f"[01_build_network] Loading cached graph from {graphml_path}")
        G = load_cached_graph(graphml_path)
        print_report(G, None, cfg, h, was_cached=True)
        return 0

    print(f"[01_build_network] Building graph for '{cfg.name}' (config_hash={h})")

    raw_xml_path = Path("data/raw") / f"{cfg.name}.osm.xml"
    fetch_raw_osm_xml(cfg, raw_xml_path, force=args.force)

    G_raw = fetch_osmnx_graph(cfg)
    G, report_stats = build_graph(cfg, G_raw)
    save_graph(G, graphml_path)

    print_report(G, report_stats, cfg, h, was_cached=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
