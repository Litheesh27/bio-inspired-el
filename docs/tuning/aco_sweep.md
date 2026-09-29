# Phase 4 ACO (MMAS) hyperparameter sweep

This is the reproducibility record for `src/bioroute/algorithms/aco.py`'s
final hyperparameters. Every configuration tried is kept, including the
ones that missed the exit criterion -- see CLAUDE.md's honest-reporting
note and the Phase 4 spec ("keeping the losing configurations matters").

## Exit criterion

On >=90% of a 30-OD-pair sample, ACO reaches `stretch <= 1.10` within the
iteration budget, measured under both freeflow and BPR-heavy congestion.

## Two separate problems, two separate fixes -- and why they were split apart

The literal spec (`eta = 1/travel_time`, full-path node tabu, discard on
dead end) was measured at a **0% ant completion rate** on this graph, for
every beta in {1, 2, 3, 4} and regardless of tabu-window size. Central
Bengaluru's road graph has mean out-degree ~2.5 (19% of nodes are
out-degree 1), and sampled OD pairs commonly sit 30-40 hops apart.
Diagnostic traces showed ants walking into small residential loops /
cul-de-sacs with only two out-edges, both already on the ant's own path --
a genuine dead end with no forward-only escape.

Two independent changes were tried:

1. **Backtracking construction** (`_construct_ant` in `aco.py`): on a local
   dead end, pop back to the last node with an untried branch (bounded by
   `max_steps * backtrack_multiplier` total operations) instead of
   discarding the ant on the spot. Always on -- not a config switch, since
   it fixes a structural graph-topology problem rather than a modeling
   choice.
2. **Goal-directed heuristic** (`use_goal_heuristic` in `configs/aco.yaml`):
   `eta(u,v,k) = 1/(travel_time(u,v,k) + h(v))`, `h(v)` = the same
   admissible great-circle/global-max-speed heuristic `AStarRouter` uses.

**These fix different things, and conflating them would have been a
methodological mistake.** Giving ACO the same target-location knowledge
A*'s heuristic encodes is not apples-to-apples against the A* baseline,
and Physarum has no equivalent mechanism -- so before treating the goal
heuristic as load-bearing, the actual question was isolated empirically:
**does backtracking alone restore completion, with the heuristic off?**

### Ablation: ant completion rate, backtracking ON, goal heuristic OFF

Measured directly (`_empty_iterations` / total iterations) on 3 real OD
pairs (30-41 hops), `beta_no_heuristic=4.0` (pre-tuning value), budget=400:

| OD pair (hops) | iterations run | empty iterations | completion rate |
|---|---|---|---|
| ~30 hops | 42 (converged) | 0 | 100% |
| ~27 hops | 39 (converged) | 0 | 100% |
| ~41 hops | 400 (budget exhausted) | 1 | 99.75% |

**Backtracking alone restores ant completion from 0% to effectively 100%.**
The goal heuristic is NOT necessary for completion -- confirming the
coordinator's hypothesis. It is therefore implemented as a config switch
(`use_goal_heuristic`, default **false**), not a hardcoded default, and the
project's headline result runs with it off.

### What backtracking-only (no heuristic) buys and costs: convergence quality

With ants completing but no directional signal, MMAS converges -- but to
whichever path its early random exploration happens to reinforce, which is
frequently NOT close to optimal. Deep-dive on one representative OD pair
(source/target ~30 hops apart, Dijkstra cost 357.27s, freeflow):

| n_ants | k_stall / branching threshold | budget | outcome |
|---|---|---|---|
| 40 | 60 / 1.02 | 300 | converges at iter 107, stretch=1.212 (misses 1.10) |
| 40 | 100 / 1.005, tau_min_ratio=50 | 300 | same stretch=1.212 (looser convergence criteria did not help) |
| 60 | 100 / 1.005, tau_min_ratio=50 | 300 | same again (more ants alone did not help) |
| 40 | never converges (forced) | 1500 | 2.66 (iter1) -> 1.212 (iter100) -> **1.157 (iter600)**, flat through iter1500 |
| 150 | never converges (forced) | 450 | reaches 1.157 by iter150 (faster in iteration count, ~2x slower per iteration -- no net wall-clock win), flat through iter450 |
| 40 (seeds 0-5) | k_stall=60 / 1.02 | 300 | stretch alternates between exactly 1.2118 and 1.1413 regardless of seed -- two attractors, not a random-seed artifact |

