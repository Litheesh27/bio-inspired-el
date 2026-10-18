"""Phase 9b -- regenerate figures 1, 5 and 6, which the Parquet cannot carry.

    .venv/Scripts/python.exe scripts/06_make_traces.py
    .venv/Scripts/python.exe scripts/06_make_traces.py --figures 6
    .venv/Scripts/python.exe scripts/06_make_traces.py --from-artifacts

Three of Phase 9's figures have no source column in
`data/results/final_<confighash>.parquet`:

  figure 1  convergence (cost vs iteration) -- the Parquet is one flat row
            per run and holds no per-iteration trace;
  figure 5  routes before/after a closure   -- it holds `path_hops`, not
            the paths;
  figure 6  congestion light vs heavy       -- a `TrafficSnapshot`
            property, not a result-row property.

This script therefore RE-RUNS a small, explicitly chosen, seeded subset
under the sweep's own config, graph and snapshots, writes the regenerated
artifacts to `data/results/traces/`, renders the three figures, and
CROSS-CHECKS the re-run against the matching Parquet cells.

**Everything it produces is illustrative. It is not the sweep's measured
evidence, and no statistic may be quoted from it.** `scripts/04_make_figures.py`
(Phase 9a) owns figures 2, 3, 4 and 7, all of which do come from the
Parquet.

`--from-artifacts` re-renders the figures from the JSON already on disk
without recomputing anything, which is why the artifacts are written at
all.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from bioroute.viz import maps, style, traces  # noqa: E402

# ==========================================================================
# THE CHOSEN SUBSET -- stated here, in one place, with its reasons
# ==========================================================================

#: Figure 1: one pair per distance stratum, all members of the sweep's own
#: draw (seed 8080). Chosen from the Parquet for ACO SEED SPREAD on S0 /
#: SUMO heavy -- Phase 4d's point is that a single seed could honestly have
#: reported a pass or a miss, so a convergence figure that showed one seed
#: would misrepresent the method. These three also happen to cover all
#: three of Physarum's extraction-provenance classes, which lets the figure
#: label provenance honestly rather than asserting it.
#:
#:   short   254665681 -> 10019824654   1233 m, 28 hops, ACO 1.000-1.225,
#:                                      Physarum via max_flux_walk
#:   medium  11387476249 -> 11915994482 2532 m, 50 hops, ACO 1.020-1.260,
#:                                      Physarum via theta_subgraph_fallback
#:   long    13247265224 -> 459793811   3512 m, 79 hops, ACO 1.118-1.459,
#:                                      Physarum via full_graph_fallback
FIG1_PAIRS: tuple[tuple[int, int], ...] = (
    (254665681, 10019824654),
    (11387476249, 11915994482),
    (13247265224, 459793811),
)
FIG1_LEVEL = "heavy"

#: Figure 5: the one cell in the whole sweep where all three algorithms
#: genuinely disagree after the closure (post-disruption stretch: Dijkstra
#: and A* 1.000, ACO 1.4218 identical across all five seeds, Physarum
#: 1.6127) AND Physarum's reported path is attributed to `max_flux_walk`,
#: i.e. the model's own extraction rather than a fallback. A cell where
#: three identical lines overlapped would illustrate nothing, and one where
#: Physarum's line was really Dijkstra's would illustrate something false.
#: S4 closes 5 path edges; with both directions and parallel edges that is
#: 8 directed edges here. The pair is in the SHORT stratum (873 m apart)
#: yet needs 42 hops -- the irregular organic topology this project chose
#: the study area for.
FIG5_OD: tuple[int, int] = (429415654, 10038577016)
FIG5_SCENARIO = "S4"
FIG5_LEVEL = "heavy"
FIG5_WARM = True
#: ACO's path is identical on all five seeds in this cell (verified in the
#: Parquet), so one seed is enough for a map and five would be five copies
#: of one line. Seed 0 is the sweep's `seed_start`.
FIG5_SEED = 0

FIGURES_DIR = ROOT / "figures"
TRACE_DIR = ROOT / "data" / "results" / "traces"


def _parquet_path(config_hash: str) -> Path:
    return ROOT / "data" / "results" / f"final_{config_hash}.parquet"


# ==========================================================================
# Regeneration
# ==========================================================================


def regenerate(args) -> dict:
    """Re-run the chosen subset and write the three JSON artifacts."""
    started = time.perf_counter()
    print("Loading the sweep's config, graph and snapshots ...", flush=True)
    ctx = traces.load_context(args.config, bundle_dir=TRACE_DIR, verbose=True)

    parquet = _parquet_path(ctx.config_hash)
    if not parquet.exists():
        print(f"  WARNING: no Parquet at {parquet}; cross-check will be skipped")

    written: dict[str, Path] = {}
    checks: list[traces.CrossCheck] = []
    timings: dict[str, float] = {}

    # -- figure 1 artifacts -------------------------------------------------
    if 1 in args.figures:
        t0 = time.perf_counter()
        pairs = [ctx.od(s, t) for s, t in FIG1_PAIRS]
        print(
            f"\n[fig 1] convergence traces: {len(pairs)} OD pairs, "
            f"SUMO {FIG1_LEVEL}, S0",
            flush=True,
        )
        convergence = traces.collect_convergence_set(
            ctx, pairs, level=FIG1_LEVEL, verbose=True
        )
        written["traces"] = traces.write_traces(ctx, convergence, TRACE_DIR)
        timings["fig1_regen_s"] = time.perf_counter() - t0
        if parquet.exists():
            checks += traces.crosscheck_traces(convergence, parquet)

    # -- figure 5 artifacts -------------------------------------------------
    if 5 in args.figures:
        t0 = time.perf_counter()
        od = ctx.od(*FIG5_OD)
        print(
            f"\n[fig 5] routes before/after {FIG5_SCENARIO}: {od.stratum} pair "
            f"{od.source}->{od.target}, SUMO {FIG5_LEVEL}, "
            f"{'warm' if FIG5_WARM else 'cold'}",
            flush=True,
        )
        bundle = traces.collect_routes(
            ctx,
            od,
            FIG5_SCENARIO,
            level=FIG5_LEVEL,
            warm=FIG5_WARM,
            seed=FIG5_SEED,
            verbose=True,
        )
        written["routes"] = traces.write_routes(ctx, [bundle], TRACE_DIR)
        timings["fig5_regen_s"] = time.perf_counter() - t0
        if parquet.exists():
            checks += traces.crosscheck_routes(bundle, parquet)

    # -- figure 6 artifacts -------------------------------------------------
    if 6 in args.figures:
        t0 = time.perf_counter()
        print("\n[fig 6] congestion ratios, light and heavy", flush=True)
        congestion = traces.congestion_bundle(ctx)
        for level, s in congestion["summary"].items():
            print(
                f"    {level}: {s['n_edges']} edges, median {s['median']:.3f}x, "
                f"p90 {s['p90']:.3f}x, max {s['max']:.1f}x, "
                f"{s['share_at_freeflow']:.1%} at free-flow",
                flush=True,
            )
        written["congestion"] = traces.write_congestion(ctx, congestion, TRACE_DIR)
        timings["fig6_regen_s"] = time.perf_counter() - t0

    # -- cross-check --------------------------------------------------------
    print("\nCross-check against the measured Parquet (costs and stretches only;")
    print("wall-clock is NOT compared -- this machine's throughput swings 3.4x):")
    if not checks:
        print("  (skipped)")
    else:
        bad = [c for c in checks if not c.match]
        print(f"  {len(checks) - len(bad)}/{len(checks)} values match to 1e-6")
        for c in bad:
            print(
                f"  MISMATCH  {c.key}  {c.field}: regenerated={c.regenerated!r} "
                f"parquet={c.parquet!r} delta={c.delta!r}"
            )
        if bad:
            print(
                "  *** The illustration and the measured results DISAGREE. "
                "Investigate before using these figures. ***"
            )

    timings["total_regen_s"] = time.perf_counter() - started
    for key, path in written.items():
        print(f"\n  wrote {path}  ({path.stat().st_size / 1024:.1f} KB)")
    print(f"  regeneration took {timings['total_regen_s']:.1f} s")

    return {
        "ctx": ctx,
        "written": {k: str(v) for k, v in written.items()},
        "checks": [
            {
                "key": c.key,
                "field": c.field,
                "regenerated": c.regenerated,
                "parquet": c.parquet,
                "match": c.match,
            }
            for c in checks
        ],
        "timings": timings,
    }


# ==========================================================================
# Rendering
# ==========================================================================


def _traces_from_json(payload: dict) -> list[traces.ConvergenceTrace]:
    return [traces.ConvergenceTrace(**t) for t in payload["traces"]]


def _bundles_from_json(payload: dict) -> list[traces.RouteBundle]:
    out = []
    for raw in payload["bundles"]:
        raw = dict(raw)
        routes = [traces.RouteRecord(**r) for r in raw.pop("routes")]
        out.append(traces.RouteBundle(**raw, routes=routes))
    return out


def _write_caption(outdir: Path, stem: str, text: str) -> Path:
    """Park a figure's caption next to it, for `\\caption{}` in LaTeX.

    The IEEE variants draw no in-figure caption and no suptitle (LaTeX
    sets both, in the column), so the text has to go somewhere -- and
    these three captions carry the standing "ILLUSTRATIVE: regenerated by
    a seeded re-run" warning that must travel with figures 1, 5 and 6.
    """
    import textwrap  # noqa: PLC0415

    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"{stem}.caption.txt"
    path.write_text(
        textwrap.fill(" ".join(text.split()), width=78) + "\n",
        encoding="utf-8",
    )
    return path


def render(args, ctx=None) -> dict[str, list[Path]]:
    """Render figures 1, 5 and 6 from the JSON artifacts on disk."""
    from bioroute.experiments.matrix import load_experiment_config  # noqa: PLC0415

    if ctx is not None:
        config_hash = ctx.config_hash
        graph = ctx.graph
    else:
        # --from-artifacts: the graph is still needed for figures 5 and 6
        # (geometry), but no snapshot and no router is.
        from bioroute.config import config_hash as hash_of  # noqa: PLC0415
        from bioroute.config import load_area_config  # noqa: PLC0415
        from bioroute.experiments.matrix import with_area_hash  # noqa: PLC0415
        from bioroute.experiments.runner import load_sweep_graph  # noqa: PLC0415

        cfg = load_experiment_config(args.config)
        area_cfg = load_area_config(cfg.area_config_path)
        cfg = with_area_hash(cfg, hash_of(area_cfg))
        config_hash = cfg.config_hash
        graph, _info = load_sweep_graph(cfg, area_cfg)

    out: dict[str, list[Path]] = {}
    outdir = args.outdir

    if 1 in args.figures:
        payload = traces.load_json(TRACE_DIR / f"traces_{config_hash}.json")
        series = _traces_from_json(payload)
        out["fig01"] = traces.plot_convergence(
            series, outdir / "fig01_convergence"
        )
        caption = traces.FIG1_CAPTION
        if args.ieee:
            # The IEEE panel titles drop the OD node ids (10-11 digits
            # each on a 2.2 in panel), so the caption has to carry them:
            # without them the cell cannot be re-run.
            pairs = "; ".join(
                f"{t.stratum} {t.od_source}->{t.od_target}"
                for t in sorted(
                    {(t.od_source, t.od_target): t for t in series}.values(),
                    key=lambda t: t.stratum,
                )
            )
            caption += f" OD pairs shown: {pairs}."
            _write_caption(outdir, "fig01_convergence", caption)
        print("\n" + caption)

    if 5 in args.figures:
        payload = traces.load_json(TRACE_DIR / f"routes_{config_hash}.json")
        bundle = _bundles_from_json(payload)[0]
        out["fig05"] = maps.plot_routes_before_after(
            bundle, graph, outdir / "fig05_routes_before_after"
        )
        caption = maps.FIG5_CAPTION.format(
            scenario=bundle.scenario_id,
            source=bundle.od_source,
            target=bundle.od_target,
        )
        if args.ieee:
            # The legend keeps each route's identity and provenance; its
            # per-route stretch and hop counts move here (and are in
            # Table 5 of docs/results/summary.md).
            caption += " Per-route stretch and hop counts before the "
            caption += "closure: " + "; ".join(
                f"{r.algorithm} {r.stretch_before:.3f} / {r.hops_before} hops"
                for r in bundle.routes if r.stretch_before is not None
            ) + "."
            _write_caption(outdir, "fig05_routes_before_after", caption)
        print("\n" + caption)

    if 6 in args.figures:
        payload = traces.load_json(TRACE_DIR / f"congestion_{config_hash}.json")
        written, scale = maps.plot_congestion_heatmap(
            graph,
            payload["ratios"],
            outdir / "fig06_congestion_heatmap",
            summary=payload["summary"],
            levels=payload["levels"],
        )
        out["fig06"] = written
        caption = maps.FIG6_CAPTION.format(
            vmax=scale["vmax"],
            share_light=payload["summary"]["light"]["share_at_freeflow"],
            share_heavy=payload["summary"]["heavy"]["share_at_freeflow"],
        )
        if args.ieee:
            _write_caption(outdir, "fig06_congestion_heatmap", caption)
        print("\n" + caption)

    for name, paths in out.items():
        for path in paths:
            print(f"  wrote {path}  ({path.stat().st_size / 1024:.1f} KB)")
    return out


# ==========================================================================


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="configs/experiments/disruption.yaml",
        help="the SWEEP's experiment config (default: the real one)",
    )
    parser.add_argument(
        "--figures",
        default="1,5,6",
        help="comma-separated subset of {1,5,6} (default: all three)",
    )
    parser.add_argument(
        "--from-artifacts",
        action="store_true",
        help="re-render from the JSON already on disk; recompute nothing",
    )
    parser.add_argument(
        "--no-render",
        action="store_true",
        help="regenerate the artifacts only; do not draw the figures",
    )
    parser.add_argument(
        "--ieee",
        action="store_true",
        help="render the IEEE two-column paper variant into figures/ieee "
             "(7.16 in full width at final size, 8 pt type floor, PDF for "
             "LaTeX). Pair it with --from-artifacts: the IEEE pass is a "
             "re-render and must not recompute a solve.",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=None,
        help="figure output directory (default: figures/, or figures/ieee "
             "with --ieee)",
    )
    args = parser.parse_args(argv)
    style.set_mode("ieee" if args.ieee else "screen")
    if args.outdir is None:
        args.outdir = FIGURES_DIR / "ieee" if args.ieee else FIGURES_DIR
    args.figures = {int(x) for x in str(args.figures).split(",") if x.strip()}
    unknown = args.figures - {1, 5, 6}
    if unknown:
        parser.error(f"Phase 9b owns figures 1, 5 and 6 only; got {sorted(unknown)}")

    started = time.perf_counter()
    result: dict = {}
    ctx = None
    if not args.from_artifacts:
        result = regenerate(args)
        ctx = result["ctx"]

    if not args.no_render:
        print("\nRendering ...", flush=True)
        render(args, ctx)

    elapsed = time.perf_counter() - started
    print(f"\nTotal: {elapsed:.1f} s")

    checks = result.get("checks", [])
    bad = [c for c in checks if not c["match"]]
    print(
        json.dumps(
            {
                "figures": sorted(args.figures),
                "crosscheck_values": len(checks),
                "crosscheck_mismatches": len(bad),
                "seconds": round(elapsed, 1),
            }
        )
    )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
