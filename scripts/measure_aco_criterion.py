"""Phase 4d: a statistically clean re-measurement of the Phase 4 ACO exit
criterion.

Run:
    .venv/Scripts/python.exe scripts/measure_aco_criterion.py
    .venv/Scripts/python.exe scripts/measure_aco_criterion.py --report-only

The exit criterion is: **>=90% of OD pairs reach stretch <= 1.10 within the
iteration budget, under BOTH freeflow and BPR heavy**, in the headline
purely-local mode (`use_goal_heuristic: false`).

WHY THIS SCRIPT EXISTS. Phase 4c measured that criterion once and got 86.7%
freeflow / 96.7% BPR heavy -- a miss on freeflow by a single pair. Two
methodological problems make that number untrustworthy in EITHER direction,
and neither is fixable by re-reading the old run:

  1. **In-sample contamination.** 10 of the 30 criterion pairs were also the
     pairs the coarse re-tune was run on, so a third of the sample had been
     seen by the tuning.
  2. **n=1 in the stochastic dimension.** Every Phase 4c figure is algorithm
     seed 0. ACO is a stochastic search and the freeflow margin was ONE pair
     out of 30 (3.3 points), which is well inside plausible seed variation.

So this script re-measures with:

  * a **fresh out-of-sample OD draw** (`--pair-seed`, default 4242; Phase 4c
    used 2024 for pair selection), with the overlap against the Phase 4c /
    tuning pairs verified and printed rather than assumed;
  * **5 algorithm seeds** per pair, so the stochastic spread is visible
    instead of hidden;
  * a **two-stage bootstrap 95% CI** on the mean fraction, resampling OD
    pairs and then seeds within pair (see `_bootstrap_ci`), so the headline
    number comes with an interval and can be honestly compared to the 90%
    bar;
  * **stratification by path length in hops**, because Phase 4c's four
    freeflow failures were all long-hop pairs (46-83 hops) and that claim
    deserves to be confirmed or refuted on a larger, independent sample.

THIS SCRIPT DOES NOT TUNE ANYTHING. It reads configs/aco.yaml as shipped and
changes nothing. A miss measured here is a result to report, not a gap to
close.

OPERATIONAL NOTES

  * Snapshots are **pre-warmed serially** (congestion/base.py
    `prewarm_snapshots`) before any worker starts. Phase 4c's run died
    halfway because 12 joblib workers raced on the BPR snapshot cache; the
    cache write is now concurrency-safe, and this pre-warm means the race
    is not even exercised. Phase 8's runner must do the same.
  * Results are **checkpointed to JSONL after every completed solve**, and
    a re-run resumes from the file. A watchdog kill mid-sweep costs the
    in-flight solves only.
  * WALL-CLOCK FROM THIS SCRIPT IS NOT A PERFORMANCE NUMBER. It runs many
    workers at once on a machine whose throughput swings up to 3.4x with
    thermal state; per-solve times printed here are for progress
    estimation. `scripts/bench_aco.py` is the speed measurement.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
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
from bioroute.algorithms.aco import ACORouter, load_aco_config  # noqa: E402

AREA_CONFIG_PATH = ROOT / "configs" / "area_central_blr.yaml"
CONGESTION_CONFIG_PATH = ROOT / "configs" / "congestion.yaml"
ACO_CONFIG_PATH = ROOT / "configs" / "aco.yaml"

# The OD sample the Phase 4 / 4b / 4c sweep record used for BOTH tuning and
# the first exit-criterion measurement. Kept here only so the overlap check
# can prove the new sample is disjoint from it.
PHASE_4C_PAIR_SEED = 2024
PHASE_4C_N_PAIRS = 30
PHASE_4C_N_TUNING_PAIRS = 10  # the tuning set = the FIRST 10 of that draw

# Fresh, out-of-sample OD draw for Phase 4d.
DEFAULT_PAIR_SEED = 4242

STRETCH_BAR = 1.10
CRITERION_FRACTION = 0.90

LEVELS = ("freeflow", "bpr_heavy")


# --------------------------------------------------------------------------
# OD sampling and the out-of-sample check
# --------------------------------------------------------------------------


def draw_od_pairs(nodes: list, seed: int, n_pairs: int) -> list[tuple]:
    """The project's standard OD draw, unchanged from the Phase 4c protocol
    except for the seed -- so the only difference between the Phase 4c
    sample and this one is which pairs came out, not how they were chosen."""
    rng = random.Random(seed)
    return [tuple(rng.sample(nodes, 2)) for _ in range(n_pairs)]


def overlap_report(new_pairs: list[tuple], nodes: list) -> dict:
    """Prove the new sample is out-of-sample rather than asserting it.

    Checks the new draw against (a) the 10 tuning pairs and (b) all 30
    Phase 4c criterion pairs, both as ORDERED pairs (same origin AND same
    destination, same direction) and as UNORDERED pairs (same two endpoints
    in either direction -- a reversed pair is a different routing problem
    on a directed graph, but it shares the corridor, so it is worth
    counting). Node-level overlap is reported as context: with ~5k nodes
    and 60 node slots per sample a few shared endpoints are expected by
    chance and do not make a PAIR in-sample."""
    old_all = draw_od_pairs(nodes, PHASE_4C_PAIR_SEED, PHASE_4C_N_PAIRS)
    old_tuning = old_all[:PHASE_4C_N_TUNING_PAIRS]

    def unordered(pairs):
        return {frozenset(p) for p in pairs}

    new_set = set(new_pairs)
    new_unordered = unordered(new_pairs)
    new_nodes = {n for p in new_pairs for n in p}

    return {
        "n_new_pairs": len(new_pairs),
        "n_new_pairs_distinct": len(new_set),
        "ordered_overlap_with_tuning": sorted(new_set & set(old_tuning)),
        "ordered_overlap_with_phase4c_all": sorted(new_set & set(old_all)),
        "unordered_overlap_with_tuning": len(new_unordered & unordered(old_tuning)),
        "unordered_overlap_with_phase4c_all": len(new_unordered & unordered(old_all)),
        "shared_endpoint_nodes_with_phase4c_all": len(
            new_nodes & {n for p in old_all for n in p}
        ),
        "n_nodes_in_graph": len(nodes),
    }


# --------------------------------------------------------------------------
# Worker side: one (pair, seed, level) solve per task
# --------------------------------------------------------------------------

_WORKER_STATE: dict = {}


def _worker_init(graph_path: str, area_config_path: str, congestion_config_path: str,
                 aco_config_path: str, cache_dir: str) -> None:
    """Each worker process loads the graph and both snapshots ONCE.

    The snapshots come out of the pre-warmed on-disk cache the parent
    materialised serially, so no worker ever computes a BPR assignment and
    no worker ever writes the snapshot cache."""
    G = load_cached_graph(graph_path)
    area_cfg = load_area_config(area_config_path)
    snapshots = {"freeflow": FreeFlowProvider(G).snapshot()}
    bpr = BPRProvider(G, area_cfg, congestion_config_path, cache_dir=cache_dir)
    snapshots["bpr_heavy"] = bpr.snapshot("heavy", time_bin=1)
    _WORKER_STATE["G"] = G
    _WORKER_STATE["snapshots"] = snapshots
    _WORKER_STATE["aco_config_path"] = aco_config_path


def _solve(task: dict) -> dict:
    """Run ONE ACO solve and score it against the Dijkstra cost measured on
    the SAME snapshot (CLAUDE.md's stretch invariant)."""
    G = _WORKER_STATE["G"]
    snap = _WORKER_STATE["snapshots"][task["level"]]
    s, t = task["source"], task["target"]

    router = ACORouter(config_path=_WORKER_STATE["aco_config_path"])
    router.prepare(G, s, t, snap, seed=task["seed"])
    t0 = time.perf_counter()
    result = run_to_convergence(router, budget=task["budget"])
    wall = time.perf_counter() - t0

    out = dict(task)
    out.update(
        {
            "iterations": result.iterations,
            "converged": bool(result.converged),
            "first_valid_iter": result.time_to_first_valid_path_iters,
            "ants_total": int(router._ants_total),
            "ants_completed": int(router._ants_completed),
            "wallclock_s": wall,
            "aco_cost": result.cost,
            "failed": result.cost is None,
        }
    )
    if result.cost is None:
        out["stretch"] = None
        return out

    stretch = result.cost / task["dijkstra_cost"]
    # CLAUDE.md invariant: stretch < 1.0 means ACO beat Dijkstra on the same
    # weighted graph, which is a bug, not a good result.
    assert stretch >= 1.0 - 1e-9, (
        f"stretch {stretch} < 1.0 for OD ({s},{t}) level={task['level']} "
        f"seed={task['seed']} -- ACO beat Dijkstra on the same weighted graph."
    )
    out["stretch"] = stretch
    return out


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _bootstrap_ci(
    per_pair_seed_ok: list[list[int]], n_boot: int = 10000, seed: int = 0
) -> tuple[float, float]:
    """Two-stage (clustered) percentile bootstrap 95% CI for the mean
    fraction of OD pairs meeting the bar.

    `per_pair_seed_ok[i][j]` is 1 if pair i met stretch<=1.10 on algorithm
    seed j. Two things vary and both must be in the interval:

      * WHICH OD pairs were drawn -- resample pairs with replacement. This
        is the dominant term (n=30) and is what makes the estimate a claim
        about the network's OD population rather than about these 30 pairs.
      * WHICH algorithm seed was used -- resample seeds with replacement
        WITHIN each resampled pair (the clustered part), since the 5 seeds
        of a given pair are not independent draws from the population.

    A seeds-only bootstrap (n=5) would give a uselessly wide and
    unstable interval; a pairs-only bootstrap would understate the
    stochastic term. This does both."""
    rng = random.Random(seed)
    n_pairs = len(per_pair_seed_ok)
    if n_pairs == 0:
        return (float("nan"), float("nan"))
    means = []
    for _ in range(n_boot):
        total = 0.0
        for _ in range(n_pairs):
            row = per_pair_seed_ok[rng.randrange(n_pairs)]
            k = len(row)
            total += sum(row[rng.randrange(k)] for _ in range(k)) / k
        means.append(total / n_pairs)
    means.sort()
    lo = means[int(0.025 * (n_boot - 1))]
    hi = means[int(0.975 * (n_boot - 1))]
    return lo, hi


def _stratify_hops(hops_by_pair: dict, pair_ids: list) -> dict[str, list]:
    """Split pairs into short/medium/long by TERCILES of this sample's own
    Dijkstra hop counts.

    Terciles are a pre-declared, outcome-blind rule: the cut points depend
    only on the hop distribution, never on which pairs failed. Fixed hop
    thresholds would have been chosen with Phase 4c's failing hop counts
    (46-83) already known, which is exactly the post-hoc move this
    measurement is trying to avoid."""
    ordered = sorted(pair_ids, key=lambda pid: hops_by_pair[pid])
    n = len(ordered)
    c1, c2 = n // 3, (2 * n) // 3
    return {"short": ordered[:c1], "medium": ordered[c1:c2], "long": ordered[c2:]}


def aggregate(rows: list[dict], seeds: list[int]) -> dict:
    by_level: dict[str, dict] = {}
    for level in LEVELS:
        lrows = [r for r in rows if r["level"] == level]
        if not lrows:
            continue
        pair_ids = sorted({r["pair_index"] for r in lrows})
        hops_by_pair = {r["pair_index"]: r["dijkstra_hops"] for r in lrows}

        # per algorithm seed
        per_seed = []
        for sd in seeds:
            srows = [r for r in lrows if r["seed"] == sd]
            if not srows:
                continue
            stretches = [
                r["stretch"] if r["stretch"] is not None else float("inf") for r in srows
            ]
            finite = [x for x in stretches if math.isfinite(x)]
            ok = sum(1 for x in stretches if x <= STRETCH_BAR)
            ants_t = sum(r["ants_total"] for r in srows)
            ants_d = sum(r["ants_completed"] for r in srows)
            per_seed.append(
                {
                    "seed": sd,
                    "n": len(srows),
                    "ok": ok,
                    "frac": ok / len(srows),
                    "median": statistics.median(finite) if finite else float("inf"),
                    "mean": statistics.fmean(finite) if finite else float("inf"),
                    "worst": max(stretches),
                    "median_iters": statistics.median([r["iterations"] for r in srows]),
                    "completion": ants_d / ants_t if ants_t else float("nan"),
                    "n_failed": sum(1 for r in srows if r["failed"]),
                }
            )

        # per pair x seed success matrix, for the bootstrap
        ok_matrix = []
        for pid in pair_ids:
            prows = sorted(
                (r for r in lrows if r["pair_index"] == pid), key=lambda r: r["seed"]
            )
            ok_matrix.append(
                [
                    1 if (r["stretch"] is not None and r["stretch"] <= STRETCH_BAR) else 0
                    for r in prows
                ]
            )
        lo, hi = _bootstrap_ci(ok_matrix)
        fracs = [p["frac"] for p in per_seed]

        strata = _stratify_hops(hops_by_pair, pair_ids)
        strata_out = {}
        for name, pids in strata.items():
            if not pids:
                continue
            srows = [r for r in lrows if r["pair_index"] in set(pids)]
            stretches = [
                r["stretch"] if r["stretch"] is not None else float("inf") for r in srows
            ]
            finite = [x for x in stretches if math.isfinite(x)]
            ok = sum(1 for x in stretches if x <= STRETCH_BAR)
            # a pair "fails the bar" if it misses on a MAJORITY of seeds
            pair_fail = []
            for pid in pids:
                prows = [r for r in lrows if r["pair_index"] == pid]
                n_ok = sum(
                    1 for r in prows
                    if r["stretch"] is not None and r["stretch"] <= STRETCH_BAR
                )
                if n_ok * 2 < len(prows):
                    pair_fail.append(pid)
            strata_out[name] = {
                "n_pairs": len(pids),
                "hops_min": min(hops_by_pair[p] for p in pids),
                "hops_max": max(hops_by_pair[p] for p in pids),
                "n_solves": len(srows),
                "frac": ok / len(srows) if srows else float("nan"),
                "median": statistics.median(finite) if finite else float("inf"),
                "worst": max(stretches) if stretches else float("inf"),
                "pairs_failing_majority_of_seeds": sorted(pair_fail),
            }

        # per-pair summary (mean stretch over seeds, how many seeds met bar)
        per_pair = []
        for pid in pair_ids:
            prows = sorted(
                (r for r in lrows if r["pair_index"] == pid), key=lambda r: r["seed"]
            )
            st = [r["stretch"] for r in prows if r["stretch"] is not None]
            per_pair.append(
                {
                    "pair_index": pid,
                    "source": prows[0]["source"],
                    "target": prows[0]["target"],
                    "hops": hops_by_pair[pid],
                    "n_seeds": len(prows),
                    "n_seeds_ok": sum(
                        1 for r in prows
                        if r["stretch"] is not None and r["stretch"] <= STRETCH_BAR
                    ),
                    "mean_stretch": statistics.fmean(st) if st else float("inf"),
                    "min_stretch": min(st) if st else float("inf"),
                    "max_stretch": max(st) if st else float("inf"),
                }
            )

        by_level[level] = {
            "per_seed": per_seed,
            "mean_frac": statistics.fmean(fracs) if fracs else float("nan"),
            "min_frac": min(fracs) if fracs else float("nan"),
            "max_frac": max(fracs) if fracs else float("nan"),
            "sd_frac": statistics.stdev(fracs) if len(fracs) > 1 else 0.0,
            "ci95": (lo, hi),
            "strata": strata_out,
            "per_pair": per_pair,
            "n_pairs": len(pair_ids),
            "n_solves": len(lrows),
        }
    return by_level


def print_report(agg: dict, meta: dict) -> None:
    bar = f"{CRITERION_FRACTION:.0%}"
    print()
    print("=" * 78)
    print("PHASE 4D -- OUT-OF-SAMPLE ACO EXIT-CRITERION MEASUREMENT")
    print("=" * 78)
    print(
        f"pairs={meta['n_pairs']} (pair seed {meta['pair_seed']}, out-of-sample) | "
        f"algorithm seeds {meta['seeds']} | budget {meta['budget']} | "
        f"use_goal_heuristic={meta['use_goal_heuristic']} | "
        f"use_dead_node_memo={meta['use_dead_node_memo']}"
    )
    print(f"criterion: >= {bar} of pairs at stretch <= {STRETCH_BAR:.2f}")

    for level, a in agg.items():
        print()
        print(f"--- {level} " + "-" * (70 - len(level)))
        print(
            f"{'seed':>5} {'ok/n':>8} {'frac':>8} {'median':>8} {'mean':>8} "
            f"{'worst':>8} {'med iters':>10} {'ant compl':>10} {'failed':>7}"
        )
        for p in a["per_seed"]:
            print(
                f"{p['seed']:>5} {str(p['ok']) + '/' + str(p['n']):>8} "
                f"{p['frac']:>7.1%} {p['median']:>8.3f} {p['mean']:>8.3f} "
                f"{p['worst']:>8.3f} {p['median_iters']:>10.0f} "
                f"{p['completion']:>9.0%} {p['n_failed']:>7}"
            )
        lo, hi = a["ci95"]
        print(
            f"\n  mean fraction across seeds: {a['mean_frac']:.1%}  "
            f"[bootstrap 95% CI {lo:.1%} - {hi:.1%}]  "
            f"min {a['min_frac']:.1%} / max {a['max_frac']:.1%}  "
            f"(sd {a['sd_frac']:.3f})"
        )
        verdict = _verdict(a["mean_frac"], lo, hi)
        print(f"  vs the {bar} bar: {verdict}")

        print("\n  by Dijkstra path length (terciles of this sample's hop counts):")
        print(
            f"    {'stratum':>8} {'pairs':>6} {'hops':>10} {'frac<=1.10':>11} "
            f"{'median':>8} {'worst':>8}  pairs failing a majority of seeds"
        )
        for name in ("short", "medium", "long"):
            s = a["strata"].get(name)
            if not s:
                continue
            print(
                f"    {name:>8} {s['n_pairs']:>6} "
                f"{str(s['hops_min']) + '-' + str(s['hops_max']):>10} "
                f"{s['frac']:>10.1%} {s['median']:>8.3f} {s['worst']:>8.3f}  "
                f"{s['pairs_failing_majority_of_seeds']}"
            )

        bad = [p for p in a["per_pair"] if p["n_seeds_ok"] * 2 < p["n_seeds"]]
        if bad:
            print("\n  pairs missing the bar on a majority of seeds:")
            print(
                f"    {'pair':>5} {'hops':>5} {'seeds ok':>9} {'mean':>8} "
                f"{'min':>8} {'max':>8}   (source, target)"
            )
            for p in sorted(bad, key=lambda r: -r["mean_stretch"]):
                print(
                    f"    {p['pair_index']:>5} {p['hops']:>5} "
                    f"{str(p['n_seeds_ok']) + '/' + str(p['n_seeds']):>9} "
                    f"{p['mean_stretch']:>8.3f} {p['min_stretch']:>8.3f} "
                    f"{p['max_stretch']:>8.3f}   ({p['source']}, {p['target']})"
                )
    print()


def _verdict(mean_frac: float, lo: float, hi: float) -> str:
    if lo >= CRITERION_FRACTION:
        return "MET (whole CI at or above the bar)"
    if hi < CRITERION_FRACTION:
        return "MISSED (whole CI below the bar)"
    return (
        "INDISTINGUISHABLE -- the CI straddles the bar, so this sample "
        f"cannot separate {mean_frac:.1%} from {CRITERION_FRACTION:.0%}"
    )


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def _load_checkpoint(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # A kill mid-write can leave one truncated final line.
                # Drop it and recompute that solve; never half-believe it.
                print(f"  [resume] dropping one truncated checkpoint line", flush=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pair-seed", type=int, default=DEFAULT_PAIR_SEED)
    ap.add_argument("--n-pairs", type=int, default=30)
    ap.add_argument("--seeds", type=int, default=5, help="algorithm seeds 0..N-1")
    ap.add_argument("--budget", type=int, default=None, help="default: aco.yaml suggested_budget")
    ap.add_argument("--jobs", type=int, default=10)
    ap.add_argument(
        "--out",
        type=str,
        default=str(ROOT / "data" / "results" / "aco_criterion_phase4d.jsonl"),
    )
    ap.add_argument("--report-only", action="store_true",
                    help="aggregate the existing checkpoint without running anything")
    args = ap.parse_args()

    aco_cfg = load_aco_config(str(ACO_CONFIG_PATH))
    budget = args.budget if args.budget is not None else int(aco_cfg["suggested_budget"])
    seeds = list(range(args.seeds))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    area_cfg = load_area_config(AREA_CONFIG_PATH)
    graph_path = cache_path(area_cfg)
    if not Path(graph_path).exists():
        print(f"Cached graph not found at {graph_path}; run scripts/01_build_network.py first.")
        return 1
    G = load_cached_graph(graph_path)
    nodes = sorted(G.nodes())

    od_pairs = draw_od_pairs(nodes, args.pair_seed, args.n_pairs)
    ov = overlap_report(od_pairs, nodes)
    print("OUT-OF-SAMPLE CHECK (new draw vs. the Phase 4c / tuning draw)")
    print(f"  new pairs: {ov['n_new_pairs']} ({ov['n_new_pairs_distinct']} distinct)")
    print(f"  ordered overlap with the 10 TUNING pairs:        "
          f"{len(ov['ordered_overlap_with_tuning'])}  {ov['ordered_overlap_with_tuning']}")
    print(f"  ordered overlap with all 30 Phase 4c pairs:      "
          f"{len(ov['ordered_overlap_with_phase4c_all'])}  {ov['ordered_overlap_with_phase4c_all']}")
    print(f"  unordered (either direction) overlap, tuning:    {ov['unordered_overlap_with_tuning']}")
    print(f"  unordered overlap, all 30 Phase 4c pairs:        {ov['unordered_overlap_with_phase4c_all']}")
    print(f"  shared endpoint NODES with Phase 4c (context):   "
          f"{ov['shared_endpoint_nodes_with_phase4c_all']} of {2 * ov['n_new_pairs']} slots, "
          f"graph has {ov['n_nodes_in_graph']} nodes")
    if ov["ordered_overlap_with_phase4c_all"]:
        print("  !! NOT out-of-sample -- pick a different --pair-seed")
        return 1
    print("  => zero pair overlap: this sample is out-of-sample.")

    # ---- serial snapshot pre-warm, BEFORE any parallelism -----------------
    cache_dir = ROOT / "data" / "results" / "congestion_cache"
    print("\nPre-warming congestion snapshots serially (Phase 4d cache fix) ...")
    bpr = BPRProvider(G, area_cfg, CONGESTION_CONFIG_PATH, cache_dir=cache_dir)
    prewarm_snapshots(bpr, levels=["heavy"], time_bins=[1], verbose=True)
    snapshots = {
        "freeflow": FreeFlowProvider(G).snapshot(),
        "bpr_heavy": bpr.snapshot("heavy", time_bin=1),
    }
    print("  done.")

    # ---- Dijkstra baselines (serial, milliseconds) ------------------------
    baselines: dict[tuple[int, str], dict] = {}
    for i, (s, t) in enumerate(od_pairs):
        for level, snap in snapshots.items():
            d = DijkstraRouter()
            d.prepare(G, s, t, snap, seed=0)
            res = run_to_convergence(d, budget=1)
            if res.cost is None:
                baselines[(i, level)] = {"cost": None, "hops": None}
            else:
                baselines[(i, level)] = {"cost": res.cost, "hops": len(res.path) - 1}
    unroutable = [k for k, v in baselines.items() if v["cost"] is None]
    if unroutable:
        print(f"  {len(unroutable)} (pair, level) combinations have no Dijkstra path; skipped.")

    # ---- task list, minus whatever the checkpoint already holds -----------
    done_rows = _load_checkpoint(out_path)
    done_keys = {
        (r["pair_index"], r["seed"], r["level"])
        for r in done_rows
        if r.get("budget") == budget and r.get("pair_seed") == args.pair_seed
    }
    if done_rows:
        print(f"\nResuming: {len(done_keys)} solves already in {out_path.name}")

    tasks = []
    for i, (s, t) in enumerate(od_pairs):
        for level in LEVELS:
            base = baselines[(i, level)]
            if base["cost"] is None:
                continue
            for sd in seeds:
                if (i, sd, level) in done_keys:
                    continue
                tasks.append(
                    {
                        "pair_index": i,
                        "source": s,
                        "target": t,
                        "level": level,
                        "seed": sd,
                        "budget": budget,
                        "pair_seed": args.pair_seed,
                        "dijkstra_cost": base["cost"],
                        "dijkstra_hops": base["hops"],
                    }
                )

    if args.report_only:
        tasks = []

    if tasks:
        print(f"\nRunning {len(tasks)} solves on {args.jobs} workers "
              f"(budget {budget}). Wall-clock here is NOT a performance figure.")
        t_start = time.perf_counter()
        n_done = 0
        with out_path.open("a", encoding="utf-8") as sink:
            with ProcessPoolExecutor(
                max_workers=args.jobs,
                initializer=_worker_init,
                initargs=(
                    str(graph_path),
                    str(AREA_CONFIG_PATH),
                    str(CONGESTION_CONFIG_PATH),
                    str(ACO_CONFIG_PATH),
                    str(cache_dir),
                ),
            ) as pool:
                futures = {pool.submit(_solve, task): task for task in tasks}
                for fut in as_completed(futures):
                    row = fut.result()
                    # Checkpoint after EVERY solve: an earlier agent in this
                    # project lost an entire sweep to a watchdog kill.
                    sink.write(json.dumps(row) + "\n")
                    sink.flush()
                    os.fsync(sink.fileno())
                    n_done += 1
                    if n_done % 10 == 0 or n_done == len(tasks):
                        el = time.perf_counter() - t_start
                        rate = el / n_done
                        print(
                            f"  {n_done}/{len(tasks)} solves | {el/60:.1f} min elapsed | "
                            f"~{rate * (len(tasks) - n_done) / 60:.1f} min left",
                            flush=True,
                        )
        print(f"  finished in {(time.perf_counter() - t_start)/60:.1f} min")

    rows = [
        r for r in _load_checkpoint(out_path)
        if r.get("budget") == budget and r.get("pair_seed") == args.pair_seed
    ]
    if not rows:
        print("No results to aggregate.")
        return 1

    agg = aggregate(rows, seeds)
    meta = {
        "n_pairs": args.n_pairs,
        "pair_seed": args.pair_seed,
        "seeds": seeds,
        "budget": budget,
        "use_goal_heuristic": aco_cfg["use_goal_heuristic"],
        "use_dead_node_memo": aco_cfg["use_dead_node_memo"],
    }
    print_report(agg, meta)

    summary_path = out_path.with_suffix(".summary.json")
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump({"meta": meta, "overlap": ov, "by_level": agg}, f, indent=2, default=str)
    print(f"summary written to {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