**Finding:** for this OD pair, backtracking-only MMAS appears to hit a
genuine structural local-optimum ceiling around stretch 1.157, regardless
of ant count, evaporation, convergence looseness, or seed. This is an
honest negative result for that pair, not a tuning failure -- see the
30-OD-pair measurement below for how often this happens across the sample.
On a *different*, harder OD pair (~41 hops) a **budget of 800** did reach
stretch 1.053 (meets the criterion), showing outcomes are pair-dependent:
some pairs need a large budget and get there; at least one plateaus above
the threshold no matter the budget tried.

**Conclusion driving the final config:** the no-heuristic mode needs a
materially larger iteration budget than the goal-heuristic mode to have any
chance of meeting the exit criterion on longer OD pairs (`suggested_budget:
600` vs. `suggested_budget_goal_heuristic: 250`), and even then is not
guaranteed to succeed on every pair. This is the honest cost of having no
target-location information, reported here rather than papered over.

## Coarse sweep, goal-heuristic mode (5 OD pairs, freeflow, budget=100, n_ants=40)

Run before the methodology pivot, kept as part of the reproducibility
record (`alpha=1.0` fixed, seed 2024 sample of 5 OD pairs):

| beta | rho | n_ants | frac <=1.10 | median stretch | worst stretch | median iters | wallclock/pair (s) |
|---|---|---|---|---|---|---|---|
| 3.0 | 0.1 | 40 | 0.80 (4/5) | 1.000 | 1.364 | 89 | 31.7 |
| 3.0 | 0.2 | 40 | 0.60 (3/5) | 1.043 | 1.407 | 67 | 25.8 |

(Sweep over beta in {3,4,5} x rho in {0.1,0.2} was interrupted mid-run by
the methodology pivot to also measure the no-heuristic mode; beta=3.0 was
carried forward and beta=4.0 kept as the goal-heuristic default in
`configs/aco.yaml` based on earlier single-pair testing that showed 4/5
pairs at stretch<=1.10, consistent with the beta=3.0 row above.)

## Tuning search, no-heuristic (headline) mode

Starting point `beta=4.0` (the goal-heuristic-tuned value) was first
re-checked with the heuristic off and found to give poor/inconsistent
stretch; `beta=1.0` (less aggressive exploitation of purely-local edge
speed, since there is no directional correction available) was more
stable across the single-pair deep-dive above and is the value shipped.
`k_stall` was raised 20 -> 60 and `branching_factor_threshold` tightened
1.05 -> 1.02 so the no-heuristic mode does not lock into a suboptimal
plateau within a handful of iterations (it still can, and does, plateau --
see the ablation above -- but not artificially early).

## Final tuned configuration

See `configs/aco.yaml`. Headline (default): `use_goal_heuristic=false`,
`alpha=1.0`, `beta_no_heuristic=1.0`, `rho=0.1`, `n_ants=40`,
`k_stall=60`, `branching_factor_threshold=1.02`, `tau_min_ratio=20`,
`suggested_budget=600`. Ablation mode: `use_goal_heuristic=true`,
`beta=4.0`, same `rho`/`n_ants`, `suggested_budget_goal_heuristic=250`.

## 30-OD-pair exit-criterion measurement

<!-- FILLED IN BELOW ONCE MEASURED -->

---

# Phase 4b performance record

Phase 4b is a **speed-only** change: `src/bioroute/algorithms/aco.py` was
rewritten for throughput so the Phase 8 sweep becomes affordable. Nothing
above this line changes -- the rewrite reproduces the Phase 4
implementation **bit-for-bit** (see "Equivalence" below), so every tuning
result, the goal-heuristic ablation, and the stretch-1.157 plateau finding
still describe the shipped code exactly.

