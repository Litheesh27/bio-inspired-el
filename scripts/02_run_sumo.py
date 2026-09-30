"""Phase 2b/2c/2d entrypoint: build the SUMO pipeline, build the shared
free-flow baseline graph, and report the SUMO<->OSMnx edge-ID join coverage.

Usage:
    SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe scripts/02_run_sumo.py \
        --config configs/area_central_blr.yaml [--skip-pipeline]

By default this (re)runs the full pipeline: netconvert -> demand generation
(randomTrips.py + duarouter validation) for light and heavy levels ->
headless sumo with 300s edgeData bins. Pass --skip-pipeline to reuse
whatever is already on disk under data/sumo/ (e.g. after editing only
network/ids.py) and go straight to the baseline build + join + coverage
report.

PHASE 2D. Two things were added here.

1. The shared free-flow baseline (``--baseline``). SUMO derives its own
   free-flow speeds from OSM/netconvert defaults, which are faster than the
   conservative Bengaluru ``speeds_kph`` table the Phase 1 graph uses, so
   SUMO *congested* times could come out below the graph's *free-flow*
   times and SUMO/BPR ratios had different denominators. This script now
   re-annotates the cached graph's ``speed_kph``/``travel_time`` from
   data/sumo/net.net.xml (via the same Phase 2c edge-ID join) and caches
   that as a SEPARATE artifact with its own hash. Phase 1 is deliberately
   NOT made to depend on SUMO: the re-annotation happens here, downstream
   of netconvert, so the graph can still be built without SUMO installed.

2. Congestion-ratio reporting against that one baseline, so SUMO's and
   BPR's numbers are finally comparable line for line.

Prints: netconvert node/edge counts and warning summary, per-level vehicle
counts / teleports / zero-traffic fraction, the baseline re-annotation
coverage, edge-match coverage (ID-based, then after geometric fallback if
triggered), the per-level congestion ratios, and the exit-criterion verdict
(>=85% of graph edges matched).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml

from bioroute.config import load_area_config
from bioroute.congestion.sumo import (
    SUMOProvider,
    generate_demand,
    resolve_sumo_home,
    run_netconvert,
    run_simulation,
    write_edgedata_additional,
)
from bioroute.network.graph import (
    BASELINE_CONFIG_SPEEDS,
    BASELINE_SUMO_SPEEDS,
    build_sumo_speeds_graph,
    cache_path,
    load_baseline_graph,
    load_cached_graph,
)


def _load_yaml(path: str | Path) -> dict:
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
        inserted = sim_stats.get("inserted") or 0
        teleports = sim_stats.get("teleports") or 0
        print(f"  {sim_stats}")
        if inserted:
            # Phase 2d's headline calibration number: a teleport is a
            # vehicle that jumped past a blockage instead of driving
            # through it, so its travel times on the edges it skipped are
            # fiction. Target is <~5% of inserted vehicles.
            print(
                f"  teleports = {100.0 * teleports / inserted:.2f}% of inserted; "
                f"still running at sim end = "
                f"{100.0 * (sim_stats.get('running_at_end') or 0) / inserted:.2f}%"
            )

    return report


def build_baseline(area_cfg, sumo_cfg: dict, force: bool) -> None:
    """PHASE 2D step: re-annotate the cached Phase 1 graph's free-flow
    speeds from the SUMO net and cache it as its own artifact."""
    g_path = cache_path(area_cfg)
    if not g_path.exists():
        raise FileNotFoundError(
            f"Cached graph not found at {g_path}; run scripts/01_build_network.py first."
        )
    G_cfg = load_cached_graph(g_path)
    print("=" * 70)
    print("Phase 2d: shared free-flow baseline (SUMO-speed re-annotation)")
    print("-" * 70)
    G_sumo, rep, was_cached = build_sumo_speeds_graph(
        area_cfg,
        G_cfg,
        net_path="data/sumo/net.net.xml",
        netconvert_flags=list(sumo_cfg["netconvert_flags"]),
        force=force,
    )
    if was_cached:
        print(f"  cached artifact reused: {G_sumo.graph.get('baseline_hash')}")
        return
    print(f"  graph edges: {rep['n_graph_edges']}")
    print(
        f"  re-annotated from SUMO speeds: {rep['n_edges_sumo_speed']} "
        f"({rep['coverage_pct']:.2f}%)"
    )
    print(f"  kept config-derived speed (no SUMO match): {rep['n_edges_kept_config_speed']}")
    print(
        f"  mean speed_kph: {rep['mean_speed_kph_before']:.2f} -> "
        f"{rep['mean_speed_kph_after']:.2f}"
    )
    print(
        f"  total free-flow time: {rep['total_freeflow_time_s_before']:.0f}s -> "
        f"{rep['total_freeflow_time_s_after']:.0f}s "
        f"(x{rep['total_freeflow_time_ratio_after_over_before']:.4f})"
    )


def congestion_ratios(G, snapshot) -> dict:
    """Mean congested/free-flow travel-time ratio of a snapshot against the
    free-flow ``travel_time`` of the graph it was produced on. Reported both
    length-weighted (the number a router actually experiences on long roads)
    and unweighted (one vote per edge), because they can differ a lot when
    congestion concentrates on short arterial links."""
    num = den = 0.0
    vals = []
    for u, v, k, data in G.edges(keys=True, data=True):
        ff = float(data["travel_time"])
        tt = snapshot.travel_time.get((u, v, k))
        if tt is None or ff <= 0:
            continue
        r = tt / ff
        vals.append(r)
        length = float(data["length"])
        num += r * length
        den += length
    vals.sort()
    return {
        "n_edges": len(vals),
        "ratio_length_weighted": num / den if den else float("nan"),
        "ratio_unweighted": sum(vals) / len(vals) if vals else float("nan"),
        "ratio_median": vals[len(vals) // 2] if vals else float("nan"),
        "ratio_p95": vals[int(0.95 * len(vals))] if vals else float("nan"),
        "ratio_max": vals[-1] if vals else float("nan"),
        "frac_edges_below_freeflow": (
            sum(1 for r in vals if r < 1.0) / len(vals) if vals else float("nan")
        ),
    }


def print_join_report(provider: SUMOProvider, G) -> bool:
    print("=" * 70)
    print("SUMO <-> OSMnx edge-ID join report")
    print("-" * 70)
    print(f"Baseline graph in use: {G.graph.get('baseline', BASELINE_CONFIG_SPEEDS)}")
    print(f"ID-based coverage: {provider.id_only_coverage_pct():.2f}%")
    cov = provider.coverage_report()
    if cov.get("coverage_pct", provider.id_only_coverage_pct()) != provider.id_only_coverage_pct():
        print(f"Coverage after geometric fallback: {cov['coverage_pct']:.2f}%")
    print(f"Graph edges: {cov['n_graph_edges']}")
    print(
        f"Matched: {cov['n_matched_edges']}  "
        f"Unmatched (free-flow fallback): {cov['n_unmatched_edges']}"
    )
    print(f"Ambiguous-direction edges (both directions assigned): {cov['n_ambiguous_direction_edges']}")
    print(
        f"Matched via node-ID: {cov['n_matched_via_node_id']}  "
        f"via sign (confirmed): {cov['n_matched_via_sign_confirmed']}  "
        f"via sign (one-way, unpaired): {cov['n_matched_via_sign_unpaired']}  "
        f"via sign (ambiguous both dirs): {cov['n_matched_via_sign_ambiguous']}"
    )
    print(f"SUMO edges total: {cov['n_sumo_edges_total']}  unassigned: {cov['n_sumo_edges_unassigned']}")

    fallback_by_level = cov.get("n_fallback_no_traveltime_observation_by_level", {})
    for level in provider.levels:
        edr = provider.edgedata_report(level)
        print(
            f"[{level}] graph edges falling back to free-flow (no traveltime "
            f"observation on any matched SUMO edge): {fallback_by_level.get(level)}"
        )
        print(
            f"[{level}] SUMO edges with >=1 vehicle observation: "
            f"{edr['n_edges_with_traffic']}/{edr['n_edges_total']} "
            f"(zero-traffic fraction {edr['frac_zero_traffic']:.3f})"
        )
        # Phase 2d: make the implausible-value clipping visible. If this
        # reads 0, the cap is a no-op on this run -- which is the expected
        # outcome once teleports are rare. The guard stays regardless.
        print(
            f"[{level}] implausible-value clipping (cap = {edr['cap_factor']}x "
            f"free-flow): {edr['n_samples_clipped']}/{edr['n_interval_samples']} "
            f"interval samples clipped ({100.0 * edr['frac_samples_clipped']:.3f}%), "
            f"max observed sample = {edr['max_sample_ratio_to_freeflow']:.1f}x free-flow"
        )
        ratios = congestion_ratios(G, provider.snapshot(level))
        print(
            f"[{level}] congestion ratio vs this baseline: "
            f"length-weighted {ratios['ratio_length_weighted']:.3f}x, "
            f"unweighted {ratios['ratio_unweighted']:.3f}x, "
            f"median {ratios['ratio_median']:.3f}x, p95 {ratios['ratio_p95']:.3f}x, "
            f"{100.0 * ratios['frac_edges_below_freeflow']:.1f}% of edges below free-flow"
        )

    ok = cov["coverage_pct"] >= 85.0
    print("-" * 70)
    print(f"Exit criterion (>=85% graph edges matched): {'PASS' if ok else 'FAIL'} ({cov['coverage_pct']:.2f}%)")
    print("=" * 70)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Phase 2b/2c/2d: SUMO pipeline, shared baseline graph, and edge-ID join."
    )
    parser.add_argument("--config", default="configs/area_central_blr.yaml")
    parser.add_argument("--sumo-config", default="configs/sumo.yaml")
    parser.add_argument("--congestion-config", default="configs/congestion.yaml")
    parser.add_argument(
        "--skip-pipeline",
        action="store_true",
        help="Skip netconvert/demand/simulation; reuse data/sumo/* already on disk.",
    )
    parser.add_argument(
        "--baseline",
        choices=[BASELINE_CONFIG_SPEEDS, BASELINE_SUMO_SPEEDS],
        default=None,
        help=(
            "Free-flow baseline graph to report against. Defaults to "
            "configs/congestion.yaml's `baseline_graph`."
        ),
    )
    parser.add_argument(
        "--force-baseline",
        action="store_true",
        help="Rebuild the sumo-speeds baseline graph even if its cached artifact exists.",
    )
    args = parser.parse_args()

    area_cfg = load_area_config(args.config)
    sumo_cfg = _load_yaml(args.sumo_config)
    cong_cfg = _load_yaml(args.congestion_config)
    baseline = args.baseline or cong_cfg.get("baseline_graph", BASELINE_CONFIG_SPEEDS)
    sumo_home = resolve_sumo_home()

    if not args.skip_pipeline:
        run_pipeline(sumo_cfg, sumo_home)
    else:
        print("[02_run_sumo] --skip-pipeline: reusing data/sumo/* as-is")

    # The sumo-speeds artifact is (re)built unconditionally, even when the
    # report is requested against config_speeds: it is cheap given a warm
    # net, and Phase 8 should never be the first thing to discover it is
    # missing.
    build_baseline(area_cfg, sumo_cfg, force=args.force_baseline)

    G, info = load_baseline_graph(
        area_cfg,
        baseline=baseline,
        net_path="data/sumo/net.net.xml",
        netconvert_flags=list(sumo_cfg["netconvert_flags"]),
    )
    print(f"[02_run_sumo] Baseline graph: {info['baseline']} ({info['path']})")

    provider = SUMOProvider(G, area_cfg, sumo_cfg_path=args.sumo_config)
    ok = print_join_report(provider, G)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
