"""Phase 4b benchmark: seconds/iteration and construction work (operations
per ant) for ACORouter on the real Central Bengaluru graph, plus the
projected Phase 8 ACO wall-clock cost those numbers imply.

Run: .venv/Scripts/python.exe scripts/bench_aco.py [--pairs N] [--iters N]
                                                  [--memo-ab]

`--memo-ab` additionally runs the Phase 4c dead-node memo on/off A/B with
the two variants' iterations ROUND-ROBINED inside this one process --
which is the only way a before/after is meaningful on this machine (see
the throughput caveat below and in docs/tuning/aco_sweep.md).

Prints, per OD pair and in aggregate:
  - seconds/iteration (mean over N timed iterations, after a couple of
    warmup iterations so the BFS/array build in `prepare()` is not counted
    as per-iteration cost)
  - operations per ant -- forward steps + backtracks inside
    `_construct_ant`, the quantity `op_budget = max_steps *
    backtrack_multiplier` bounds. Reported alongside wall-clock because
    wall-clock alone cannot distinguish "each operation got cheaper" from
    "we stopped doing pointless operations"; Phase 4b's speedup is
    entirely the former (the search itself is bit-for-bit unchanged).
  - ant completion rate over the timed window (fraction of ants that
    reached the target rather than exhausting the op budget) -- context
    for the ops number, since an ant that never completes always burns the
    full budget.
  - projected serial hours for the full Phase 8 ACO sweep, and the same
    projection assuming a 10x speedup from joblib parallelism across 14
    cores.

Several OD pairs are benchmarked because per-iteration cost varies by more
than an order of magnitude with pair difficulty: a pair whose ants complete
cheaply costs a fraction of one whose ants burn the whole backtracking
budget every time. The projection uses the MEAN over pairs, and the spread
is printed so a single lucky/unlucky pair cannot be mistaken for the
sweep's cost.

This does not touch the exit-criterion measurement (stretch, completion
rate over 30 pairs) -- see tests/test_aco.py's slow tests for that. This
script measures speed only.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bioroute.config import load_area_config
from bioroute.network.graph import cache_path, load_cached_graph
from bioroute.congestion.base import FreeFlowProvider
from bioroute.algorithms.aco import ACORouter, load_aco_config

AREA_CONFIG_PATH = ROOT / "configs" / "area_central_blr.yaml"
ACO_CONFIG_PATH = ROOT / "configs" / "aco.yaml"

# Phase 8 sweep size assumption used only for the projection printed below
# (see docs/PLAN.md Phase 8 for the actual matrix once it exists) --
# CLAUDE.md's Phase 4b task description quotes ~4,200 ACO solves total.
N_PROJECTED_SOLVES = 4200
N_PARALLEL_CORES_SPEEDUP = 10  # "10x speedup ... 14 cores" per the task brief

# Same OD sampling as tests/test_aco.py and the docs/tuning/aco_sweep.md
# record, so benchmark pairs and quality-measurement pairs are the same
# population.
OD_SAMPLE_SEED = 2024


def _od_pairs(G, n_pairs: int):
    rng = random.Random(OD_SAMPLE_SEED)
    nodes = sorted(G.nodes())
    return [tuple(rng.sample(nodes, 2)) for _ in range(n_pairs)]


def bench_pair(G, snap, s, t, seed=0, n_iters=10, n_warmup=2, memo=None) -> dict:
    router = ACORouter(config_path=str(ACO_CONFIG_PATH), use_dead_node_memo=memo)
    router.prepare(G, s, t, snap, seed=seed)

    for _ in range(n_warmup):
        router.step()

    ops0, ants0, done0 = router._ops_total, router._ants_total, router._ants_completed
    per_iter = []
    for _ in range(n_iters):
        start = time.perf_counter()
        router.step()
        per_iter.append(time.perf_counter() - start)
    ops = router._ops_total - ops0
    ants = router._ants_total - ants0
    done = router._ants_completed - done0

    return {
        "od": (s, t),
        # Median, not mean: this machine drops to roughly a third of its
        # clock after ~1.3 s of sustained single-threaded load (measured --
        # an identical repeated workload goes from ~80 ms to ~250 ms and
        # stays there), so the first iterations of any run are "burst"
        # speed and everything afterwards is "sustained" speed. A sweep
        # that runs for hours on every core runs at SUSTAINED speed, so
        # the median is what the Phase 8 projection below is based on;
        # `min` is reported alongside as the burst figure so the two are
        # never confused when comparing against other machines' numbers.
        "sec_per_iter": statistics.median(per_iter),
        "sec_per_iter_burst": min(per_iter),
        "ops_per_ant": ops / ants if ants else float("nan"),
        "completion_rate": done / ants if ants else float("nan"),
        "op_budget": router._max_steps * router._backtrack_multiplier,
        "memo": router._use_dead_node_memo,
    }


def bench_pair_memo_ab(G, snap, s, t, seed=0, n_iters=10, n_warmup=2) -> dict:
    """Phase 4c: memo ON vs memo OFF, INTERLEAVED IN ONE PROCESS.

    This machine's throughput swings by up to 3.4x with thermal state, so
    two numbers taken from two separate runs are not comparable (see
    docs/tuning/aco_sweep.md's "Measurement conditions"). Here both
    variants are prepared up front on the same OD pair with the same seed
    and their timed iterations are round-robined, so any throttling that
    occurs hits both equally.
    """
    routers = {}
    for memo in (True, False):
        r = ACORouter(config_path=str(ACO_CONFIG_PATH), use_dead_node_memo=memo)
        r.prepare(G, s, t, snap, seed=seed)
        for _ in range(n_warmup):
            r.step()
        routers[memo] = r

    times = {True: [], False: []}
    base = {m: (r._ops_total, r._ants_total, r._ants_completed)
            for m, r in routers.items()}
    for _ in range(n_iters):
        for memo in (True, False):  # round-robin, one iteration each
            r = routers[memo]
            start = time.perf_counter()
            r.step()
            times[memo].append(time.perf_counter() - start)

    out = {"od": (s, t)}
    for memo, r in routers.items():
        ops = r._ops_total - base[memo][0]
        ants = r._ants_total - base[memo][1]
        done = r._ants_completed - base[memo][2]
        out[memo] = {
            "sec_per_iter": statistics.median(times[memo]),
            "ops_per_ant": ops / ants if ants else float("nan"),
            "completion_rate": done / ants if ants else float("nan"),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", type=int, default=5, help="number of OD pairs to benchmark")
    ap.add_argument("--iters", type=int, default=10, help="timed iterations per pair")
    ap.add_argument(
        "--memo-ab",
        action="store_true",
        help="also run the Phase 4c memo on/off A/B, interleaved in this process",
    )
    args = ap.parse_args()

    area_cfg = load_area_config(AREA_CONFIG_PATH)
    path = cache_path(area_cfg)
    if not path.exists():
        print(f"Cached graph not found at {path}; run scripts/01_build_network.py first.")
        sys.exit(1)
    G = load_cached_graph(path)
    print(f"Graph: {G.number_of_nodes()} nodes / {G.number_of_edges()} edges")

    snap = FreeFlowProvider(G).snapshot()
    aco_cfg = load_aco_config(str(ACO_CONFIG_PATH))
    print(
        f"config: n_ants={aco_cfg['n_ants']}, "
        f"use_goal_heuristic={aco_cfg['use_goal_heuristic']}, "
        f"use_dead_node_memo={aco_cfg['use_dead_node_memo']}, "
        f"beta_no_heuristic={aco_cfg['beta_no_heuristic']}, rho={aco_cfg['rho']}, "
        f"backtrack_multiplier={aco_cfg['backtrack_multiplier']}"
    )
    print()

    rows = []
    for s, t in _od_pairs(G, args.pairs):
        row = bench_pair(G, snap, s, t, n_iters=args.iters)
        rows.append(row)
        print(
            f"  OD ({s}, {t}): {row['sec_per_iter']:.4f} s/iter sustained "
            f"({row['sec_per_iter_burst']:.4f} burst), "
            f"{row['ops_per_ant']:.0f} ops/ant (budget {row['op_budget']}), "
            f"completion {row['completion_rate']:.0%}"
        )

    sec = [r["sec_per_iter"] for r in rows]
    burst = [r["sec_per_iter_burst"] for r in rows]
    ops = [r["ops_per_ant"] for r in rows]
    mean_sec = statistics.fmean(sec)
    print()
    print(
        f"seconds/iteration (sustained): mean {mean_sec:.4f} s "
        f"(min {min(sec):.4f}, median {statistics.median(sec):.4f}, max {max(sec):.4f})"
    )
    print(
        f"seconds/iteration (burst):     mean {statistics.fmean(burst):.4f} s "
        f"-- unthrottled, NOT the basis for the sweep projection"
    )
    print(
        f"operations/ant:    mean {statistics.fmean(ops):.0f} "
        f"(min {min(ops):.0f}, median {statistics.median(ops):.0f}, max {max(ops):.0f})"
    )

    budget = aco_cfg["suggested_budget"]
    sec_per_solve = mean_sec * budget
    print(
        f"suggested_budget={budget} -> ~{sec_per_solve:.1f} s/solve (upper bound, "
        f"assumes no early convergence)"
    )

    serial_hours = sec_per_solve * N_PROJECTED_SOLVES / 3600.0
    print(
        f"Projected Phase 8 ACO sweep ({N_PROJECTED_SOLVES} solves), serial: "
        f"{serial_hours:.1f} hours"
    )
    parallel_hours = serial_hours / N_PARALLEL_CORES_SPEEDUP
    print(
        f"Projected Phase 8 ACO sweep, assuming {N_PARALLEL_CORES_SPEEDUP}x "
        f"parallel (joblib, 14 cores): {parallel_hours:.1f} hours"
    )

    if args.memo_ab:
        print()
        print("Phase 4c memo A/B (interleaved in this process -- the only")
        print("comparison this machine's thermal behaviour makes meaningful):")
        agg = {True: {"s": [], "o": [], "c": []}, False: {"s": [], "o": [], "c": []}}
        for s_, t_ in _od_pairs(G, args.pairs):
            ab = bench_pair_memo_ab(G, snap, s_, t_, n_iters=args.iters)
            for memo in (True, False):
                agg[memo]["s"].append(ab[memo]["sec_per_iter"])
                agg[memo]["o"].append(ab[memo]["ops_per_ant"])
                agg[memo]["c"].append(ab[memo]["completion_rate"])
            print(
                f"  OD ({s_}, {t_}): "
                f"memo ON {ab[True]['sec_per_iter']:.4f} s/it, "
                f"{ab[True]['ops_per_ant']:.0f} ops/ant, "
                f"{ab[True]['completion_rate']:.0%} complete | "
                f"memo OFF {ab[False]['sec_per_iter']:.4f} s/it, "
                f"{ab[False]['ops_per_ant']:.0f} ops/ant, "
                f"{ab[False]['completion_rate']:.0%} complete"
            )
        for memo in (True, False):
            a = agg[memo]
            print(
                f"  memo {'ON ' if memo else 'OFF'} mean: "
                f"{statistics.fmean(a['s']):.4f} s/iteration, "
                f"{statistics.fmean(a['o']):.0f} ops/ant, "
                f"{statistics.fmean(a['c']):.0%} ant completion"
            )


if __name__ == "__main__":
    main()