## Measurement conditions (read this before comparing any number)

This laptop's absolute throughput varies by up to **3.4x** depending on its
power/thermal state: an identical repeated workload was measured at ~80 ms
per batch for the first ~1.3 s of sustained single-threaded load and then
~210-290 ms per batch from then on, and separate runs of the very same code
came out at 0.067 s/iteration in one session and 0.25 s/iteration in
another. Any "before vs after" taken from two different runs is therefore
meaningless. All speedup figures below come from an **interleaved**
benchmark: the variants are round-robined within a single process on the
same OD pair with the same seed, so throttling hits all of them equally.
Absolute figures state which regime they were taken in.

## Before / after (interleaved, same process, same OD pair, n_ants=40)

Measured on OD (10011007443, 459793784) -- pair A, the 59-hop pair used for
the Phase 4b profile -- and on OD (1566702638, 10026627726) -- pair B:

| variant | s/iteration (pair A) | s/iteration (pair B) | vs. Phase 4 |
|---|---|---|---|
| Phase 4 (committed, dict-keyed pheromone) | 0.186 | 0.177 | 1.00x |
| numpy-per-candidate attempt (uncommitted WIP) | 0.756 | 0.750 | **0.25x** |
| Phase 4b (shipped) | 0.081 | 0.083 | **2.3x** |

Across 5 sampled OD pairs (`scripts/bench_aco.py --pairs 5`), Phase 4b runs
at **0.067 s/iteration mean** (0.039-0.076 by pair) in the fast regime and
~0.25 s/iteration in the throttled regime; the Phase 4 implementation is
2.1-2.7x slower than it in whichever regime both are measured.

**Note on the earlier WIP:** the intermediate "vectorize with numpy"
attempt in the working tree was not a 6% improvement over Phase 4, it was a
**4x regression** (0.756 vs 0.186 s/iteration). Applying numpy per
candidate set -- fancy-indexing, `cumsum`, `searchsorted` and an `np.ones`
mask over arrays of 1-3 elements -- costs far more in dispatch overhead
than the arithmetic it replaces. That is the single most useful negative
result from this phase.

## Operations per ant (unchanged, and that is the point)

`ops` = forward steps + backtracks inside `_construct_ant`, the quantity
bounded by `op_budget = max_steps * backtrack_multiplier`.

| | Phase 4 | Phase 4b |
|---|---|---|
| ops/ant, 5-pair mean | 4,716 | 4,716 |
| ops/ant, pair A | 5,176 (= its budget 5,175: every ant exhausts it) | 5,176 |
| forward moves/ant, pair A | 2,687 | 2,687 |
| ant completion rate, 5-pair mean | 26.2% | 26.2% |

The speedup is therefore **entirely "each operation got cheaper"**, not
"we stopped doing pointless operations". At ~200,000 operations per
iteration, Phase 4 spent ~0.9 us per operation and Phase 4b spends ~0.37 us
(fast regime).

## What bought the speedup, and what did not

Helped, in rough order of contribution:

1. **De-numpy-ing the per-candidate path** (the WIP's 4x regression
   removed, plus dense integer indexing instead of `(u, v, k)` dict
   lookups, and a `bytearray` tabu instead of a node-id set).
2. **Per-iteration weight cache** (`_refresh_weight_cache`):
   `tau^alpha * eta^beta` is constant within an iteration, so all ~7,000
   weights are computed once per iteration instead of ~110,000 times.
3. **Inlining `_weighted_candidates` / `_pick_and_remove` into
   `_construct_ant`**: two Python calls per hop x ~110,000 hops per
   iteration. The methods are kept as the reference form and
   `tests/test_aco.py::test_aco_hot_loop_matches_reference_helpers_*`
   asserts the inlined loop makes identical choices.
4. **Single-candidate fast path** (61% of picks are 1-element sets; the RNG
   draw is still consumed, so the random stream is unchanged) and tracking
   `depth` instead of recomputing `len(path_idx)`.

