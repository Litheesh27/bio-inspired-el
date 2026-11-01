"""Phase 9d: per-iteration animations of how ACO and Physarum form their paths.

The Parquet stores no per-iteration state, so -- exactly like figures 1, 5
and 6 -- these runs are REGENERATED under the sweep's own config, graph,
snapshots and seeds, and every run is cross-checked against the Parquet
(iteration counts and stretch, initial and recovery, warm and cold) before
anything is written. A mismatch aborts the export: an animation of a run
that is not the one in the evidence file would illustrate a result that does
not exist.

What is captured, per iteration:
  Physarum  the conductivity field D on every undirected tube; the max-flux
            walk's ATTEMPT that iteration, including where it dead-ended
            (via `replay_walk`, verified against the router on ~40k
            iterations in Phase 9b-mech); which extraction method produced
            the path; and the reported best path WITH ITS PROVENANCE.
  ACO       the pheromone field on every directed edge, the iteration-best
            ant's path, and the global best.

Provenance is carried on every frame on purpose. An animation that shows the
tubes converging and then labels the final route "Physarum's" would commit
the exact error the paper is about: on warm recoveries that route is plain
Dijkstra two-thirds of the time.

Routers are read, never modified. The run loop is `run_to_convergence`'s,
line for line, with a capture hook.

Run:
    .venv/Scripts/python.exe scripts/07_export_animation.py
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd  # noqa: E402

# Imported from exactly where `viz/traces.py` imports them, so the closure is
# drawn and the routers built byte-identically to the dashboard's existing
# regenerated routes.
from bioroute.disruption.apply import apply_disruption  # noqa: E402
from bioroute.disruption.scenarios import generate_disruption  # noqa: E402
from bioroute.experiments.runner import (  # noqa: E402
    _disruption_config,  # noqa: PLC2701
    make_router,
)
from bioroute.metrics.collectors import dijkstra_reference  # noqa: E402
from bioroute.viz import traces  # noqa: E402
from measure_physarum_warm_mechanism import replay_walk  # noqa: E402

PARQUET = ROOT / "data" / "results" / "final_14a535c02b83.parquet"
OUT_DIR = ROOT / "web" / "data" / "anim"
LEVEL = "heavy"
SEED = 0
FRAMES_PER_PHASE = 26

# Chosen from the dashboard's own route cells to show three DIFFERENT real
# behaviours, not three flattering ones: the model working, the model's
# answer secretly being Dijkstra's, and both bio-inspired methods failing.
SHOWCASE = [
    {
        "od": (429415768, 10038603036), "scenario": "S4",
        "title": "When the model works",
        "story": ("Physarum's own max-flux walk reaches the optimum both before "
                  "and after five path edges close, and so does ACO."),
    },
    {
        "od": (6991440387, 10060658429), "scenario": "S1",
        "title": "When the answer is really Dijkstra's",
        "story": ("Before the closure Physarum's own walk finds the route. After "
                  "it, the warm walk dead-ends and the full-graph fallback, which "
                  "is plain Dijkstra, supplies a perfect-looking answer in a "
                  "single iteration."),
    },
    {
        "od": (429415654, 10038577016), "scenario": "S4",
        "title": "When both methods go wrong",
        "story": ("Both bio-inspired methods converge confidently onto routes "
                  "that are shorter in hops but slower: geometrically direct "
                  "corridors that are exactly the congested ones."),
    },
]


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------

def _b64(arr: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode("ascii")


def encode_field(values: np.ndarray) -> dict:
    """Quantise a [0,1] field to uint8 and pick the smaller of a dense or a
    sparse encoding. Late frames are mostly zero (tubes have atrophied,
    pheromone sits at its floor), early frames are near-uniform, so neither
    encoding wins everywhere."""
    q = np.clip(np.rint(values * 255.0), 0, 255).astype(np.uint8)
    nz = np.flatnonzero(q)
    if nz.size * 3 < q.size:
        return {"s": 1, "i": _b64(nz.astype("<u2")), "v": _b64(q[nz])}
    return {"s": 0, "d": _b64(q)}


def pick_frames(frames: list, k: int = FRAMES_PER_PHASE) -> list[int]:
    """Indices of the frames to keep, spaced EVENLY IN VISUAL CHANGE rather
    than in iterations.

    Measured on these runs, the field is not a gradual fade but a step: every
    tube stays within half-scale for ~20 iterations while conductivity (or
    pheromone) decays in lockstep, then ~95% of them collapse within about
    four iterations. Spacing by iteration, even log-spaced, spends most frames
    on the uniform stretch and gives the collapse, the actual event, two.

    So frames are placed at even steps of cumulative mean |change| in the
    quantised field, and every frame where something the story depends on
    happens is kept as well: the reported route changes, or (Physarum) the
    walk flips between reaching and dead-ending, or a different extraction
    method answers. First and last are always kept. The page states the true
    iteration on every frame."""
    n = len(frames)
    if n <= k:
        return list(range(n))
    q = [np.rint(f["field"] * 255.0) for f in frames]
    change = np.zeros(n)
    for t in range(1, n):
        change[t] = float(np.abs(q[t] - q[t - 1]).mean())
    cum = np.cumsum(change)
    keep = {0, n - 1}
    if cum[-1] > 0:
        for target in np.linspace(0.0, cum[-1], k):
            keep.add(min(n - 1, int(np.searchsorted(cum, target))))
    for t in range(1, n):
        a, b = frames[t - 1], frames[t]
        if a.get("best") != b.get("best"):
            keep.add(t)
        if "reached" in b and (a.get("reached") != b.get("reached")
                               or a.get("method") != b.get("method")):
            keep.add(t)
    return sorted(keep)


def rest_frame(frames: list, kept: list[int]) -> int:
    """Position, within the KEPT frames, of the frame the page opens on: the
    first one after the field has differentiated (under 35% of edges still at
    half-scale or above), so the panel shows structure before anyone presses
    Play. Falls back to the middle frame if the field never differentiates."""
    for pos, t in enumerate(kept):
        if pos == 0:
            continue
        if (frames[t]["field"] >= 0.5).mean() < 0.35:
            return pos
    return len(kept) // 2


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------

# Both fields are displayed on a LOG scale over this many decades. Conductivity
# decays geometrically, by (1 - dt) per iteration on a starved tube, and so does
# pheromone, by (1 - rho), so equal visual steps then mean equal amounts of
# decay. On a linear scale the tubes beside the source and sink, which carry the
# whole flux, dwarf everything else and nearly the entire field rounds to zero
# when quantised to 0..255. The transform is applied HERE, before quantisation,
# because quantising linearly first would already have discarded the low end.
LOG_DECADES = 3.0


def _log_unit(ratio: np.ndarray) -> np.ndarray:
    """Map a ratio in (0, 1] to [0, 1] on a LOG_DECADES-decade log scale; 0
    and anything below the bottom decade map to 0."""
    out = np.zeros_like(ratio)
    pos = ratio > 0
    out[pos] = 1.0 + np.log10(ratio[pos]) / LOG_DECADES
    return np.clip(out, 0.0, 1.0)


def _physarum_field(router, global_index: dict) -> np.ndarray:
    """Conductivity on the GLOBAL (pre-closure) tube list, relative to the
    strongest LIVE tube, on a log scale. Normalising per frame is principled
    here, not just convenient: Phase 9b-mech proved the walk is
    scale-invariant in D, so only the field's shape carries meaning, never its
    level."""
    D = np.asarray(router._D, dtype=np.float64)
    active = getattr(router, "_active_edge", None)
    live = D if active is None else D[np.asarray(active, dtype=bool)]
    top = float(live.max()) if live.size else 0.0
    ratio = np.zeros(len(global_index), dtype=np.float64)
    if top <= 0:
        return ratio
    for i, pair in enumerate(router._pairs):
        g = global_index.get(pair)
        if g is not None:
            ratio[g] = D[i] / top
    return _log_unit(np.clip(ratio, 0.0, 1.0))


def _aco_field(router, global_index: dict) -> np.ndarray:
    """Pheromone ABOVE the current MMAS floor, as a fraction of the floor-to-
    ceiling span, on the same log scale. Edges sitting at tau_min are 0."""
    tau = np.asarray(router._pheromone_arr, dtype=np.float64)
    lo, hi = float(router._tau_min), float(router._tau_max)
    span = hi - lo if hi > lo else 1.0
    ratio = np.zeros(len(global_index), dtype=np.float64)
    for i, key in enumerate(router._edge_keys):
        g = global_index.get(key)
        if g is not None:
            ratio[g] = (tau[i] - lo) / span
    return _log_unit(np.clip(ratio, 0.0, 1.0))


def _stretch(snapshot, G, path, ref_cost):
    if not path:
        return None
    return float(snapshot.cost_of_path(G, path)) / float(ref_cost)


def run_phase(router, algorithm, budget, global_index, snapshot, G_eval, ref_cost):
    """`run_to_convergence`, line for line, recording state every iteration."""
    frames = []
    iterations = 0
    for i in range(1, budget + 1):
        D_pre = np.array(router._D, copy=True) if algorithm == "physarum" else None
        router.step()
        iterations = i

        best = router.best_path()
        fr = {
            "i": i,
            "best": [int(n) for n in best] if best else None,
            "stretch": _stretch(snapshot, G_eval, best, ref_cost),
        }
        if algorithm == "physarum":
            walk, reached = replay_walk(router, D_pre)
            method = router.last_extraction_method()
            extracted = router._path_history[-1] if router._path_history else None
            fr.update({
                "field": _physarum_field(router, global_index),
                "walk": [int(n) for n in walk],
                "reached": bool(reached),
                "method": method,
                # the path this iteration's extraction actually returned:
                # the walk itself if it reached, otherwise a fallback's
                "extracted": [int(n) for n in extracted] if extracted else None,
                "best_method": router.best_path_extraction_method(),
            })
        else:
            ant = router._prev_iteration_best_path
            fr.update({
                "field": _aco_field(router, global_index),
                "ant": [int(n) for n in ant] if ant else None,
            })
        frames.append(fr)
        if router.converged():
            break
    return frames, iterations


def export_phase(frames, n_iter):
    kept = pick_frames(frames)
    out = []
    for t in kept:
        g = dict(frames[t])
        g["field"] = encode_field(frames[t]["field"])
        if g.get("stretch") is not None:
            g["stretch"] = round(g["stretch"], 5)
        out.append(g)
    return {"n_iter": int(n_iter), "frames": out, "rest": rest_frame(frames, kept)}


def run_algorithm(ctx, algorithm, od, removed, G, G_dis, snapshot,
                  ref_cost, dis_ref_cost, global_index):
    spec = ctx.cfg.algorithm(algorithm)
    budget = int(spec.budget)
    result = {}

    for branch in ("warm", "cold"):
        router = make_router(algorithm, ctx.options(algorithm))
        router.prepare(G, od.source, od.target, snapshot, SEED)
        f0, n0 = run_phase(router, algorithm, budget, global_index,
                           snapshot, G, ref_cost)
        initial_best = router.best_path()
        router.apply_disruption(list(removed), warm=(branch == "warm"))
        f1, n1 = run_phase(router, algorithm, budget, global_index,
                           snapshot, G_dis, dis_ref_cost)

        if branch == "warm":
            result["initial"] = export_phase(f0, n0)
            result["_initial_check"] = (n0, initial_best,
                                        f0[-1]["stretch"] if f0 else None)
        else:
            # The pre-closure run is deterministic for a fixed seed, so the
            # cold branch's must equal the warm branch's exactly. If it does
            # not, the two recovery branches are not continuations of the
            # same run and side-by-side comparison would be meaningless.
            w_n, w_best, _ = result["_initial_check"]
            assert n0 == w_n and router_path_equal(initial_best, w_best), (
                f"{algorithm}: pre-closure run not reproducible across branches")
        result[branch] = export_phase(f1, n1)
        result["_" + branch + "_n"] = n1
        result["_" + branch + "_stretch"] = f1[-1]["stretch"] if f1 else None
    return result


def router_path_equal(a, b):
    return (a is None and b is None) or (a is not None and b is not None
                                         and list(a) == list(b))


# --------------------------------------------------------------------------
# cross-check against the evidence file
# --------------------------------------------------------------------------

def crosscheck(df, od, scenario, algorithm, res, tol=1e-6):
    sel = df[(df.algorithm == algorithm) & (df.od_source == od.source)
             & (df.od_target == od.target) & (df.scenario_id == scenario)
             & (df.traffic_level == LEVEL) & (df.seed == SEED)]
    report = []
    n0, _best, s0 = res["_initial_check"]
    for branch in ("warm", "cold"):
        row = sel[sel.warm_start == (branch == "warm")]
        if len(row) != 1:
            raise SystemExit(f"crosscheck: expected 1 Parquet row for "
                             f"{algorithm} {branch}, found {len(row)}")
        r = row.iloc[0]
        checks = [
            ("initial iterations", int(r.iterations), int(n0)),
            ("initial stretch", float(r.stretch), s0),
            ("recovery iterations", int(r.recovery_iterations),
             int(res["_" + branch + "_n"])),
            ("post-disruption stretch", float(r.post_disruption_stretch),
             res["_" + branch + "_stretch"]),
        ]
        for name, want, got in checks:
            ok = (got is not None and abs(float(want) - float(got)) <= tol)
            report.append((f"{algorithm} {branch} {name}", want, got, ok))
    return report


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    print("loading context ...", flush=True)
    ctx = traces.load_context()
    G = ctx.graph
    snapshot = ctx.snapshot(LEVEL)
    df = pd.read_parquet(PARQUET)

    # Global edge lists, pre-closure, shared by every cell so the page only
    # has to resolve geometry once per algorithm.
    probe_p = make_router("physarum", ctx.options("physarum"))
    od0 = ctx.od(*SHOWCASE[0]["od"])
    probe_p.prepare(G, od0.source, od0.target, snapshot, SEED)
    phys_edges = list(probe_p._pairs)
    probe_a = make_router("aco", ctx.options("aco"))
    probe_a.prepare(G, od0.source, od0.target, snapshot, SEED)
    aco_edges = list(probe_a._edge_keys)
    phys_index = {p: i for i, p in enumerate(phys_edges)}
    aco_index = {k: i for i, k in enumerate(aco_edges)}
    print(f"  physarum tubes {len(phys_edges):,}, aco directed edges "
          f"{len(aco_edges):,}", flush=True)

    all_checks = []
    cells_meta = []
    for ci, spec in enumerate(SHOWCASE):
        od = ctx.od(*spec["od"])
        scenario = spec["scenario"]
        print(f"\n[{ci}] {od.source}->{od.target} {scenario}: {spec['title']}",
              flush=True)

        ref_path, ref_cost = dijkstra_reference(G, od.source, od.target, snapshot)
        dis = generate_disruption(
            G, od.source, od.target, snapshot, scenario,
            seed=ctx.cfg.scenario_seed, path=ref_path,
            config=_disruption_config(ctx.cfg.disruption))
        if not dis.ok:
            raise SystemExit(f"scenario infeasible: {dis.reason}")
        removed = list(dis.removed_edges)
        G_dis = apply_disruption(G, removed, copy=False)
        dis_ref_path, dis_ref_cost = dijkstra_reference(
            G_dis, od.source, od.target, snapshot)

        runs = {}
        for algorithm, index in (("physarum", phys_index), ("aco", aco_index)):
            t1 = time.time()
            res = run_algorithm(ctx, algorithm, od, removed, G, G_dis, snapshot,
                                ref_cost, dis_ref_cost, index)
            checks = crosscheck(df, od, scenario, algorithm, res)
            all_checks.extend(checks)
            bad = [c for c in checks if not c[3]]
            print(f"  {algorithm}: initial {res['initial']['n_iter']} it, "
                  f"warm {res['warm']['n_iter']} it, cold {res['cold']['n_iter']} "
                  f"it  ({time.time()-t1:.0f}s)  crosscheck "
                  f"{len(checks)-len(bad)}/{len(checks)}", flush=True)
            for c in bad:
                print(f"    MISMATCH {c[0]}: parquet {c[1]} vs regenerated {c[2]}",
                      flush=True)
            runs[algorithm] = {k: v for k, v in res.items() if not k.startswith("_")}

        cell = {
            "key": f"{od.source}|{od.target}|{scenario}|{LEVEL}",
            "title": spec["title"],
            "story": spec["story"],
            "od_source": int(od.source), "od_target": int(od.target),
            "stratum": od.stratum, "scenario_id": scenario,
            "removed_edges": [[int(u), int(v), int(k)] for u, v, k in removed],
            "reference_path": [int(n) for n in ref_path],
            "reference_cost": float(ref_cost),
            "disrupted_reference_path": [int(n) for n in dis_ref_path],
            "disrupted_reference_cost": float(dis_ref_cost),
            "runs": runs,
        }
        path = out_dir / f"cell{ci}.json"
        path.write_text(json.dumps(cell, separators=(",", ":")), encoding="utf-8")
        print(f"  wrote {path.name}  {path.stat().st_size/1e6:.2f} MB", flush=True)
        cells_meta.append({"file": f"cell{ci}.json", "key": cell["key"],
                           "title": spec["title"], "story": spec["story"],
                           "scenario_id": scenario, "stratum": od.stratum})

    n_bad = sum(1 for c in all_checks if not c[3])
    if n_bad:
        raise SystemExit(f"\nABORT: {n_bad} cross-check mismatch(es) against the "
                         f"Parquet. Not writing the index; the animations would "
                         f"not depict the runs in the evidence file.")

    index = {
        "regenerated": True,
        "level": LEVEL, "seed": SEED,
        "config_hash": ctx.cfg.config_hash,
        "baseline_hash": ctx.meta.get("baseline_hash"),
        "frames_per_phase": FRAMES_PER_PHASE,
        "crosscheck": {"n": len(all_checks), "passed": len(all_checks) - n_bad},
        "edges": {
            "physarum": [f"{u}|{v}" for u, v in phys_edges],
            "aco": [f"{u}|{v}|{k}" for u, v, k in aco_edges],
        },
        "cells": cells_meta,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ip = out_dir / "index.json"
    ip.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
    total = sum(p.stat().st_size for p in out_dir.glob("*.json"))
    print(f"\nindex.json {ip.stat().st_size/1e6:.2f} MB; total {total/1e6:.2f} MB; "
          f"crosscheck {len(all_checks)}/{len(all_checks)}; "
          f"elapsed {time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
