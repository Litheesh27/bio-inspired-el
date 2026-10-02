"""Phase 8c: how accurate is Physarum's OWN path extraction?

Run:
    SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe scripts/measure_physarum_walk.py
    .venv/Scripts/python.exe scripts/measure_physarum_walk.py --report-only

THE QUESTION
--------------------------------------------------------------------------
Phase 8b measured where Physarum's *reported* path comes from and found only
33% of pairs attributing it to the model's own `max_flux_walk`; 53% came from
`theta_subgraph_fallback` and 13% from `full_graph_fallback` (plain
Dijkstra). That figure is a true statement about provenance and a MISLEADING
statement about the walk's accuracy, because the contest it reports is rigged
by two pieces of bookkeeping:

  1. `theta = 0.05` while every conductivity starts at `D_init = 1.0`, so for
     roughly the first 18 iterations the theta subgraph is essentially the
     whole graph -- the "fallback" is Dijkstra, and it sets `best_cost`
     before the flux field has differentiated anything at all.
  2. `best_cost` improves on a STRICT `cost < best_cost`. Once the optimum is
     in, the walk can only TIE, never win. Phase 8b recorded the walk
     succeeding on 115 of 118 iterations and still scoring as a loss.

So the quantity that actually bears on the research question was unmeasured:
**what would Physarum report if the max-flux walk were its only extraction
method?** This script measures exactly that, using the read-only Phase 8c
instrumentation on `PhysarumRouter` (`walk_best_cost()` and friends).

WHAT IT DOES NOT DO
--------------------------------------------------------------------------
It changes NOTHING. `theta` is read as shipped, `best_cost` keeps its pooled
semantics, and the router still reports the path it always reported -- the
walk-only figures are a parallel tracker. A walk that falls short here is a
result about path extraction on irregular real topology, not a gap to tune
away. (Lab-maze Physarum results are usually reported on planar grids where
a greedy flux walk cannot dead-end; a real city graph with one-ways, mean
out-degree 2.5 and cul-de-sacs is a different problem.)

PROTOCOL
--------------------------------------------------------------------------
  * Graph: the Phase 1 cached Central Bengaluru graph, i.e. the SAME graph
    Phase 5's exit criterion and Phase 8b's provenance measurement used, so
    these numbers sit directly beside them.
  * OD draws, BOTH of them:
      - `2024`: the 30 pairs of `tests/test_physarum.py::od_pairs_30`,
        reproduced bit-exactly here (plain `random.Random(2024).sample` with
        an `nx.has_path` filter). In-sample -- it is the Phase 4 tuning draw
        -- and used only for comparability with the published 26/30 and the
        Phase 8b 33%.
      - `8080`: the Phase 8 sweep's own stratified draw
        (`experiments.matrix.sample_od_pairs`), verified disjoint from 2024
        and 4242 by that function itself. This is the draw whose result
        transfers to the sweep.
  * Conditions: `freeflow` and BPR `heavy` (time_bin 1) -- the two Phase 5
    reported on. Snapshots are pre-warmed before anything else touches them
    (Phase 4d cache discipline).
  * Budget: 300 = `configs/physarum.yaml: default_iteration_budget`.
  * Denominator: Dijkstra on the same graph and the same snapshot, per
    CLAUDE.md's stretch invariant.

Results are appended to JSONL after every pair and a re-run resumes from the
file, because a kill mid-measurement must not cost the whole run.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import networkx as nx  # noqa: E402

from bioroute.config import load_area_config  # noqa: E402
from bioroute.network.graph import cache_path, load_cached_graph  # noqa: E402
from bioroute.congestion.base import FreeFlowProvider, prewarm_snapshots  # noqa: E402
from bioroute.congestion.bpr import BPRProvider  # noqa: E402
from bioroute.algorithms.base import run_to_convergence  # noqa: E402
from bioroute.algorithms.classical import DijkstraRouter  # noqa: E402
from bioroute.algorithms.physarum import (  # noqa: E402
    PhysarumRouter,
    PhysarumDegenerateSolve,
    load_physarum_config,
)
from bioroute.experiments.matrix import ODSampleConfig, sample_od_pairs  # noqa: E402

AREA_CONFIG_PATH = ROOT / "configs" / "area_central_blr.yaml"
CONGESTION_CONFIG_PATH = ROOT / "configs" / "congestion.yaml"
PHYSARUM_CONFIG_PATH = ROOT / "configs" / "physarum.yaml"

DEFAULT_OUT = ROOT / "data" / "results" / "phase8c_physarum_walk.jsonl"

#: Exact-match tolerance on stretch. Same 1e-6 Phase 5's exit criterion and
#: Phase 8b used, so "exact" means the same thing in all three places.
EXACT_TOL = 1e-6
#: Tie tolerance between the walk's own best and the pooled best. Tighter,
#: because both are sums over the SAME edge travel times and an identical
#: path is bit-identical; a tie this close means "the walk found the same
#: cost and the strict `<` denied it the credit".
TIE_REL_TOL = 1e-9

STRETCH_BAR = 1.10

DRAWS = ("2024", "8080")
CONDITIONS = ("freeflow", "bpr_heavy")


# --------------------------------------------------------------------------
# OD draws
# --------------------------------------------------------------------------


def draw_2024(G) -> list[dict]:
    """Bit-exact reproduction of `tests/test_physarum.py::od_pairs_30`.

    Duplicated rather than imported for the same reason
    `matrix.legacy_draw_od_pairs` is duplicated: this draw defines which
    pairs the published 26/30 and the Phase 8b 33% were measured on, so it
    must not drift if the fixture is ever refactored. Note it is NOT
    `legacy_draw_od_pairs` -- the Physarum fixture filters on `has_path`,
    which changes the sequence of draws, so the two are different samples.
    """
    rng = random.Random(2024)
    nodes = sorted(G.nodes())
    pairs: list[dict] = []
    attempts = 0
    while len(pairs) < 30 and attempts < 30 * 50:
        attempts += 1
        s, t = rng.sample(nodes, 2)
        if nx.has_path(G, s, t):
            pairs.append({"source": s, "target": t, "stratum": "unstratified"})
    return pairs


def draw_8080(G) -> list[dict]:
    """The Phase 8 sweep's own 30 pairs, via the sweep's own sampler (which
    enforces disjointness from the 2024 and 4242 draws itself)."""
    pairs = sample_od_pairs(G, ODSampleConfig())
    return [
        {"source": p.source, "target": p.target, "stratum": p.stratum}
        for p in pairs
    ]


# --------------------------------------------------------------------------
# One measurement
# --------------------------------------------------------------------------


def measure_pair(G, snapshot, source, target, budget: int) -> dict:
    """Solve one OD pair and report the pooled and walk-only answers side by
    side. Never raises for a run-level problem -- a degenerate solve is
    recorded as `failed`, per CLAUDE.md."""
    d_router = DijkstraRouter()
    d_router.prepare(G, source, target, snapshot, seed=0)
    d_result = run_to_convergence(d_router, budget=1)
    if d_result.cost is None:
        return {"failed": True, "reason": "no dijkstra reference path"}

    p_router = PhysarumRouter(PHYSARUM_CONFIG_PATH)
    p_router.prepare(G, source, target, snapshot, seed=0)
    try:
        p_result = run_to_convergence(p_router, budget=budget)
    except PhysarumDegenerateSolve as exc:
        return {"failed": True, "reason": f"{type(exc).__name__}: {exc}"}

    dijkstra_cost = float(d_result.cost)
    pooled_cost = None if p_result.cost is None else float(p_result.cost)
    walk_cost = p_router.walk_best_cost()
    walk_cost = None if walk_cost is None else float(walk_cost)

    return {
        "failed": False,
        "dijkstra_cost": dijkstra_cost,
        "dijkstra_hops": len(d_result.path) - 1 if d_result.path else None,
        "pooled_cost": pooled_cost,
        "pooled_stretch": None if pooled_cost is None else pooled_cost / dijkstra_cost,
        "pooled_method": p_router.best_path_extraction_method(),
        "walk_cost": walk_cost,
        "walk_stretch": None if walk_cost is None else walk_cost / dijkstra_cost,
        "walk_best_iteration": p_router.walk_best_iteration(),
        "walk_first_success_iteration": p_router.walk_first_success_iteration(),
        "walk_success_count": p_router.walk_success_count(),
        "iterations": p_result.iterations,
        "converged": bool(p_result.converged),
        "last_method": p_router.last_extraction_method(),
        "extraction_counts": p_router.extraction_counts(),
        "mean_solve_time_s": p_router.mean_solve_time_s(),
    }


# --------------------------------------------------------------------------
# Checkpointed sweep
# --------------------------------------------------------------------------


def _key(row: dict) -> tuple:
    return (row["draw"], row["condition"], row["source"], row["target"])


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def run_sweep(out_path: Path, draws: list[str], conditions: list[str], budget: int) -> list[dict]:
    area_cfg = load_area_config(AREA_CONFIG_PATH)
    gpath = cache_path(area_cfg)
    if not gpath.exists():
        raise SystemExit(
            f"Cached graph not found at {gpath}; run scripts/01_build_network.py first."
        )
    G = load_cached_graph(gpath)
    print(f"graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges ({gpath.name})")

    snapshots = {}
    if "freeflow" in conditions:
        print("pre-warming freeflow snapshot ...", flush=True)
        snapshots["freeflow"] = FreeFlowProvider(G).snapshot()
    if "bpr_heavy" in conditions:
        print("pre-warming BPR heavy snapshot ...", flush=True)
        bpr = BPRProvider(G, area_cfg, CONGESTION_CONFIG_PATH)
        snapshots["bpr_heavy"] = prewarm_snapshots(
            bpr, levels=["heavy"], time_bins=[1], verbose=True
        )[("heavy", 1)]

    pair_sets = {}
    if "2024" in draws:
        pair_sets["2024"] = draw_2024(G)
    if "8080" in draws:
        pair_sets["8080"] = draw_8080(G)

    # Disjointness is printed, not assumed (CLAUDE.md Phase 8 discipline).
    if len(pair_sets) == 2:
        a = {(p["source"], p["target"]) for p in pair_sets["2024"]}
        b = {(p["source"], p["target"]) for p in pair_sets["8080"]}
        print(
            f"draw overlap 2024 vs 8080: {len(a & b)} ordered, "
            f"{len({frozenset(p) for p in a} & {frozenset(p) for p in b})} unordered"
        )

    existing = load_rows(out_path)
    done = {_key(r) for r in existing}
    print(f"resume: {len(done)} rows already on disk at {out_path}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(existing)
    todo = [
        (draw, cond, p)
        for draw in pair_sets
        for cond in conditions
        for p in pair_sets[draw]
    ]
    for i, (draw, cond, p) in enumerate(todo, 1):
        row = {
            "draw": draw,
            "condition": cond,
            "source": p["source"],
            "target": p["target"],
            "stratum": p["stratum"],
            "budget": budget,
        }
        if _key(row) in done:
            continue
        row.update(measure_pair(G, snapshots[cond], p["source"], p["target"], budget))
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
            f.flush()
        rows.append(row)
        ws = row.get("walk_stretch")
        ps = row.get("pooled_stretch")
        print(
            f"[{i}/{len(todo)}] {draw}/{cond} {p['source']}->{p['target']}  "
            f"pooled={('%.4f' % ps) if ps else 'NA'} ({row.get('pooled_method')})  "
            f"walk={('%.4f' % ws) if ws else 'NO PATH'} "
            f"@iter {row.get('walk_best_iteration')}  "
            f"iters={row.get('iterations')}",
            flush=True,
        )
    return rows


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def _quantiles(xs: list[float]) -> str:
    if not xs:
        return "n/a"
    xs = sorted(xs)
    def q(f):
        return xs[min(len(xs) - 1, int(f * len(xs)))]
    return (
        f"min={xs[0]:g} p25={q(0.25):g} median={statistics.median(xs):g} "
        f"p75={q(0.75):g} max={xs[-1]:g}"
    )


def report(rows: list[dict]) -> None:
    for draw in DRAWS:
        for cond in CONDITIONS:
            sub = [r for r in rows if r["draw"] == draw and r["condition"] == cond]
            if not sub:
                continue
            print("\n" + "=" * 74)
            print(f"draw {draw}  condition {cond}   n={len(sub)}")
            print("=" * 74)
            failed = [r for r in sub if r.get("failed")]
            ok = [r for r in sub if not r.get("failed")]
            if failed:
                print(f"FAILED runs: {len(failed)}")
                for r in failed:
                    print(f"  {r['source']}->{r['target']}: {r.get('reason')}")
            if not ok:
                continue

            n = len(ok)
            walk_ok = [r for r in ok if r.get("walk_cost") is not None]
            walk_str = [r["walk_stretch"] for r in walk_ok]
            pooled_str = [r["pooled_stretch"] for r in ok if r.get("pooled_stretch")]

            walk_exact = sum(1 for s in walk_str if abs(s - 1.0) < EXACT_TOL)
            pooled_exact = sum(1 for s in pooled_str if abs(s - 1.0) < EXACT_TOL)

            print(
                f"walk produced ANY valid path : {len(walk_ok)}/{n} "
                f"({len(walk_ok)/n:.1%})"
            )
            print(
                f"WALK-ONLY exact match        : {walk_exact}/{n} "
                f"({walk_exact/n:.1%})   [headline]"
            )
            print(
                f"POOLED exact match           : {pooled_exact}/{n} "
                f"({pooled_exact/n:.1%})"
            )
            if walk_str:
                le = sum(1 for s in walk_str if s <= STRETCH_BAR + 1e-12)
                print(
                    f"walk stretch (n={len(walk_str)})   : median="
                    f"{statistics.median(walk_str):.4f} worst={max(walk_str):.4f} "
                    f"<=1.10: {le}/{n} ({le/n:.1%})"
                )
            if pooled_str:
                print(
                    f"pooled stretch (n={len(pooled_str)}) : median="
                    f"{statistics.median(pooled_str):.4f} worst={max(pooled_str):.4f}"
                )

            # The crux: did the walk find the pooled optimum and lose only on
            # the strict `<`?
            ties = [
                r
                for r in walk_ok
                if abs(r["walk_cost"] - r["pooled_cost"])
                <= TIE_REL_TOL * max(1.0, abs(r["pooled_cost"]))
            ]
            fb = [r for r in ok if r.get("pooled_method") != "max_flux_walk"]
            fb_tied = [r for r in ties if r.get("pooled_method") != "max_flux_walk"]
            print(
                f"walk TIES the pooled best    : {len(ties)}/{n} ({len(ties)/n:.1%})"
            )
            print(
                f"  of the {len(fb)} fallback-attributed pairs, the walk matched "
                f"the pooled cost on {len(fb_tied)} "
                f"({(len(fb_tied)/len(fb) if fb else 0):.1%}) -- credit denied "
                f"purely by the strict `<`"
            )
            print(
                "  pooled provenance: "
                + json.dumps(dict(Counter(r.get("pooled_method") for r in ok)))
            )

            iters = [r["walk_best_iteration"] for r in walk_ok if r["walk_best_iteration"]]
            first = [
                r["walk_first_success_iteration"]
                for r in walk_ok
                if r["walk_first_success_iteration"]
            ]
            print(f"walk best reached at iter    : {_quantiles(iters)}")
            print(f"walk first valid path at iter: {_quantiles(first)}")
            if iters:
                early = sum(1 for i in iters if i <= 18)
                print(
                    f"  walk best already in by iter 18 (the theta>D_init window): "
                    f"{early}/{len(iters)} ({early/len(iters):.1%})"
                )
            runlen = [r["iterations"] for r in ok]
            print(f"iterations run               : {_quantiles(runlen)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--draws", default=",".join(DRAWS))
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    ap.add_argument(
        "--budget",
        type=int,
        default=int(load_physarum_config(PHYSARUM_CONFIG_PATH)["default_iteration_budget"]),
    )
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    draws = [d for d in args.draws.split(",") if d]
    conditions = [c for c in args.conditions.split(",") if c]
    for d in draws:
        if d not in DRAWS:
            raise SystemExit(f"unknown draw {d!r}; expected one of {DRAWS}")
    for c in conditions:
        if c not in CONDITIONS:
            raise SystemExit(f"unknown condition {c!r}; expected one of {CONDITIONS}")

    if args.report_only:
        rows = load_rows(args.out)
        if not rows:
            raise SystemExit(f"no rows at {args.out}; run without --report-only first")
    else:
        rows = run_sweep(args.out, draws, conditions, args.budget)
    report(rows)


if __name__ == "__main__":
    main()