Measured and rejected (no gain, so not shipped):

* Hand-unrolling the candidate build for out-degree 1 and 2 (~0%).
* Fast-forwarding consecutive backtracks in an inner loop (~3%, within
  noise).
* `gc.disable()` / `gc.freeze()` around construction (~0%; the loop's
  allocations are refcount-freed and no GC passes fire during it).
* numpy anywhere on the per-candidate path (4x slower, see above).

## Fix 2 investigated: is the backtracking thrashing, and does a memo help?

Yes, it thrashes, badly. Per ant on pair B: **2,793 forward moves but only
320 distinct nodes entered**, with the most-visited node re-entered ~129
times. Ants re-explore the same dead subtrees from different parents,
because "dead" under a node tabu is context-dependent and nothing
remembers it.

A per-ant memo (mark a node dead once the ant has backtracked out of it,
refuse to re-enter it for the rest of that ant's construction) was
prototyped and measured on 5 pairs x 2 seeds, budget 40:

| OD pair | ops/ant base -> memo | completion base -> memo | stretch base -> memo |
|---|---|---|---|
| A (59 hops) | 5,173 -> 3,223 | 0% -> 45% | 5.10 -> 1.44 |
| (20 hops) | 1,651 -> 1,009 | 70% -> 100% | 1.000 -> 1.000 |
| (46 hops) | 3,770 -> 2,976 | 30% -> 65% | 1.222 -> 1.104 |
| B (67 hops) | 5,131 -> 2,937 | 6% -> 55% | 1.796 -> 1.102 |
| (38 hops) | 4,764 -> 2,140 | 12% -> 86% | 1.944 -> 1.442 |

**It is not shipped.** Two reasons, in order:

1. It is a **search-behaviour change**, which Phase 4b explicitly is not
   allowed to make: a node's deadness depends on the visited set in force
   when it was popped, so the memo prunes branches that were genuinely
   still open. Everything above this line -- the tuning record, the
   ablation, the plateau finding -- was measured without it.
2. It does not solve the problem Phase 4b exists to solve anyway: ops/ant
   fall ~40%, but wall-clock falls only ~10%, because ants that now
   complete pay for path costing and longer successful walks.

It is, however, a strong **Phase 4 quality** lead: completion rate roughly
triples and stretch improves on every pair measured. Whether MMAS ants here
should carry a dead-end memory is an algorithm decision for the project
owner, and adopting it would require re-running the tuning sweep and the
exit criterion. Recorded here so the measurement is not lost.

## Equivalence with Phase 4: identity, not just statistics

The Phase 4b spec permitted an RNG-stream change and therefore only
required *statistical* equivalence. The rewrite kept `random.Random(seed)`,
the same candidate ordering and bit-identical weights, so the stronger
property holds: **identical output**.

Verified on 5 real OD pairs x 3 seeds (budget 50) plus the toy graph: final
cost, stretch, iterations, convergence flag, iteration of first valid path,
path hash, last-3 trace values, ops/ant and ant completion rate --
**15 runs x 10 fields, zero differences**. Summary of that sample (the same
for both implementations): median stretch 1.461, mean 1.979, 14/15 runs
found a path, mean completion 26.2%, mean ops/ant 3,965.

## Phase 8 projection

At the 5-pair mean of 0.067 s/iteration (fast regime) and
`suggested_budget=600` with no early convergence: ~40 s per solve,
**47 serial hours** for 4,200 solves, **~4.7 hours at 10x joblib
parallelism**. In the throttled regime those become ~150 s/solve, 175
serial hours and ~17 hours at 10x. A long sweep saturating 14 cores will
sit nearer the throttled end, so plan for the pessimistic figure; either
way this is down from the 588 serial hours quoted in CLAUDE.md for Phase 4.

Re-measure any time with `.venv/Scripts/python.exe scripts/bench_aco.py`,
which prints seconds/iteration (sustained and burst), operations per ant,
ant completion rate and both projections.
