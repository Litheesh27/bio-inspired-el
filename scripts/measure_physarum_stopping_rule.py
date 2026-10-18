"""Phase 9b-mech follow-up: does Physarum's ADAPTATION improve its own path
extraction at all -- and if so, is there a better stopping rule than
`max|dD| < eps`?

Run:
    SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe \
        scripts/measure_physarum_stopping_rule.py --uniform-check
    SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe \
        scripts/measure_physarum_stopping_rule.py --part1
    SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe \
        scripts/measure_physarum_stopping_rule.py --part2
    .venv/Scripts/python.exe scripts/measure_physarum_stopping_rule.py --report-only

THE QUESTION, AND THE TRAP IN IT
--------------------------------------------------------------------------
Phase 9b-mech showed the max-flux walk works on a near-uniform conductivity
field and stops working as the field converges (walk completion 69.8% on SOME
iteration vs 43.0% on the FINAL one, n = 358). The obvious next move is to
hunt for a better stopping rule. That presupposes something nobody measured:
9b-mech measured walk COMPLETION, never walk COST.

So the primary question here is NOT the stopping rule. It is:

    Q1. Does adaptation improve the walk's extraction AT ALL?

The control: the walk on `D = D_init` uniform with ZERO adaptation steps. With
`D` constant, `w = D/L` is proportional to `1/L`, so that walk is an
electrical flow on a travel-time-resistive network, solved once -- it contains
none of the Tero adaptation. If it is as good as the best adapted iteration,
the max-flux walk is a static electrical-flow heuristic and there is no
stopping rule worth proposing.

THE CONTROL IS FREE
--------------------------------------------------------------------------
`step()` solves the pressures with the `D` in place BEFORE its own update (it
advances `self._D` after the solve -- which is exactly why
`measure_physarum_warm_mechanism.replay_walk` takes `D_pre`). So ITERATION 1
of a fresh solve IS the zero-adaptation uniform-field control, bit-for-bit.
`--uniform-check` asserts that rather than assuming it: it converges a router,
overwrites `_D` with `D_init`, re-solves, walks, and requires the walk to be
identical to iteration 1's.

Q3 IS ALSO FREE, AND THAT MATTERS
--------------------------------------------------------------------------
`step()` returns `self._best_cost`, a running min over every iteration AND
every extraction method, and `run_to_convergence` stores those returns in
`RouterResult.trace`. So `trace[k-1]` IS "the pooled best cost had we stopped
at iteration k" -- the cost of early stopping is readable off the trace with no
extra Dijkstra calls and, more importantly, with no redefinition of the
quantity the sweep reported.

BEHAVIOURAL IDENTITY (hard constraint)
--------------------------------------------------------------------------
`src/bioroute/algorithms/physarum.py` is NOT TOUCHED. The walk is NOT
re-implemented a third time: `replay_walk` is imported from
`scripts/measure_physarum_warm_mechanism.py`, where Phase 9b-mech validated it
against the router's own `last_extraction_method()` on ~40,000 iterations. The
same assertion runs on every iteration here, and each cell additionally
reconciles the replay's min walk cost / first-argmin iteration against the
router's `walk_best_cost()` / `walk_best_iteration()` -- which is how
`walk_best_iteration()` is verified to mean what Q2 relies on it meaning.

The only writes to router internals anywhere in this file are in
`uniform_field_check`, which overwrites a THROWAWAY router's `_D` as a
numerical probe (the same discipline as 9b-mech's `--scale-check`). No measured
number in the notes comes from a probed router.

SAMPLING
--------------------------------------------------------------------------
Declared in `docs/tuning/physarum_stopping_rule.md` before running:
  Part 1 (Q1/Q2/Q3): census of the undisrupted solve, 30 OD x 2 SUMO levels.
  Part 2 (Q4):       30 OD x {S1, S6} x SUMO heavy x {warm, cold}.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402

from bioroute.algorithms.base import RouterResult, run_to_convergence  # noqa: E402
from bioroute.algorithms.physarum import (  # noqa: E402
    PhysarumDegenerateSolve,
    PhysarumRouter,
    PhysarumUnreachableTarget,
)
from bioroute.disruption.apply import apply_disruption  # noqa: E402
from bioroute.disruption.scenarios import generate_disruption  # noqa: E402
from bioroute.experiments.runner import _disruption_config, make_router  # noqa: E402
from bioroute.metrics.collectors import dijkstra_reference  # noqa: E402
from bioroute.viz.traces import load_context  # noqa: E402

# REUSE, not reimplementation -- see the module docstring.
from measure_physarum_warm_mechanism import replay_walk  # noqa: E402

OUT_P1 = ROOT / "data" / "results" / "physarum_stopping_rule_part1.jsonl"
OUT_P2 = ROOT / "data" / "results" / "physarum_stopping_rule_part2.jsonl"

PART2_SCENARIOS = ("S1", "S6")
PART2_LEVEL = "heavy"


# ==========================================================================
# Driver: per-iteration walk completion AND walk cost
# ==========================================================================


def run_with_walk_costs(
    router: PhysarumRouter, budget: int
) -> tuple[RouterResult, list[dict[str, Any]]]:
    """`run_to_convergence`, recording the replayed walk AND its path cost
    every iteration.

    Line-for-line the same driver loop as
    `measure_physarum_warm_mechanism.run_with_walk_replay` (whose equivalence
    to `algorithms.base.run_to_convergence` that script's `--check` proved
    18/18), with one addition: when the replayed walk reaches the target, its
    path cost is computed with the router's OWN snapshot and graph, i.e. the
    identical `cost_of_path` call the router itself makes. That is the quantity
    Q1 turns on and the Parquet has no column for.
    """
    trace: list[float] = []
    per_iter: list[dict[str, Any]] = []
    first_valid_iter: int | None = None
    start = time.perf_counter()
    iterations = 0

    for i in range(1, budget + 1):
        D_pre = router._D.copy()
        cost = router.step()
        iterations = i
        if cost is None:
            trace.append(float("inf"))
        else:
            trace.append(cost)
            if first_valid_iter is None:
                first_valid_iter = i

        walk_path, reached = replay_walk(router, D_pre)
        method = router.last_extraction_method()
        # The replay is EXACT or the measurement is worthless (9b-mech's
        # discipline, kept verbatim).
        if reached != (method == "max_flux_walk"):
            raise AssertionError(
                f"walk replay disagrees with the router at iteration {i}: "
                f"replay reached={reached} but router method={method!r}"
            )
        walk_cost = (
            float(router._snapshot.cost_of_path(router._G, walk_path))
            if reached
            else None
        )
        per_iter.append(
            {
                "iter": i,
                "walk_ok": bool(reached),
                "walk_cost": walk_cost,
                "walk_hops": len(walk_path) - 1,
                "method": method,
                "pooled_best": None if cost is None else float(cost),
            }
        )

        if router.converged():
            break

    return (
        RouterResult(
            path=router.best_path(),
            cost=router.best_cost(),
            iterations=iterations,
            converged=router.converged(),
            trace=trace,
            time_to_first_valid_path_iters=first_valid_iter,
            wallclock_s=time.perf_counter() - start,
            extras={},
        ),
        per_iter,
    )


# ==========================================================================
# Per-solve summary
# ==========================================================================


def _argmin_first(values: Sequence[tuple[int, float]]) -> tuple[int, float] | None:
    """First (iteration, cost) achieving the strict minimum -- the same
    `cost < best` semantics `step()` uses for `_walk_best_*`."""
    best: tuple[int, float] | None = None
    for it, c in values:
        if best is None or c < best[1]:
            best = (it, c)
    return best


def summarise_solve(
    router: PhysarumRouter,
    res: RouterResult,
    per_iter: Sequence[dict[str, Any]],
    reference_cost: float,
) -> dict[str, Any]:
    """Everything Q1-Q3 need from one solve, plus the reconciliation that
    verifies `walk_best_iteration()` means what Q2 relies on."""
    oks = [(r["iter"], float(r["walk_cost"])) for r in per_iter if r["walk_ok"]]
    K = len(per_iter)

    # --- Q1's three comparators, all on this one solve ---------------------
    iter1 = per_iter[0] if per_iter else None
    iter1_ok = bool(iter1 and iter1["walk_ok"])
    iter1_cost = float(iter1["walk_cost"]) if iter1_ok else None

    adapted = [(it, c) for it, c in oks if it >= 2]
    best_adapted = _argmin_first(adapted)
    best_all = _argmin_first(oks)

    final = per_iter[-1] if per_iter else None
    final_ok = bool(final and final["walk_ok"])
    final_cost = float(final["walk_cost"]) if final_ok else None

    # --- reconciliation against the router's own trackers ------------------
    r_best_cost = router.walk_best_cost()
    r_best_iter = router.walk_best_iteration()
    r_first = router.walk_first_success_iteration()
    r_count = router.walk_success_count()
    recon = {
        "walk_best_cost_match": (
            (best_all is None and r_best_cost is None)
            or (
                best_all is not None
                and r_best_cost is not None
                and abs(best_all[1] - float(r_best_cost)) <= 1e-9 * max(1.0, abs(r_best_cost))
            )
        ),
        "walk_best_iter_match": (
            (best_all is None and r_best_iter is None)
            or (best_all is not None and r_best_iter == best_all[0])
        ),
        "walk_first_match": (
            (not oks and r_first is None) or (bool(oks) and r_first == oks[0][0])
        ),
        "walk_count_match": r_count == len(oks),
        "router_walk_best_cost": None if r_best_cost is None else float(r_best_cost),
        "router_walk_best_iter": r_best_iter,
    }

    def st(c: float | None) -> float | None:
        return None if c is None else float(c) / float(reference_cost)

    # --- Q3: the pooled running min, read off the trace --------------------
    def pooled_at(k: int | None) -> float | None:
        if k is None or not res.trace:
            return None
        v = res.trace[min(max(k, 1), len(res.trace)) - 1]
        return None if not math.isfinite(v) else float(v)

    pooled_final = pooled_at(K)
    pooled_at_walk_best = pooled_at(best_all[0] if best_all else None)
    # First iteration at which the pooled running min reached its final value.
    pooled_opt_iter = None
    if pooled_final is not None:
        for i, v in enumerate(res.trace, start=1):
            if math.isfinite(v) and v <= pooled_final * (1 + 1e-12):
                pooled_opt_iter = i
                break

    return {
        "n_iters": K,
        "converged": bool(res.converged),
        "reference_cost": float(reference_cost),
        # completion
        "iter1_walk_ok": iter1_ok,
        "final_walk_ok": final_ok,
        "any_walk_ok": bool(oks),
        "any_adapted_walk_ok": bool(adapted),
        "n_walk_ok": len(oks),
        "walk_ok_rate": len(oks) / K if K else None,
        # quality
        "iter1_walk_stretch": st(iter1_cost),
        "final_walk_stretch": st(final_cost),
        "best_adapted_walk_stretch": st(best_adapted[1]) if best_adapted else None,
        "best_adapted_walk_iter": best_adapted[0] if best_adapted else None,
        "best_walk_stretch": st(best_all[1]) if best_all else None,
        "best_walk_iter": best_all[0] if best_all else None,
        "first_walk_ok_iter": oks[0][0] if oks else None,
        # pooled (Q3)
        "pooled_final_stretch": st(pooled_final),
        "pooled_at_walk_best_stretch": st(pooled_at_walk_best),
        "pooled_optimum_first_iter": pooled_opt_iter,
        "best_path_extraction_method": router.best_path_extraction_method(),
        # the series themselves (Q2); 300 iterations max, so this is small
        "series_walk_ok": [int(r["walk_ok"]) for r in per_iter],
        "series_walk_stretch": [
            None if r["walk_cost"] is None else float(r["walk_cost"]) / float(reference_cost)
            for r in per_iter
        ],
        "series_method": [r["method"] for r in per_iter],
        # The POOLED running min as a stretch, per iteration. This IS
        # `RouterResult.trace` normalised: trace[k-1] is the pooled best cost
        # had the run stopped at iteration k, so this series answers Q3 for
        # every candidate fixed stopping rule at once.
        "series_pooled_stretch": [
            None if not math.isfinite(v) else float(v) / float(reference_cost)
            for v in res.trace
        ],
        "reconcile": recon,
    }


# ==========================================================================
# Part 1 -- the undisrupted census (Q1, Q2, Q3)
# ==========================================================================


def measure_part1_cell(ctx, od, level: str, options, budget: int) -> dict[str, Any]:
    G = ctx.graph
    snapshot = ctx.snapshot(level)
    ref_path, ref_cost = dijkstra_reference(G, od.source, od.target, snapshot)
    if ref_cost is None:
        return {}
    router: PhysarumRouter = make_router("physarum", options)  # type: ignore[assignment]
    router.prepare(G, od.source, od.target, snapshot, 0)
    res, per_iter = run_with_walk_costs(router, budget)
    row = {
        "part": 1,
        "od_source": int(od.source),
        "od_target": int(od.target),
        "stratum": od.stratum,
        "traffic_level": level,
        "scenario_id": "S0",
        "reference_hops": len(ref_path) - 1,
        "solve": summarise_solve(router, res, per_iter, ref_cost),
        # Parquet cross-check column.
        "stretch": None if res.cost is None else float(res.cost) / float(ref_cost),
        "iterations": int(res.iterations),
    }
    return row


# ==========================================================================
# Part 2 -- warm vs cold (Q4)
# ==========================================================================


def measure_part2_cell(
    ctx, od, scenario_id: str, level: str, options, budget: int
) -> list[dict[str, Any]]:
    G = ctx.graph
    snapshot = ctx.snapshot(level)
    cfg = _disruption_config(ctx.cfg.disruption)
    ref_path, ref_cost = dijkstra_reference(G, od.source, od.target, snapshot)
    if ref_cost is None:
        return []
    # scenario_id is the FIFTH positional argument (CLAUDE.md Phase 8d GOTCHA),
    # and `dis.ok` MUST be checked before use.
    dis = generate_disruption(
        G, od.source, od.target, snapshot, scenario_id,
        seed=ctx.cfg.scenario_seed, path=ref_path, config=cfg,
    )
    if not dis.ok:
        return [{"part": 2, "od_source": int(od.source), "od_target": int(od.target),
                 "scenario_id": scenario_id, "traffic_level": level,
                 "skipped": dis.reason}]
    removed = list(dis.removed_edges)
    G_dis = apply_disruption(G, removed, copy=False)
    _dp, dis_ref_cost = dijkstra_reference(G_dis, od.source, od.target, snapshot)
    if dis_ref_cost is None:
        return []

    rows: list[dict[str, Any]] = []
    for warm in (True, False):
        router: PhysarumRouter = make_router("physarum", options)  # type: ignore[assignment]
        try:
            router.prepare(G, od.source, od.target, snapshot, 0)
            init_res, init_iters = run_with_walk_costs(router, budget)
            init_sum = summarise_solve(router, init_res, init_iters, ref_cost)
            router.apply_disruption(list(removed), warm=warm)
            rec_res, rec_iters = run_with_walk_costs(router, budget)
            rec_sum = summarise_solve(router, rec_res, rec_iters, dis_ref_cost)
        except (PhysarumDegenerateSolve, PhysarumUnreachableTarget) as exc:
            rows.append({"part": 2, "od_source": int(od.source),
                         "od_target": int(od.target), "scenario_id": scenario_id,
                         "traffic_level": level, "warm_start": bool(warm),
                         "failed": f"{type(exc).__name__}: {exc}"})
            continue
        rows.append(
            {
                "part": 2,
                "od_source": int(od.source),
                "od_target": int(od.target),
                "stratum": od.stratum,
                "scenario_id": scenario_id,
                "traffic_level": level,
                "warm_start": bool(warm),
                "n_removed_edges": len(removed),
                "reference_hops": len(ref_path) - 1,
                "initial": init_sum,
                "recovery": rec_sum,
                # Parquet cross-check columns.
                "stretch": None if init_res.cost is None
                else float(init_res.cost) / float(ref_cost),
                "post_disruption_stretch": None if rec_res.cost is None
                else float(rec_res.cost) / float(dis_ref_cost),
                "recovery_iterations": int(rec_res.iterations),
            }
        )
    return rows


# ==========================================================================
# The uniform-field identity check
# ==========================================================================


def uniform_field_check(ctx, od_sel, options, budget: int, level: str) -> list[dict]:
    """Is iteration 1 really the zero-adaptation uniform-field control?

    Builds the control explicitly: converge a router (so `_D` is the thin
    converged corridor), then overwrite `_D` with `D_init` uniform, re-solve,
    and walk. The graph, `L` and the injection vector are unchanged by
    convergence, so the pressures -- and therefore the walk -- must be
    IDENTICAL to a fresh router's iteration 1. Anything else would mean
    iteration 1 is not the control Q1 claims it is.

    The `_D` overwrite is a NUMERICAL PROBE on a throwaway router. Nothing in
    `physarum.py` moves and no measured number comes from a probed run.
    """
    out: list[dict] = []
    G = ctx.graph
    snapshot = ctx.snapshot(level)
    for od in od_sel:
        a: PhysarumRouter = make_router("physarum", options)  # type: ignore[assignment]
        a.prepare(G, od.source, od.target, snapshot, 0)
        D0 = a._D.copy()
        a.step()
        walk1, ok1 = replay_walk(a, D0)

        b: PhysarumRouter = make_router("physarum", options)  # type: ignore[assignment]
        b.prepare(G, od.source, od.target, snapshot, 0)
        run_to_convergence(b, budget)
        b._D = np.full_like(b._D, float(b._cfg["D_init"]))  # numerical probe
        Dp = b._D.copy()
        b.step()
        walk2, ok2 = replay_walk(b, Dp)

        out.append(
            {
                "od": [int(od.source), int(od.target)],
                "iter1_walk_ok": bool(ok1),
                "uniform_probe_walk_ok": bool(ok2),
                "identical": bool(tuple(walk1) == tuple(walk2) and ok1 == ok2),
                "hops": len(walk1) - 1,
            }
        )
    return out


def crosscheck_parquet(rows: Sequence[dict], parquet_path: Path) -> dict[str, Any]:
    """Every measured cell against its Phase 8e Parquet row."""
    import pandas as pd

    df = pd.read_parquet(parquet_path)
    df = df[df.algorithm == "physarum"]
    checked = 0
    mismatches: list[dict[str, Any]] = []
    for r in rows:
        if r.get("failed") or r.get("skipped"):
            continue
        sel = df[
            (df.od_source == r["od_source"])
            & (df.od_target == r["od_target"])
            & (df.scenario_id == r["scenario_id"])
            & (df.traffic_level == r["traffic_level"])
        ]
        if "warm_start" in r:
            sel = sel[sel.warm_start == r["warm_start"]]
        if sel.empty:
            mismatches.append({"cell": str(r.get("scenario_id")), "why": "no parquet row"})
            continue
        row = sel.iloc[0]
        cols = ["stretch"]
        if r.get("part") == 2:
            cols += ["post_disruption_stretch", "recovery_iterations"]
        for col in cols:
            mine = r.get(col)
            theirs = row[col]
            theirs = None if theirs is None or pd.isna(theirs) else float(theirs)
            checked += 1
            ok = (mine is None and theirs is None) or (
                mine is not None and theirs is not None
                and abs(float(mine) - theirs) <= 1e-6 * max(1.0, abs(theirs))
            )
            if not ok:
                mismatches.append(
                    {"cell": f"{r['od_source']}->{r['od_target']} {r['scenario_id']}",
                     "field": col, "mine": mine, "parquet": theirs}
                )
    return {"checked": checked, "mismatches": mismatches[:20],
            "n_mismatches": len(mismatches)}


# ==========================================================================
# Reporting
# ==========================================================================


def _q(values: Sequence[float], q: float) -> float | None:
    s = sorted(float(v) for v in values)
    if not s:
        return None
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _fmt(values: Sequence[float | None], label: str) -> str:
    v = [float(x) for x in values if x is not None]
    if not v:
        return f"{label}: n=0"
    return (f"{label}: n={len(v)} median={statistics.median(v):.4f} "
            f"IQR=[{_q(v, .25):.4f},{_q(v, .75):.4f}]")


def _wilcoxon(a: Sequence, b: Sequence, label: str = "") -> str:
    pairs = [(float(x), float(y)) for x, y in zip(a, b)
             if x is not None and y is not None]
    if len(pairs) < 6:
        return f"{label} n={len(pairs)} -- UNDERPOWERED, no test"
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    lt = sum(1 for x, y in pairs if x < y)
    gt = sum(1 for x, y in pairs if x > y)
    eq = sum(1 for x, y in pairs if x == y)
    try:
        from scipy.stats import wilcoxon as _w
        p = f"p={_w(xs, ys, zero_method='wilcox').pvalue:.3g}"
    except Exception:
        p = "p=n/a (all differences zero)"
    return (f"{label} n={len(pairs)} med_a={statistics.median(xs):.4f} "
            f"med_b={statistics.median(ys):.4f} a<b:{lt} a>b:{gt} ties:{eq} {p}")


def _mcnemar(a_only: int, b_only: int) -> str:
    from scipy.stats import binomtest
    n = a_only + b_only
    if n == 0:
        return "discordant 0/0 -- no discordant pairs"
    return f"discordant {a_only}/{b_only}, exact p={binomtest(a_only, n, 0.5).pvalue:.3g}"


def _rate(flags: Sequence[bool], label: str) -> str:
    n = len(flags)
    k = sum(bool(x) for x in flags)
    return f"{label}: {k}/{n} = {k / n:.3f}" if n else f"{label}: n=0"


def report_solves(solves: Sequence[dict], tag: str) -> None:
    """Q1 / Q2 / Q3 on a set of solve summaries, paired by construction (each
    comparator is read off the SAME solve)."""
    print(f"\n{'=' * 74}\n== {tag}  (n = {len(solves)} solves)\n{'=' * 74}")
    if not solves:
        return

    bad = [s for s in solves if not all(
        s["reconcile"][k] for k in
        ("walk_best_cost_match", "walk_best_iter_match", "walk_first_match",
         "walk_count_match"))]
    print(f"-- RECONCILIATION vs the router's own walk trackers: "
          f"{len(solves) - len(bad)}/{len(solves)} solves agree on all 4 fields "
          f"(walk_best_cost, walk_best_iteration, first_success, count)")
    if bad:
        print(f"   MISMATCHES: {json.dumps([b['reconcile'] for b in bad[:3]])}")

    # ---------------- Q1: completion ----------------
    print("\n-- Q1a COMPLETION (paired: every rate is over the same solves) --")
    print("   " + _rate([s["iter1_walk_ok"] for s in solves],
                        "iteration 1  (UNIFORM, zero adaptation)"))
    print("   " + _rate([s["any_adapted_walk_ok"] for s in solves],
                        "best of iters 2..K (adapted, BEST-OF-MANY)"))
    print("   " + _rate([s["final_walk_ok"] for s in solves],
                        "final iteration (what the router ships)"))
    print("   " + _rate([s["any_walk_ok"] for s in solves],
                        "any iteration"))
    i1 = [s["iter1_walk_ok"] for s in solves]
    fin = [s["final_walk_ok"] for s in solves]
    ada = [s["any_adapted_walk_ok"] for s in solves]
    print("   iter1 vs final  : " + _mcnemar(
        sum(1 for a, b in zip(i1, fin) if a and not b),
        sum(1 for a, b in zip(i1, fin) if b and not a)) + "  (iter1-only / final-only)")
    print("   iter1 vs adapted: " + _mcnemar(
        sum(1 for a, b in zip(i1, ada) if a and not b),
        sum(1 for a, b in zip(i1, ada) if b and not a)) + "  (iter1-only / adapted-only)")

    # ---------------- Q1: quality ----------------
    print("\n-- Q1b QUALITY: walk stretch, CONDITIONAL ON COMPLETION --")
    print("   (different comparators complete on different solves, so the")
    print("    marginal medians below are NOT comparable -- the paired tests are)")
    print("   " + _fmt([s["iter1_walk_stretch"] for s in solves], "iter1 stretch"))
    print("   " + _fmt([s["best_adapted_walk_stretch"] for s in solves],
                       "best-of-2..K stretch"))
    print("   " + _fmt([s["final_walk_stretch"] for s in solves], "final-iter stretch"))
    print("   " + _fmt([s["best_walk_stretch"] for s in solves],
                       "best-over-all-iters stretch"))

    def paired(key_a: str, key_b: str, label: str) -> None:
        both = [s for s in solves
                if s[key_a] is not None and s[key_b] is not None]
        print(f"   PAIRED (both complete, n={len(both)}): " + _wilcoxon(
            [s[key_a] for s in both], [s[key_b] for s in both], label))

    paired("iter1_walk_stretch", "best_adapted_walk_stretch",
           "iter1 vs best-of-2..K:")
    paired("iter1_walk_stretch", "final_walk_stretch", "iter1 vs final-iter:  ")
    # Exact-match shares on the same conditional samples.
    for key, lbl in (("iter1_walk_stretch", "iter1"),
                     ("best_adapted_walk_stretch", "best 2..K"),
                     ("final_walk_stretch", "final")):
        vals = [s[key] for s in solves if s[key] is not None]
        if vals:
            ex = sum(1 for v in vals if v <= 1.0 + 1e-9)
            print(f"   exact-match (stretch == 1.0) | completed, {lbl:<10} "
                  f"{ex}/{len(vals)} = {ex / len(vals):.3f}")

    print("\n   THE UNCONDITIONAL VIEW (a solve that never completes counts as a")
    print("   loss for that comparator -- the honest 'which would you ship' table):")
    for key, okkey, lbl in (
            ("iter1_walk_stretch", "iter1_walk_ok", "iter1 (uniform)"),
            ("best_adapted_walk_stretch", "any_adapted_walk_ok", "best 2..K"),
            ("final_walk_stretch", "final_walk_ok", "final iter")):
        n = len(solves)
        got = sum(1 for s in solves if s[key] is not None)
        ex = sum(1 for s in solves if s[key] is not None and s[key] <= 1.0 + 1e-9)
        print(f"     {lbl:<16} completes {got}/{n} = {got / n:.3f}   "
              f"exact-match over ALL solves {ex}/{n} = {ex / n:.3f}")

    # ---------------- Q2: where do things peak ----------------
    print("\n-- Q2 WHERE THINGS PEAK --")
    bi = [s["best_walk_iter"] for s in solves if s["best_walk_iter"] is not None]
    fi = [s["first_walk_ok_iter"] for s in solves if s["first_walk_ok_iter"] is not None]
    print("   " + _fmt(bi, "walk_best_iteration (first strict argmin of walk cost)"))
    if bi:
        print(f"     == 1 on {sum(1 for x in bi if x == 1)}/{len(bi)} = "
              f"{sum(1 for x in bi if x == 1) / len(bi):.3f} of solves whose walk ever completes")
    print("   " + _fmt(fi, "first successful walk iteration"))
    if fi:
        print(f"     == 1 on {sum(1 for x in fi if x == 1)}/{len(fi)} = "
              f"{sum(1 for x in fi if x == 1) / len(fi):.3f}")
    print("   " + _fmt([s["n_iters"] for s in solves], "iterations run (K)"))

    # Pooled per-iteration series: completion rate and median stretch by iter.
    maxK = max(s["n_iters"] for s in solves)
    print("\n   per-iteration, pooled over solves (denominator = solves still running):")
    print("     iter   n_running  walk_ok_rate   median_walk_stretch  n_completing")
    grid = [1, 2, 3, 4, 5, 7, 10, 15, 20, 25, 30, 40, 50, 60, 80, 100, 150, 200, 300]
    for it in [g for g in grid if g <= maxK]:
        run = [s for s in solves if s["n_iters"] >= it]
        okf = [s["series_walk_ok"][it - 1] for s in run]
        sts = [s["series_walk_stretch"][it - 1] for s in run
               if s["series_walk_stretch"][it - 1] is not None]
        med = f"{statistics.median(sts):.4f}" if sts else "     --"
        print(f"     {it:>4}   {len(run):>9}  {sum(okf) / len(run):>12.3f}   "
              f"{med:>19}  {len(sts):>12}")
    print("   (the denominator shrinks as solves converge and stop; a solve that")
    print("    has stopped is not counted as a failure)")

    # Would a FIXED-iteration rule work?
    print("\n   a FIXED stopping rule, evaluated over all solves "
          "(walk result at exactly iteration k, or 'no path' if the solve ended):")
    print("     k    completes   exact-match   median stretch | completed")
    for it in [g for g in grid if g <= maxK]:
        comp, ex, sts = 0, 0, []
        for s in solves:
            if s["n_iters"] < it:
                continue
            v = s["series_walk_stretch"][it - 1]
            if v is not None:
                comp += 1
                sts.append(v)
                if v <= 1.0 + 1e-9:
                    ex += 1
        n = len(solves)
        med = f"{statistics.median(sts):.4f}" if sts else "  --"
        print(f"     {it:>3}  {comp:>4}/{n} = {comp / n:.3f}  {ex:>4}/{n} = "
              f"{ex / n:.3f}  {med:>8}")

    # ---------------- Q3: cost of stopping early ----------------
    print("\n-- Q3 WHAT STOPPING EARLY COSTS THE REPORTED (POOLED) RESULT --")
    pf = [s["pooled_final_stretch"] for s in solves]
    pw = [s["pooled_at_walk_best_stretch"] for s in solves]
    print("   " + _fmt(pf, "pooled stretch at convergence (what the sweep reported)"))
    print("   " + _fmt(pw, "pooled stretch if stopped at walk_best_iteration"))
    print("   " + _wilcoxon(pw, pf, "   stop-early vs converge:"))
    poi = [s["pooled_optimum_first_iter"] for s in solves
           if s["pooled_optimum_first_iter"] is not None]
    print("   " + _fmt(poi, "first iteration the pooled running-min hits its FINAL value"))
    if poi:
        for k in (1, 2, 5, 10, 20):
            print(f"     pooled optimum already reached by iteration {k:>3}: "
                  f"{sum(1 for x in poi if x <= k)}/{len(poi)} = "
                  f"{sum(1 for x in poi if x <= k) / len(poi):.3f}")
    worse = [(s["pooled_at_walk_best_stretch"], s["pooled_final_stretch"]) for s in solves
             if s["pooled_at_walk_best_stretch"] is not None
             and s["pooled_final_stretch"] is not None]
    nw = sum(1 for a, b in worse if a > b + 1e-12)
    print(f"   cells where stopping at walk_best_iteration is STRICTLY worse "
          f"pooled: {nw}/{len(worse)}")
    print("   provenance of the reported path: "
          + json.dumps(Counter(s["best_path_extraction_method"] for s in solves)))

    # And the fixed-rule version of Q3, straight off the pooled series.
    print("\n   pooled stretch under a FIXED stop at iteration k (= trace[k-1];")
    print("   a solve that already stopped contributes its own final value):")
    print("     k      n   median pooled stretch   exact-match        p90")
    for it in [g for g in grid if g <= maxK]:
        vals: list[float] = []
        for s in solves:
            ser = s.get("series_pooled_stretch") or []
            if not ser:
                continue
            v = ser[min(it, len(ser)) - 1]
            if v is not None:
                vals.append(float(v))
        if not vals:
            continue
        ex = sum(1 for v in vals if v <= 1.0 + 1e-9)
        print(f"     {it:>3}  {len(vals):>5}  {statistics.median(vals):>19.4f}  "
              f"{ex:>5}/{len(vals)} = {ex / len(vals):.3f}  {_q(vals, .9):>9.4f}")


def report(rows: Sequence[dict]) -> None:
    p1 = [r for r in rows if r.get("part") == 1 and "solve" in r]
    p2 = [r for r in rows if r.get("part") == 2 and "recovery" in r]

    if p1:
        report_solves([r["solve"] for r in p1],
                      "PART 1 -- UNDISRUPTED SOLVE, census 30 OD x 2 SUMO levels")
        # Stratified by path length, because 9b-mech showed reference_hops is
        # the strongest cross-cell predictor of walk behaviour.
        hops = sorted(r["reference_hops"] for r in p1)
        t1, t2 = _q(hops, 1 / 3) or 0, _q(hops, 2 / 3) or 0
        print(f"\n-- PART 1 by reference_hops tercile (cuts {t1:.0f}/{t2:.0f}) --")
        for lo, hi, lbl in ((-1, t1, "short"), (t1, t2, "medium"), (t2, 1e9, "long")):
            grp = [r["solve"] for r in p1 if lo < r["reference_hops"] <= hi]
            if not grp:
                continue
            i1c = [s for s in grp if s["iter1_walk_stretch"] is not None]
            bac = [s for s in grp if s["best_adapted_walk_stretch"] is not None]
            print(f"   {lbl:<7} n={len(grp):<3} "
                  f"iter1_ok={sum(s['iter1_walk_ok'] for s in grp) / len(grp):.3f} "
                  f"final_ok={sum(s['final_walk_ok'] for s in grp) / len(grp):.3f} "
                  f"iter1_med_stretch="
                  f"{statistics.median([s['iter1_walk_stretch'] for s in i1c]):.4f} (n={len(i1c)}) "
                  f"best2K_med_stretch="
                  f"{statistics.median([s['best_adapted_walk_stretch'] for s in bac]):.4f} (n={len(bac)})"
                  if i1c and bac else f"   {lbl:<7} n={len(grp):<3} UNDERPOWERED")

    if p2:
        print(f"\n\n{'#' * 74}\n## Q4 -- WARM vs COLD (part 2)\n{'#' * 74}")
        warm = [r["recovery"] for r in p2 if r["warm_start"]]
        cold = [r["recovery"] for r in p2 if not r["warm_start"]]
        report_solves(warm, "PART 2 -- WARM recovery")
        report_solves(cold, "PART 2 -- COLD recovery")

        # Paired warm vs cold on identical cells.
        by: dict[tuple, dict[bool, dict]] = {}
        for r in p2:
            key = (r["od_source"], r["od_target"], r["scenario_id"], r["traffic_level"])
            by.setdefault(key, {})[bool(r["warm_start"])] = r
        cells = {k: v for k, v in by.items() if True in v and False in v}
        print(f"\n-- Q4 PAIRED on identical (od, scenario, traffic): n={len(cells)} --")
        for key, lbl in (("iter1_walk_ok", "walk ok at recovery iteration 1"),
                         ("final_walk_ok", "walk ok at the final iteration"),
                         ("any_walk_ok", "walk ok at any iteration")):
            w = [bool(c[True]["recovery"][key]) for c in cells.values()]
            cl = [bool(c[False]["recovery"][key]) for c in cells.values()]
            print(f"   {lbl:<34} warm {sum(w)}/{len(w)}={sum(w)/len(w):.3f}  "
                  f"cold {sum(cl)}/{len(cl)}={sum(cl)/len(cl):.3f}  "
                  + _mcnemar(sum(1 for a, b in zip(w, cl) if a and not b),
                             sum(1 for a, b in zip(w, cl) if b and not a))
                  + " (warm-only/cold-only)")
        for key, lbl in (("iter1_walk_stretch", "iter1 walk stretch"),
                         ("best_adapted_walk_stretch", "best-of-2..K walk stretch"),
                         ("final_walk_stretch", "final-iter walk stretch"),
                         ("pooled_final_stretch", "POOLED stretch at convergence")):
            both = [c for c in cells.values()
                    if c[True]["recovery"][key] is not None
                    and c[False]["recovery"][key] is not None]
            print(f"   {lbl:<34} " + _wilcoxon(
                [c[True]["recovery"][key] for c in both],
                [c[False]["recovery"][key] for c in both], "warm vs cold:"))
        print("\n   Q4's claim under test: cold recovery's iteration 1 is a UNIFORM")
        print("   field while warm's is an already-converged one, so the whole")
        print("   warm/cold walk gap should be explained by position on the")
        print("   convergence trajectory -- check cold iter1 vs part-1 iter1 above.")


# ==========================================================================
# Main
# ==========================================================================


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--part1", action="store_true")
    ap.add_argument("--part2", action="store_true")
    ap.add_argument("--uniform-check", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--n-od", type=int, default=0, help="pilot: first N OD pairs")
    ap.add_argument("--levels", default="heavy,light")
    ap.add_argument("--scenarios", default=",".join(PART2_SCENARIOS))
    ap.add_argument("--out1", type=Path, default=OUT_P1)
    ap.add_argument("--out2", type=Path, default=OUT_P2)
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()

    if args.report_only:
        rows: list[dict] = []
        for p in (args.out1, args.out2):
            if p.exists():
                rows += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        report(rows)
        return 0

    ctx = load_context(verbose=True)
    options = ctx.options("physarum")
    budget = ctx.budget("physarum")
    od_sel = list(ctx.od_pairs)[: args.n_od] if args.n_od else list(ctx.od_pairs)
    levels = [x for x in args.levels.split(",") if x]
    print(f"  budget={budget} n_od={len(od_sel)} levels={levels}", flush=True)

    if args.uniform_check:
        res = uniform_field_check(ctx, od_sel[: args.n_od or 6], options, budget,
                                  levels[0])
        print(json.dumps(res, indent=2))
        n_id = sum(1 for r in res if r["identical"])
        print(f"\n  iteration 1 == explicit uniform-field probe on "
              f"{n_id}/{len(res)} OD pairs")
        return 0 if n_id == len(res) else 1

    all_rows: list[dict] = []
    if args.part1:
        args.out1.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if args.append else "w"
        jobs = [(od, lv) for lv in levels for od in od_sel]
        with args.out1.open(mode, encoding="utf-8") as fh:
            for n, (od, lv) in enumerate(jobs, start=1):
                t0 = time.perf_counter()
                row = measure_part1_cell(ctx, od, lv, options, budget)
                if row:
                    fh.write(json.dumps(row) + "\n")
                    fh.flush()
                    all_rows.append(row)
                    s = row["solve"]
                    print(f"  [p1 {n}/{len(jobs)}] {lv} {od.source}->{od.target} "
                          f"K={s['n_iters']} iter1_ok={int(s['iter1_walk_ok'])} "
                          f"final_ok={int(s['final_walk_ok'])} "
                          f"best_iter={s['best_walk_iter']} "
                          f"({time.perf_counter() - t0:.1f}s)", flush=True)

    if args.part2:
        args.out2.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if args.append else "w"
        scen = [s for s in args.scenarios.split(",") if s]
        jobs2 = [(od, s, lv) for lv in levels for od in od_sel for s in scen]
        with args.out2.open(mode, encoding="utf-8") as fh:
            for n, (od, s, lv) in enumerate(jobs2, start=1):
                t0 = time.perf_counter()
                rows = measure_part2_cell(ctx, od, s, lv, options, budget)
                for r in rows:
                    fh.write(json.dumps(r) + "\n")
                fh.flush()
                all_rows.extend(rows)
                got = {("warm" if r.get("warm_start") else "cold"):
                       r.get("recovery", {}).get("any_walk_ok") for r in rows
                       if "recovery" in r}
                print(f"  [p2 {n}/{len(jobs2)}] {lv} {od.source}->{od.target} {s}: "
                      f"{got} ({time.perf_counter() - t0:.1f}s)", flush=True)

    if all_rows:
        cc = crosscheck_parquet(
            all_rows, ROOT / "data/results/final_14a535c02b83.parquet")
        print(json.dumps({"parquet_crosscheck": cc}, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
