"""Phase 9c: build the dashboard's JSON bundle.

Numbers come from the committed Parquet; route geometry is re-run, because
the Parquet stores `path_hops` but not the paths. The two are labelled
separately in the bundle and on the page -- see `viz/export.py`.

Run:
    .venv/Scripts/python.exe scripts/05_export_web.py
    .venv/Scripts/python.exe scripts/05_export_web.py --scenarios S4 --limit 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bioroute.viz import export, stats, traces  # noqa: E402

DEFAULT_OUT = ROOT / "web" / "data" / "bundle.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument(
        "--scenarios", default="S1,S4,S6",
        help="disrupted scenarios to regenerate routes for (comma-separated). "
             "Each bundle carries BOTH the before- and after-closure paths, "
             "so no separate S0 pass is needed.",
    )
    ap.add_argument("--level", default="heavy", choices=["light", "heavy"])
    ap.add_argument("--limit", type=int, default=0,
                    help="only the first N OD pairs (0 = all 30); for smoke runs")
    ap.add_argument("--no-congestion", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]

    print("loading measured statistics from the committed Parquet ...", flush=True)
    df = stats.load_results()
    stat_bundle = stats.compute_all(df)
    print(f"  stats computed over {len(df):,} rows", flush=True)

    print("loading sweep context (graph + snapshots + OD draw) ...", flush=True)
    ctx = traces.load_context(verbose=True)
    od_pairs = list(ctx.od_pairs)
    if args.limit:
        od_pairs = od_pairs[: args.limit]

    print(f"regenerating routes: {len(od_pairs)} OD pairs x {len(scenarios)} "
          f"scenario(s) at {args.level} ...", flush=True)
    bundles, skipped = [], []
    total = len(od_pairs) * len(scenarios)
    done = 0
    for od in od_pairs:
        for sid in scenarios:
            done += 1
            try:
                b = traces.collect_routes(
                    ctx, od, sid, level=args.level, warm=True, verbose=False
                )
                bundles.append(b)
            except Exception as exc:  # noqa: BLE001
                # Phase 7 can legitimately fail to cut a scenario without
                # disconnecting the pair (28 such rows exist in the sweep
                # itself). Record it; never drop it silently.
                skipped.append(
                    {"od": [od.source, od.target], "scenario": sid,
                     "reason": f"{type(exc).__name__}: {exc}"}
                )
            print(f"  [{done}/{total}] {od.source}->{od.target} {sid} "
                  f"({time.time()-t0:.0f}s)", flush=True)

    congestion = None
    if not args.no_congestion:
        print("computing per-edge congestion ratios ...", flush=True)
        congestion = traces.congestion_bundle(ctx)

    print("extracting network geometry ...", flush=True)
    network = export.network_geometry(ctx.graph)
    print(f"  {network['n_nodes']:,} nodes / {network['n_edges']:,} edges", flush=True)

    bundle = export.build_bundle(
        stats=stat_bundle,
        network=network,
        od_pairs=od_pairs,
        routes=export.route_payload(bundles),
        congestion=congestion,
        traces=None,
        meta={
            "config_hash": ctx.cfg.config_hash,
            "baseline_graph": ctx.meta.get("baseline"),
            "baseline_hash": ctx.meta.get("baseline_hash"),
            "traffic_level": args.level,
            "scenarios_regenerated": scenarios,
            "n_od_pairs": len(od_pairs),
            "n_route_cells": len(bundles),
            "skipped_cells": skipped,
        },
    )

    path, size = export.write_bundle(args.out, bundle)
    print(f"\nbundle: {path}  ({size/1e6:.2f} MB)", flush=True)
    if skipped:
        print(f"skipped {len(skipped)} cell(s) (recorded in the bundle):", flush=True)
        for s in skipped:
            print(f"  {s['od']} {s['scenario']}: {s['reason']}", flush=True)
    print(f"elapsed {time.time()-t0:.0f}s", flush=True)
    print(json.dumps({"bundle_mb": round(size/1e6, 3),
                      "route_cells": len(bundles),
                      "skipped": len(skipped)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
