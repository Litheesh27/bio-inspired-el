"""Phase 8: run the experiment matrix.

    .venv/Scripts/python.exe scripts/03_run_experiments.py --dry-run
    .venv/Scripts/python.exe scripts/03_run_experiments.py --smoke
    .venv/Scripts/python.exe scripts/03_run_experiments.py \
        --config configs/experiments/disruption.yaml --workers 14 --resume

What this script does, in order:

  1. loads the experiment config and the cached baseline graph;
  2. draws the 30 stratified OD pairs and PRINTS the overlap check against
     the already-spent draws (ACO tuning seed 2024, Phase 4d seed 4242) --
     printed, not asserted away, because "out-of-sample" is a claim the
     results rest on;
  3. builds the matrix as data and prints the cell/run counts;
  4. with --dry-run, prints the projected cost and STOPS;
  5. otherwise pre-warms every congestion snapshot SERIALLY (CLAUDE.md:
     this must happen before any parallel section -- Phase 4c lost a
     12-worker run to a cache race), then runs the sweep with joblib,
     one shard per worker, merging at the end.

A note on the numbers this prints. Elapsed wall-clock from a parallel run
is NOT a performance measurement: this machine's throughput swings up to
3.4x with thermal state (CLAUDE.md, Phase 4b). It is printed for progress
and planning only, and the per-algorithm timings that reach the Parquet
file are per-solve measurements taken under contention.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bioroute.config import config_hash, load_area_config  # noqa: E402
from bioroute.experiments.matrix import (  # noqa: E402
    build_matrix,
    load_experiment_config,
    matrix_summary,
    od_overlap_report,
    project_cost,
    sample_od_pairs,
    with_area_hash,
)
from bioroute.experiments.runner import (  # noqa: E402
    completed_run_ids,
    filter_completed,
    load_sweep_graph,
    measured_seconds_per_solve,
    prepare_resources,
    run_sweep,
)

DEFAULT_CONFIG = "configs/experiments/disruption.yaml"


def _fmt_hours(hours: float) -> str:
    if hours < 1.0:
        return f"{hours * 60:.1f} min"
    return f"{hours:.2f} h"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="experiment YAML")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="apply the config's `smoke:` overrides (small OD sample, scenario "
        "subset, tiny budgets) and run end to end",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip runs already present in the shard directory",
    )
    parser.add_argument("--workers", type=int, default=None, help="joblib workers")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the cell count and the projected cost, run nothing",
    )
    parser.add_argument(
        "--cost-from",
        default=None,
        help="a finished results Parquet (e.g. a smoke run) whose measured "
        "per-solve medians replace the config's cost estimates in --dry-run",
    )
    parser.add_argument(
        "--only-algorithms",
        default=None,
        help="comma-separated algorithm names to run THIS invocation (e.g. 'dijkstra,astar,physarum'). Filters cells AFTER the matrix is built, so the config hash and therefore the shard directory are UNCHANGED -- the algorithms left out stay resumable into the same dataset later. ACO is ~97%% of this sweep's compute, so this is how you get the other three in minutes.",
    )
    parser.add_argument("--no-progress", action="store_true", help="disable the progress bar")
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="write shards but skip the final merge (for interrupted experiments)",
    )
    args = parser.parse_args(argv)

    cfg = load_experiment_config(ROOT / args.config, smoke=args.smoke)
    area_cfg = load_area_config(ROOT / cfg.area_config_path)
    cfg = with_area_hash(cfg, config_hash(area_cfg))
    workers = args.workers if args.workers is not None else cfg.workers

    print(f"=== Phase 8 experiment runner: {cfg.name} ===")
    print(f"config           : {args.config}{' (+smoke overrides)' if args.smoke else ''}")
    print(f"config hash      : {cfg.config_hash}")
    print(f"baseline graph   : {cfg.baseline_graph}")
    print(f"conditions       : {[str(c) for c in cfg.conditions]}")
    print(f"scenarios        : {list(cfg.scenarios)}")
    for spec in cfg.algorithms:
        print(
            f"  algorithm {spec.name:<9} kind={spec.kind:<9} seeds={list(spec.seeds)} "
            f"budget={spec.budget} modes={'cold only' if spec.is_classical else 'warm+cold'}"
        )

    # -- the graph, for OD sampling (and, below, for the sweep itself) ----
    graph, info = load_sweep_graph(cfg, area_cfg)
    print(
        f"graph            : {graph.number_of_nodes()} nodes / "
        f"{graph.number_of_edges()} edges  [{info['baseline']}]"
    )

    od_pairs = sample_od_pairs(graph, cfg.od)
    overlap = od_overlap_report(od_pairs, graph)
    print(
        f"OD sample        : {len(od_pairs)} pairs, seed {cfg.od.seed}, strata "
        + ", ".join(
            f"{name}={sum(1 for p in od_pairs if p.stratum == name)}"
            for name in cfg.od.strata
        )
    )
    for name in cfg.od.strata:
        band = [p.distance_m for p in od_pairs if p.stratum == name]
        if band:
            print(
                f"  {name:<7} straight-line {min(band):7.0f}-{max(band):7.0f} m "
                f"(median {sorted(band)[len(band) // 2]:.0f} m)"
            )
    print(
        "OD out-of-sample : ordered overlap "
        f"{overlap['ordered_overlap_total']}, unordered overlap "
        f"{overlap['unordered_overlap_total']} "
        f"(vs. tuning seed 2024 and Phase 4d seed 4242)"
    )
    for seed, entry in overlap["by_draw"].items():
        print(
            f"  vs draw {seed}: ordered={entry['ordered_overlap']} "
            f"unordered={entry['unordered_overlap']} "
            f"shared endpoint nodes={entry['shared_endpoint_nodes']}"
        )
    if overlap["ordered_overlap_total"] or overlap["unordered_overlap_total"]:
        print("ERROR: OD sample is NOT disjoint from the spent draws. Refusing to run.")
        return 2

    cells = build_matrix(cfg, od_pairs)
    summary = matrix_summary(cells)
    print(f"matrix           : {summary['n_cells']} cells, {summary['n_runs']} runs")
    if summary["n_unique_run_ids"] != summary["n_runs"]:
        print(
            f"ERROR: {summary['n_runs'] - summary['n_unique_run_ids']} duplicate run_ids "
            "in the matrix. Refusing to run."
        )
        return 2
    if args.only_algorithms:
        wanted = {a.strip() for a in args.only_algorithms.split(",") if a.strip()}
        known = {r.algorithm for c in cells for r in c.runs}
        unknown = wanted - known
        if unknown:
            print(f"ERROR: --only-algorithms names unknown algorithms: {sorted(unknown)}. "
                  f"Known: {sorted(known)}. Refusing to run.")
            return 2
        kept = []
        for c in cells:
            runs = tuple(r for r in c.runs if r.algorithm in wanted)
            if runs:
                kept.append(dataclasses.replace(c, runs=runs))
        n_before = summary["n_runs"]
        cells = kept
        summary = matrix_summary(cells)
        print(f"  only-algorithms: {sorted(wanted)} -> {summary['n_cells']} cells, "
              f"{summary['n_runs']} runs (of {n_before}); the rest stay resumable "
              "into this same shard dir")
    print(f"  by algorithm   : {summary['runs_by_algorithm']}")
    print(f"  by scenario    : {summary['runs_by_scenario']}")
    print(f"  by condition   : {summary['runs_by_condition']}")
    print(f"  by mode        : {summary['runs_by_mode']}")

    shard_dir = cfg.shard_dir
    skipped = 0
    if args.resume:
        done = completed_run_ids(shard_dir)
        cells, skipped = filter_completed(cells, done)
        print(
            f"resume           : {len(done)} run_ids already on disk, "
            f"{skipped} runs skipped, {len(cells)} cells remaining"
        )

    # -- projection ------------------------------------------------------
    estimates = dict(cfg.cost_estimate_s_per_solve)
    source_note = "config estimates"
    if args.cost_from:
        measured = measured_seconds_per_solve(args.cost_from)
        estimates.update(measured)
        source_note = f"measured from {args.cost_from}"
    projection = project_cost(cells, estimates, workers=workers)
    print(f"projection       : seconds/solve ({source_note}) {projection['seconds_per_solve_used']}")
    print(f"  solves         : {projection['n_solves_by_algorithm']}")
    for algo, secs in projection["serial_seconds_by_algorithm"].items():
        print(f"    {algo:<9} {_fmt_hours(secs / 3600.0):>10} serial")
    print(f"  SERIAL total   : {_fmt_hours(projection['serial_hours'])}")
    print(
        f"  at {projection['workers']} workers : "
        f"{_fmt_hours(projection['parallel_hours_lower_bound'])} (LOWER BOUND -- perfect "
        "scaling assumed; this machine's throughput swings up to 3.4x with thermal "
        "state, so treat it as planning only, never as a performance figure)"
    )

    if args.dry_run:
        print("\n--dry-run: nothing executed.")
        return 0

    if not cells:
        print("\nNothing left to run.")
        return 0

    # -- resources: SERIAL pre-warm BEFORE any parallel section -----------
    print("\npre-warming congestion snapshots serially (CLAUDE.md Phase 4d) ...")
    bundle_dir = shard_dir / "bundle"
    resources, spec, meta = prepare_resources(cfg, bundle_dir)
    print(f"  snapshot bundle: {len(spec.snapshot_paths)} file(s) in {bundle_dir}")

    print(f"\nrunning {len(cells)} cells on {workers} worker(s) ...")
    result = run_sweep(
        cfg,
        cells,
        resources=resources,
        spec=spec,
        workers=workers,
        shard_dir=shard_dir,
        progress=not args.no_progress,
        merge=not args.no_merge,
        extra_meta=meta,
    )

    print("\n=== summary ===")
    print(f"cells run        : {result['cells_run']}")
    print(f"runs attempted   : {result['runs_attempted']} (+{skipped} skipped by resume)")
    print(f"runs failed      : {result['runs_failed']}  ({result['failed_rate'] * 100:.2f}%)")
    if result.get("disruption_infeasible"):
        print(
            f"infeasible cells : {result['disruption_infeasible']} "
            f"(scenario could not preserve connectivity)"
        )
    print(f"rejections total : {result.get('disruption_rejections_total', 0)} "
          "(Phase 7 connectivity resamples -- a network-fragility result)")
    print(
        f"wall-clock       : {result['wallclock_s'] / 60:.1f} min at {result['workers']} "
        "worker(s)  [NOT a performance figure -- thermal variation up to 3.4x]"
    )
    if "output_path" in result:
        print(f"output           : {result['output_path']}")
        print(
            f"rows in output   : {result['rows_in_output']} "
            f"({result['failed_rows_in_output']} failed, "
            f"{result['failed_rate_in_output'] * 100:.2f}%)"
        )
        bar = 0.05
        print(
            "exit criterion   : "
            + ("PASS" if result["failed_rate_in_output"] < bar else "MISS")
            + f" (failed rate {'<' if result['failed_rate_in_output'] < bar else '>='} 5%)"
        )
    if result["cell_errors"]:
        print(f"cell errors      : {len(result['cell_errors'])}")
        for entry in result["cell_errors"][:5]:
            print(f"  {entry['cell']}: {entry['error'][:200]}")
    print(f"shards           : {result['shard_dir']}")
    print(json.dumps({"config_hash": result["config_hash"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
