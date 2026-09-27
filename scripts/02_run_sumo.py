"""Phase 2b/2c entrypoint: build the SUMO pipeline and report the
SUMO<->OSMnx edge-ID join coverage.

Usage:
    SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe scripts/02_run_sumo.py \
        --config configs/area_central_blr.yaml [--skip-pipeline]

By default this (re)runs the full pipeline: netconvert -> demand generation
(randomTrips.py + duarouter validation) for light and heavy levels ->
headless sumo with 300s edgeData bins. Pass --skip-pipeline to reuse
whatever is already on disk under data/sumo/ (e.g. after editing only
network/ids.py) and go straight to the join + coverage report.

Prints: netconvert node/edge counts and warning summary, per-level vehicle
counts and zero-traffic fraction, edge-match coverage (ID-based, then after
geometric fallback if triggered), and the exit-criterion verdict
(>=85% of graph edges matched).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bioroute.config import load_area_config
from bioroute.congestion.sumo import (
    SUMOProvider,
    generate_demand,
    resolve_sumo_home,
    run_netconvert,
    run_simulation,
    write_edgedata_additional,
)
from bioroute.network.graph import cache_path, load_cached_graph
import yaml


def _load_sumo_cfg(path: str | Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_pipeline(sumo_cfg: dict, sumo_home: str) -> dict:
    report: dict = {}

    print(f"[02_run_sumo] SUMO_HOME resolved to: {sumo_home}")
    print("[02_run_sumo] Running netconvert...")
    nc_report = run_netconvert(
        osm_xml_path="data/raw/central_blr.osm.xml",
        net_out_path="data/sumo/net.net.xml",
        extra_flags=list(sumo_cfg["netconvert_flags"]),
        sumo_home=sumo_home,
    )
    report["netconvert"] = nc_report
    print(f"  {nc_report['n_warning_lines']} warning lines")
    for cat, count in sorted(nc_report["rollup_categories"].items(), key=lambda kv: -kv[1]):
        print(f"    {count:5d}  {cat}")

    report["levels"] = {}
    for level in ("light", "heavy"):
        print(f"[02_run_sumo] Generating {level} demand...")
        period = sumo_cfg[f"period_{level}"]
        seed = sumo_cfg[f"seed_{level}"]
        demand_end = sumo_cfg["demand_end"]
        paths = generate_demand(
            net_path="data/sumo/net.net.xml",
            out_dir="data/sumo",
            level=level,
            period=period,
            seed=seed,
            demand_end=demand_end,
            sumo_home=sumo_home,
        )
        add_path = write_edgedata_additional(
            level, out_dir="data/sumo", freq=sumo_cfg["edgedata_freq"]
        )
        print(f"[02_run_sumo] Running {level} simulation (period={period}, seed={seed})...")
        sim_stats = run_simulation(
            net_path="data/sumo/net.net.xml",
            route_path=paths["routes"],
            additional_path=add_path,
            begin=0,
            end=sumo_cfg["sim_end"],
            seed=seed,
            tripinfo_path=f"data/sumo/tripinfo_{level}.xml",
            sumo_home=sumo_home,
        )
        report["levels"][level] = sim_stats
        print(f"  {sim_stats}")

    return report


def print_join_report(provider: SUMOProvider, sumo_cfg: dict) -> bool:
    print("=" * 70)
    print("SUMO <-> OSMnx edge-ID join report")
    print("-" * 70)
    print(f"ID-based coverage: {provider.id_only_coverage_pct():.2f}%")
    cov = provider.coverage_report()
    if cov.get("coverage_pct", provider.id_only_coverage_pct()) != provider.id_only_coverage_pct():
        print(f"Coverage after geometric fallback: {cov['coverage_pct']:.2f}%")
    print(f"Graph edges: {cov['n_graph_edges']}")
    print(f"Matched: {cov['n_matched_edges']}  Unmatched (free-flow fallback): {cov['n_unmatched_edges']}")
    print(f"Ambiguous-direction edges (both directions assigned): {cov['n_ambiguous_direction_edges']}")
    print(
        f"Matched via node-ID: {cov['n_matched_via_node_id']}  "
        f"via sign (confirmed): {cov['n_matched_via_sign_confirmed']}  "
        f"via sign (one-way, unpaired): {cov['n_matched_via_sign_unpaired']}  "
        f"via sign (ambiguous both dirs): {cov['n_matched_via_sign_ambiguous']}"
    )
    print(f"SUMO edges total: {cov['n_sumo_edges_total']}  unassigned: {cov['n_sumo_edges_unassigned']}")

    for level in provider.levels:
        edr = provider.edgedata_report(level)
        print(
            f"[{level}] SUMO edges with >=1 vehicle observation: "
            f"{edr['n_edges_with_traffic']}/{edr['n_edges_total']} "
            f"(zero-traffic fraction {edr['frac_zero_traffic']:.3f})"
        )

    ok = cov["coverage_pct"] >= 85.0
    print("-" * 70)
    print(f"Exit criterion (>=85% graph edges matched): {'PASS' if ok else 'FAIL'} ({cov['coverage_pct']:.2f}%)")
    print("=" * 70)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2b/2c: SUMO pipeline + edge-ID join.")
    parser.add_argument("--config", default="configs/area_central_blr.yaml")
    parser.add_argument("--sumo-config", default="configs/sumo.yaml")
    parser.add_argument(
        "--skip-pipeline",
        action="store_true",
        help="Skip netconvert/demand/simulation; reuse data/sumo/* already on disk.",
    )
    args = parser.parse_args()

    area_cfg = load_area_config(args.config)
    sumo_cfg = _load_sumo_cfg(args.sumo_config)
    sumo_home = resolve_sumo_home()

    if not args.skip_pipeline:
        run_pipeline(sumo_cfg, sumo_home)
    else:
        print("[02_run_sumo] --skip-pipeline: reusing data/sumo/* as-is")

    g_path = cache_path(area_cfg)
    if not g_path.exists():
        print(f"[02_run_sumo] Cached graph not found at {g_path}; run scripts/01_build_network.py first.")
        return 1
    G = load_cached_graph(g_path)

    provider = SUMOProvider(G, area_cfg, sumo_cfg_path=args.sumo_config)
    ok = print_join_report(provider, sumo_cfg)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
