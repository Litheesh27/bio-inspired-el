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

---

# Phase 4c: the per-ant dead-node memo, re-tune, and the exit criterion

Phase 4c **adopts** the per-ant dead-node memo that Phase 4b measured and
deliberately did not ship (see "Fix 2 investigated" above). Everything
above this line was measured WITHOUT the memo and is retained as the
historical record -- in particular **the stretch-1.157 plateau finding and
the whole "Tuning search, no-heuristic (headline) mode" section above are
now historical**: they describe a search that failed ~74% of its ants, and
the memo changes search behaviour, so they no longer describe the shipped
default. They are kept because the paper's ablation table needs them.

## What was shipped

`configs/aco.yaml` gains `use_dead_node_memo` (default **true**). When true,
a node the ant backtracks out of KEEPS its `visited` mark, so that ant can
never re-enter it from any other parent for the rest of its construction;
`visited` does double duty as path tabu and dead-node memo. Resumed
backtrack frames are re-filtered against the grown memo. `false` restores
the exact Phase 4/4b search, bit-for-bit.

## HONESTY: this is a lossy construction heuristic, not an optimization

Whether a node is dead is **context-dependent on the ant's visited set**.
A node with no continuation given one prefix may be perfectly viable given
another. Memoising deadness across an ant's whole construction therefore
prunes branches that are genuinely still open: the ant's reachable search
space becomes a strict subset of the true one. It must be reported in the
paper as a construction heuristic of this ACO variant -- on the same
footing as backtracking construction itself -- never as an implementation
detail.

### Measured: how often does the memo prune a still-viable branch?

Directly instrumented (an `ACORouter` subclass that re-implements
`_construct_ant` with auditing; the shipped hot path is untouched). Every
time the memo refuses a candidate whose head is marked in `visited` but is
NOT currently on the ant's path, the event is sampled at p=0.02 and a BFS
asks: forbidding ONLY the nodes currently on the path (the ant's true
tabu), could that head still have reached the target? 6 OD pairs from the
seed-2024 sample, freeflow, 5 iterations x 40 ants each.

| OD pair | prune events | audited | still-viable | ant completion |
|---|---|---|---|---|
| (10011007443, 459793784) | 115,643 | 2,376 | 73.4% | 28% |
| (10060571772, 1859880688) | 60,715 | 1,249 | 42.9% | 100% |
| (459815975, 6134427897) | 111,031 | 2,277 | 62.6% | 53% |
| (1566702638, 10026627726) | 114,125 | 2,333 | 66.5% | 39% |
| (1410617123, 11432437464) | 71,717 | 1,500 | 59.3% | 81% |
| (10017990198, 2406092355) | 112,214 | 2,296 | 77.6% | 18% |
| **TOTAL** | **585,445** | **12,031** | **65.9%** | -- |

**~66% of memo prunes refuse a node that could still have reached the
target.** The memo is aggressively lossy, and that is almost certainly why
it works: it buys tractability with completeness.

Separately, **8.17%** of all backtracks (139,054 of 1,702,137) were forced
by the ant's FORWARD step budget (`depth >= max_steps`) rather than by an
actual dead end -- so roughly one backtrack in twelve marks a node dead
that was not dead at all, purely because the ant had walked too far.

Caveat on the 65.9%: "could still reach the target" is a reachability
statement, not a claim that the ant would have found a better path down
that branch, nor that the resulting colony-level solution would be better.
It is an upper bound on how much genuine search space the memo discards,
and a lower bound on nothing. The colony-level effect -- what the memo
does to stretch and completion -- is the memo on/off ablation below.

## Re-tune with the memo on (coarse)

The memo invalidates the pre-memo tuning above, so the headline
(`use_goal_heuristic: false`) parameters were re-swept. Method: 10 OD pairs
from the same seed-2024 sample, freeflow, seed 0, budget 400, and the
per-pair stretch TRACE recorded so every smaller budget can be scored post
hoc from the same run (no re-running needed). Solves run in parallel
(joblib, 10 workers), so the wall-clock column from these runs is not
comparable between rows and is omitted -- speed is measured separately by
`scripts/bench_aco.py`, interleaved in one process.

### beta_no_heuristic (rho=0.1, n_ants=40, k_stall=60, memo on)

Fraction of the 10 pairs at stretch<=1.10, scored at several budgets from
one run each:

| beta | b=50 | b=100 | b=200 | b=400 | median stretch (b=400) | worst (b=400) | ant completion |
|---|---|---|---|---|---|---|---|
| **0.5** | **70%** | **70%** | **70%** | **70%** | **1.000** | 1.347 | 77% |
| 1.0 (pre-memo value) | 30% | 60% | 60% | 70% | 1.011 | 1.335 | 78% |
| 2.0 | 20% | 20% | 20% | 20% | 1.254 | 1.573 | 67% |
| 3.0 | 20% | 20% | 20% | 20% | 1.293 | 1.675 | 69% |
| 4.0 | 20% | 20% | 20% | 20% | 1.464 | 1.965 | 68% |

Two things fall out immediately:

1. **Lower beta is better with the memo on**, and the pre-memo choice of
   1.0 is no longer the best: beta=0.5 reaches the same fraction at
   budget 50 that beta=1.0 needs budget 400 to reach, and its median
   stretch is an exact match (1.000).
2. **The budget can come down a long way.** At beta=0.5 nothing improves
   after ~50 iterations: the fraction is flat 70% from b=50 to b=400 and
   the median is already 1.000. The pre-memo `suggested_budget: 600` was
   sized for a search that failed ~74% of its ants; with the memo the
   extra iterations buy nothing.
3. Ant completion is **67-78%** across every beta, against **26%** without
   the memo (Phase 4b, same graph and pair population). That is the memo's
   headline effect.

Extending the search downward (same protocol, budget 200):

| beta | b=25 | b=50 | b=100 | b=200 | median (b=200) | worst (b=200) | ant completion |
|---|---|---|---|---|---|---|---|
| 0.0 (pheromone only) | 50% | 60% | 60% | 80% | 1.019 | 1.394 | 82% |
| 0.25 | 40% | 60% | 60% | 60% | 1.000 | 1.344 | 81% |
| 0.5 | 40% | 70% | 70% | 70% | 1.000 | 1.355 | 79% |
| 0.75 | 20% | 50% | 60% | 60% | 1.003 | 1.428 | 79% |

Everything in beta in [0, 0.75] lands in a 60-80% band. At n=10 pairs one
pair is 10 percentage points, so these rows are **not** separable from each
other -- the honest reading is that beta anywhere below ~1 is equivalent
and beta >= 2 is clearly worse. beta=0.5 is carried forward (best median,
and the only value whose fraction is already flat by b=50).

### rho (beta_no_heuristic=0.5, n_ants=40, budget 200, memo on)

| rho | frac <=1.10 | median stretch | worst | median iters | ant completion |
|---|---|---|---|---|---|
| 0.02 | 60% | 1.047 | 1.494 | 200 (never converged) | 71% |
| **0.05** | **80%** | **1.000** | 1.352 | 172 | 77% |
| 0.1 (previous value) | 70% | 1.000 | 1.355 | 130 | 79% |
| 0.2 | 70% | 1.000 | 1.301 | 136 | 81% |
| 0.4 | 60% | 1.002 | 1.301 | 99 | 81% |

Again a 60-80% band that one pair can move by 10 points; rho in
[0.05, 0.2] is indistinguishable, rho=0.02 (too slow to reinforce) and
rho=0.4 (over-evaporates) are the only clearly worse ends.

### n_ants (beta_no_heuristic=0.5, rho=0.1, memo on)

Fraction at stretch<=1.10, scored at several budgets from one run each:

| n_ants | b=25 | b=50 | b=100 | b=150 | b=200 | median (b=200) | worst | ant completion |
|---|---|---|---|---|---|---|---|---|
| 10 | 20% | 20% | 40% | 40% | 50% | 1.148 | 1.440 | 79% |
| 20 | 20% | 40% | 50% | 50% | 60% | 1.000 | 1.377 | 81% |
| 40 (previous value) | 40% | 70% | 70% | 70% | 70% | 1.000 | 1.355 | 79% |
| **80** | 40% | **80%** | **80%** | **90%** | **90%** | **1.000** | **1.289** | 78% |

**This is the one parameter that moves the criterion.** Colony size, not
beta/rho/budget, is what the memo changed the sensitivity to: with ~78% of
ants now completing, doubling the colony doubles the number of usable
candidate paths per iteration, whereas pre-memo (26% completion) more ants
mostly bought more wasted constructions -- which is exactly why the
pre-memo sweep found "more ants alone did not help".

Cost note: n_ants=80 is ~2x the per-iteration work of 40, but reaches its
plateau at roughly b=150 instead of needing 200+, so per SOLVE it is about
1.6x, not 2x. Ant completion is flat at ~78-81% across colony sizes, as
expected -- completion is a per-ant property.

### Confirming the combination (10 pairs, budget 200, memo on)

`rho` was swept at n_ants=40 and `n_ants` at rho=0.1, so the winning
combination was re-checked directly:

| config | b=50 | b=100 | b=150 | b=200 | median | worst | median iters |
|---|---|---|---|---|---|---|---|
| beta 0.5, rho 0.05, n_ants 80 | 50% | 80% | 80% | 80% | 1.000 | 1.312 | 140 |
| **beta 0.5, rho 0.1, n_ants 80** | **80%** | **80%** | **90%** | **90%** | **1.000** | **1.289** | **102** |
| beta 0.5, rho 0.05, n_ants 120 | -- | 80% | 80% | 80% | 1.000 | 1.296 | 148 |

rho=0.05's edge at n_ants=40 does not survive at n_ants=80, and n_ants=120
buys nothing over 80 for 1.5x the work. **Final: `beta_no_heuristic=0.5`,
`rho=0.1`, `n_ants=80`, `k_stall=60` (unchanged), `suggested_budget=150`
(down from 600).**

`k_stall` and `branching_factor_threshold` were NOT re-swept -- with the
memo on, median iterations-to-plateau is ~102 and the fraction is flat from
b=150, so the convergence test is no longer the binding constraint. That is
a deliberate scope call for a coarse re-tune, recorded here as a gap rather
than as a result.

**Budget: 600 -> 150.** This is the change that matters for Phase 8: a 4x
cut in iterations against a 2x rise in ants per iteration is a net ~2x
reduction in ant-constructions per solve, on top of the memo cutting
operations per ant by ~40%.

## 30-OD-pair exit-criterion measurement (Phase 4c -- the deliverable)

First measurement of the Phase 4 exit criterion. Protocol: the 30 OD pairs
drawn by `random.Random(2024).sample(sorted(G.nodes()), 2)` (the same
population the whole sweep record uses), seed 0, `stretch =
cost_aco / cost_dijkstra` on the SAME snapshot, `stretch < 1.0` asserted as
a bug per CLAUDE.md. Per-pair stretch traces recorded, so each row can be
scored at any budget from one run.

**Criterion: >=90% of pairs at stretch <= 1.10 within the budget, under
BOTH freeflow and BPR heavy.**

Caveat on independence, stated up front: the 10 pairs used for the coarse
re-tune above are the FIRST 10 of these 30, so a third of this sample is
in-sample for the tuning. A fully held-out measurement would need a fresh
OD draw; this is a known weakness of the number below, not a hidden one.

### Headline mode (`use_goal_heuristic: false`), freeflow

| budget | frac <=1.10 | median stretch | mean | worst | median iters | ant completion | pairs with no path |
|---|---|---|---|---|---|---|---|
| 150 (shipped) | **26/30 = 86.7%** | 1.000 | 1.040 | 1.289 | 106 | 82% | 0 |
| 300 | 26/30 = 86.7% | 1.000 | 1.040 | 1.289 | 106 | 82% | 0 |

**The criterion is MISSED, at 86.7% against a 90% bar -- one pair short.**
Doubling the budget to 300 changes nothing, confirming that the four
failing pairs are plateaus, not slow convergences.

Budget curve on the full 30 pairs (same run, scored from the traces) --
this is the evidence for cutting `suggested_budget` 600 -> 150:

| budget | frac <=1.10 | median | mean | worst |
|---|---|---|---|---|
| 50 | 23/30 = 76.7% | 1.008 | 1.081 | 1.536 |
| 100 | 24/30 = 80.0% | 1.002 | 1.054 | 1.344 |
| **150** | **26/30 = 86.7%** | **1.000** | **1.040** | **1.289** |
| 300 | 26/30 = 86.7% | 1.000 | 1.040 | 1.289 |

Flat from 150 onward: every pair that is going to reach 1.10 has reached it
by ~150 iterations, and the rest never do.

### Headline mode (`use_goal_heuristic: false`), BPR heavy

| budget | frac <=1.10 | median stretch | mean | worst | median iters | ant completion | no path |
|---|---|---|---|---|---|---|---|
| 100 | 28/30 = 93.3% | 1.009 | 1.033 | 1.171 | 100 | 81% | 0 |
| **150 (shipped)** | **29/30 = 96.7%** | **1.009** | 1.028 | **1.171** | 124 | 81% | 0 |
| 300 | 29/30 = 96.7% | 1.009 | 1.027 | 1.171 | 124 | 81% | 0 |

**BPR heavy PASSES the criterion (96.7% >= 90%).**

### Exit criterion verdict: MISSED, on freeflow only, by one pair

| condition | frac at stretch<=1.10, budget 150 | median | worst | median iters | ant completion |
|---|---|---|---|---|---|
| freeflow | **86.7% (26/30)** -- misses 90% | 1.000 | 1.289 | 106 | 82% |
| BPR heavy | **96.7% (29/30)** -- passes | 1.009 | 1.171 | 124 | 81% |

The criterion demands both, so **Phase 4's exit criterion is not met**. The
threshold was NOT lowered and the headline mode was NOT switched to the
goal-directed variant to make it pass; this is the measured number.

**Why freeflow is the harder condition** (this is the interesting part, not
an excuse): under freeflow the optimal route is dominated by a few fast
arterials, so a colony that reinforces a slightly different corridor pays a
large relative penalty -- the four failures sit at stretch 1.13-1.29. Under
heavy BPR congestion those arterials are the most congested links, the cost
surface flattens, many more routes are within 10% of optimal, and the worst
pair drops to 1.171. Bio-inspired local reinforcement is therefore
*relatively better in congestion than in free flow* on this network, which
is a genuinely useful result for the paper and the opposite of what a
"congestion makes routing harder" intuition would predict.

Median stretch is an EXACT match (1.000) under freeflow: the typical pair
is solved optimally, and the miss is entirely in the tail. Reporting the
median alone would look like a pass; it is the 90%-of-pairs framing that
fails, and that framing is the honest one.

### Goal-directed ablation (`use_goal_heuristic: true`), same 30 pairs

Same tuned parameters (`rho=0.1`, `n_ants=80`, memo on), `beta=4.0` as
configured for this mode, budget 150 -- so the ONLY difference from the
headline rows above is `eta = 1/(travel_time + h(v))` instead of
`eta = 1/travel_time`.

| condition | frac <=1.10 | median | mean | worst | median iters | ant completion |
|---|---|---|---|---|---|---|
| goal-directed, freeflow | 26/30 = 86.7% | 1.000 | 1.045 | 1.351 | 90 | 88% |
| goal-directed, BPR heavy | 27/30 = 90.0% | 1.000 | -- | 1.171 | 118 | 86% |
| *(purely local, freeflow)* | *26/30 = 86.7%* | *1.000* | *1.040* | *1.289* | *106* | *82%* |
| *(purely local, BPR heavy)* | *29/30 = 96.7%* | *1.009* | *1.028* | *1.171* | *124* | *81%* |

**What does A*'s heuristic buy this ACO? Essentially nothing.** It matches
the purely-local mode exactly under freeflow (86.7% either way) and is
*worse* under BPR heavy (90.0% vs 96.7%). It converges somewhat sooner
(median 90 vs 106 iterations) and lifts ant completion a few points
(88% vs 82%), but it does not convert that into more pairs meeting the
quality bar, and its worst-case stretch under freeflow is worse
(1.351 vs 1.289).

This is a genuinely useful methodological result. The project's headline
mode was chosen to be purely local for FAIRNESS -- giving ACO the
target-location knowledge A* encodes and Physarum cannot have would have
made the three-way comparison indefensible. It now turns out that choice
also costs nothing in measured quality: the honest comparison and the
favourable one coincide. The `use_goal_heuristic: false` default therefore
needs no apology, and the ablation table can say so.

(Under heavy congestion the heuristic is actively misleading: h(v) is a
free-flow great-circle lower bound, so it steers ants toward geometrically
direct routes exactly when those routes are the congested ones. That is
the most plausible reading of the 90.0% vs 96.7% gap.)

### Memo ON vs OFF, isolated (15 OD pairs, freeflow, identical tuned config)

The only difference between these two rows is `use_dead_node_memo`. Both
use the Phase 4c tuned parameters (`beta_no_heuristic=0.5`, `rho=0.1`,
`n_ants=80`), so this separates the memo's contribution from the re-tune's.

| | memo ON | memo OFF |
|---|---|---|
| frac <=1.10 (budget 150) | **12/15 = 80.0%** | 11/15 = 73.3% |
| frac <=1.10 (budget 300) | 12/15 = 80.0% | 11/15 = 73.3% |
| median stretch | 1.000 | 1.000 |
| mean stretch | **1.042** | 1.083 |
| worst stretch (b=300) | **1.289** | 1.431 |
| median iterations | **106** | 128 |
| **ant completion** | **80%** | **36%** |
| operations per ant | **1,716** | 3,424 |

**Ant completion 36% -> 80% and operations per ant halved.** The quality
gain is real but much more modest than the completion gain: +6.7 points at
the 1.10 bar, and the median pair is solved exactly either way. The memo's
clearest effect is on the TAIL -- worst stretch 1.431 -> 1.289 -- and on
cost, since half the construction work disappears.

Two honest qualifications:

1. memo OFF here is **not** the Phase 4b baseline. It gets the Phase 4c
   tuning (n_ants=80 instead of 40, beta 0.5 instead of 1.0), which is why
   its completion rate is 36% rather than Phase 4b's 26%. The memo is worth
   roughly +44 points of completion at matched settings, not the
   26% -> 80% that comparing across phases would suggest.
2. n=15, so 1 pair = 6.7 points; the 80.0% vs 73.3% difference is one pair
   and should not be quoted as a precise effect size. The completion and
   ops/ant differences are far outside that noise; the stretch difference
   is not.

### The four failing freeflow pairs

| OD pair | Dijkstra hops | stretch @150 | stretch @300 | iterations | converged |
|---|---|---|---|---|---|
| (2334256440, 10023965571) | 83 | 1.289 | 1.289 | 248 | yes |
| (10060517257, 6536570547) | 54 | 1.232 | 1.232 | 99 | yes |
| (13920076729, 10010980693) | 71 | 1.161 | 1.145 | 300 (budget) | no |
| (429230986, 461789774) | 46 | 1.106 | 1.106 | 130 | yes |

The sample's pairs run 13-83 hops; **all four failures are in the long
tail (46-83 hops)**, and three of the four have CONVERGED by MMAS's own
criterion -- they are settled local optima, not unfinished searches. The
fourth is still creeping at budget 300 (1.161 -> 1.145) and would not reach
1.10 at any budget this project can afford. One pair sits at 1.106,
0.6% over the bar.

This is the same qualitative story as the pre-memo stretch-1.157 plateau
finding above, but it now affects 4 of 30 pairs instead of most of them.

## Speed after Phase 4c, and the new Phase 8 projection

`scripts/bench_aco.py --pairs 5 --iters 10 --memo-ab`, on the shipped
config (`n_ants=80`, memo on, `beta_no_heuristic=0.5`, `rho=0.1`), machine
otherwise idle:

```
seconds/iteration (sustained): mean 0.1059 s (0.0701 - 0.1519 by pair)
operations/ant:                mean 2,721 (1,864 - 3,211)
suggested_budget=150        -> ~15.9 s/solve (upper bound, no early convergence)
Phase 8 ACO sweep (4,200 solves), serial:        18.5 hours
Phase 8 ACO sweep, assuming 10x joblib parallel:  1.9 hours
```

**Phase 8 projection: 47 serial hours (Phase 4b) -> 18.5, and 4.7 hours ->
1.9 at 10x parallelism.** Per iteration the router got *slower* (0.067 ->
0.106 s) because the colony doubled; the win is entirely that the budget
fell 600 -> 150 and each ant does ~40% less work.

The bench's completion figures (33-100%, mean 62%) are lower than the
82% the 30-pair measurement reports, and that is expected rather than a
contradiction: bench times 10 iterations right after a 2-iteration warmup,
when pheromone is still near-uniform, whereas the exit-criterion figure
averages over a full ~106-iteration solve including its converged tail.

### Memo A/B, interleaved in one process

Per the measurement-conditions caveat above, this is a round-robin of the
two variants' iterations inside one process on the same OD pair and seed,
which is the only comparison this machine's 3.4x thermal swing permits:

| | memo ON | memo OFF |
|---|---|---|
| s/iteration, 5-pair mean | **0.1013** | 0.1419 |
| operations/ant, 5-pair mean | **2,721** | 4,848 |
| ant completion, 5-pair mean | **62%** | 9% |

Unlike Phase 4b's finding that the memo's ops saving "does not translate to
wall-clock", at n_ants=80 it clearly does: **1.4x faster per iteration**.
The difference is that Phase 4b measured the memo against a configuration
where few ants completed either way; here the completing ants' extra
path-costing work is more than paid for by the collapse in wasted
backtracking.

## Phase 4c summary

* Shipped: `use_dead_node_memo` (default true), a deliberately lossy
  construction heuristic -- ~66% of its prunes refuse a node that could
  still have reached the target.
* Re-tuned (coarse): `beta_no_heuristic` 1.0 -> 0.5, `n_ants` 40 -> 80,
  `rho` 0.1 unchanged, `suggested_budget` 600 -> **150**.
* Exit criterion: **86.7% freeflow / 96.7% BPR heavy** against a 90% bar in
  both -> **MISSED on freeflow by one pair**. Threshold not lowered,
  headline mode not switched.
* The goal-directed heuristic buys nothing (86.7% freeflow, 90.0% BPR
  heavy) -- the fair comparison is also the favourable one.
* Phase 8 ACO cost: **18.5 serial hours / ~1.9 hours at 10x**.

### Not tried (scope call, recorded as gaps rather than results)

* `k_stall` / `branching_factor_threshold` re-sweep.
* A held-out OD sample: 10 of the 30 exit-criterion pairs were also the
  tuning pairs.
* Multi-seed repetition: every number here is seed 0. The pass/fail margin
  on freeflow is one pair, which is well inside plausible seed variation,
  so the 86.7% should be read as "approximately at the bar and under it",
  not as a precise 3.3-point miss.
* Anything targeted at the long-hop tail (e.g. a candidate-list restriction
  or a local-search/2-opt daemon on the iteration-best path), which is what
  the failure analysis actually points at.

# Phase 4d: out-of-sample, multi-seed re-measurement of the exit criterion

Phase 4c's exit-criterion number (86.7% freeflow / 96.7% BPR heavy) is not
wrong, but it is not trustworthy in EITHER direction, and both reasons were
recorded as gaps in the Phase 4c summary above rather than discovered later:

1. **A third of the sample was in-sample.** The 10 pairs the coarse re-tune
   ran on are the first 10 of the 30 criterion pairs, because everything in
   this sweep record was drawn from `random.Random(2024)`.
2. **n = 1 in the stochastic dimension.** Every Phase 4c figure is algorithm
   seed 0, and the freeflow verdict turned on ONE pair out of 30 -- 3.3
   points, comfortably inside plausible seed-to-seed variation for a
   stochastic search.

Measurement is cheap now (~16 s/solve at budget 150), so Phase 4d re-measures
properly. **Nothing was tuned, and no threshold was moved.** `configs/aco.yaml`
is byte-identical to the file Phase 4c shipped; `aco.py` is untouched. This
section reports what the shipped configuration actually does on data it has
never seen.

Reproduce with:

```
.venv/Scripts/python.exe scripts/measure_aco_criterion.py
.venv/Scripts/python.exe scripts/measure_aco_criterion.py --report-only   # re-aggregate
```

## Protocol

| | Phase 4c | Phase 4d |
|---|---|---|
| OD pair seed | 2024 (same draw as the tuning) | **4242 (fresh, held out)** |
| pairs | 30 | 30 |
| algorithm seeds | 1 (seed 0) | **5 (seeds 0-4)** |
| solves | 60 | **300** |
| budget | 150 (`suggested_budget`) | 150, unchanged |
| mode | `use_goal_heuristic: false`, `use_dead_node_memo: true` | identical |
| conditions | freeflow, BPR heavy (`heavy`, `time_bin=1`) | identical |
| uncertainty reported | none | **two-stage bootstrap 95% CI** |

`stretch = cost_aco / cost_dijkstra` is computed against Dijkstra on the
SAME snapshot, and `stretch >= 1.0` is asserted on every one of the 300
solves (CLAUDE.md invariant). No solve violated it.

### Out-of-sample check (verified, not asserted)

`scripts/measure_aco_criterion.py` prints this before it runs anything, and
`tests/test_aco.py::test_out_of_sample_od_draw_is_disjoint_from_tuning_pairs`
asserts it as a fast test:

| check | result |
|---|---|
| new pairs | 30 (30 distinct) |
| ordered overlap with the 10 TUNING pairs | **0** |
| ordered overlap with all 30 Phase 4c criterion pairs | **0** |
| unordered overlap (same corridor, reversed) with Phase 4c | **0** |
| shared endpoint NODES with Phase 4c (context only) | 2 of 60 slots, graph has 2,856 nodes |

Node-level overlap is reported but NOT treated as contamination: two
samples of 60 node slots drawn from 2,856 nodes are expected to share an
endpoint occasionally, and a shared endpoint is not a shared routing
problem. Pair-level overlap is zero on both the ordered and the unordered
test, so this is a genuinely held-out sample.

### What the confidence interval is over

A two-stage (clustered) percentile bootstrap, 10,000 resamples, seed 0
(`_bootstrap_ci` in the script):

* resample the 30 OD pairs with replacement -- this is the dominant term and
  is what turns the estimate into a claim about the network's OD population
  rather than about these particular 30 pairs;
* then resample the 5 algorithm seeds with replacement *within* each
  resampled pair, since a pair's 5 seeds are not independent draws from the
  population.

A seeds-only bootstrap (n = 5) would be uselessly wide and unstable; a
pairs-only bootstrap would omit the stochastic term entirely. Both sources
are in the interval below.

### Path-length strata

Pairs are split into short / medium / long by **terciles of this sample's
own Dijkstra hop counts**, computed per condition (freeflow and BPR heavy
shortest paths differ, so a pair's hop count differs too). Terciles are a
deliberate choice over fixed hop thresholds: Phase 4c's failing hop counts
(46-83) are already known, so any fixed cut would have been chosen with the
answer in hand. The tercile rule depends only on the hop distribution and
never on which pairs failed.

## Results

300 solves, all completed, **zero failed solves** (every pair routable under
both conditions) and **zero stretch-invariant violations** -- the minimum
observed stretch is exactly 1.000 under both conditions, never below.

### Headline mode (`use_goal_heuristic: false`), freeflow

| algorithm seed | ok/n | frac <=1.10 | median | mean | worst | median iters | ant completion |
|---|---|---|---|---|---|---|---|
| 0 | 24/30 | 80.0% | 1.000 | 1.046 | 1.246 | 98 | 85% |
| 1 | 22/30 | 73.3% | 1.000 | 1.053 | 1.263 | 90 | 85% |
| 2 | 22/30 | 73.3% | 1.000 | 1.076 | 1.565 | 92 | 85% |
| 3 | 21/30 | 70.0% | 1.000 | 1.064 | 1.318 | 92 | 85% |
| 4 | 21/30 | 70.0% | 1.006 | 1.069 | 1.359 | 92 | 85% |
| **mean** | | **73.3%** | | | | | |

**mean 73.3%, bootstrap 95% CI [58.7%, 86.7%], min 70.0% / max 80.0%
(sd 4.1 points).**

### Headline mode (`use_goal_heuristic: false`), BPR heavy

| algorithm seed | ok/n | frac <=1.10 | median | mean | worst | median iters | ant completion |
|---|---|---|---|---|---|---|---|
| 0 | 24/30 | 80.0% | 1.022 | 1.054 | 1.322 | 103 | 86% |
| 1 | 27/30 | **90.0%** | 1.000 | 1.040 | 1.355 | 111 | 87% |
| 2 | 24/30 | 80.0% | 1.023 | 1.048 | 1.356 | 118 | 86% |
| 3 | 24/30 | 80.0% | 1.019 | 1.050 | 1.331 | 131 | 87% |
| 4 | 22/30 | 73.3% | 1.012 | 1.057 | 1.356 | 113 | 86% |
| **mean** | | **80.7%** | | | | | |

**mean 80.7%, bootstrap 95% CI [68.0%, 92.0%], min 73.3% / max 90.0%
(sd 6.0 points).**

## Verdict: the exit criterion is MISSED, and by more than Phase 4c thought

| condition | Phase 4c (in-sample, 1 seed) | Phase 4d (out-of-sample, 5 seeds) | verdict |
|---|---|---|---|
| freeflow | 86.7% | **73.3% [58.7 - 86.7]** | **MISSED** -- the entire CI is below 90% |
| BPR heavy | 96.7% | **80.7% [68.0 - 92.0]** | **INDISTINGUISHABLE** -- the CI straddles 90% |

The criterion demands both conditions, and freeflow misses with its whole
confidence interval below the bar, so **Phase 4's exit criterion is not
met.** No hyperparameter, threshold or algorithm was changed to produce
this number, and none was changed after seeing it.

Two things about this table matter more than the verdict itself.

**1. Phase 4c's figures were at the optimistic edge, in both conditions.**
86.7% is *exactly* the upper bound of the Phase 4d freeflow CI, and 96.7%
is outside the BPR-heavy CI entirely. That is the expected signature of a
partly in-sample estimate reported from a single favourable seed: it is not
"wrong", it is the best case of a distribution whose centre is 13-16 points
lower. This is the clearest justification the project has for insisting on
held-out samples in Phase 8.

**2. One seed genuinely could have decided the BPR-heavy verdict.** Seed 1
gives 90.0% -- an exact pass -- and seed 4 gives 73.3%. A single-seed
measurement of the same configuration, on the same 30 pairs, could honestly
have reported either "passes" or "misses badly" depending only on which
integer was handed to `prepare()`. Freeflow swings 70.0-80.0% over the same
five seeds. Any future single-seed quality claim in this project should be
read with a ~10-17 point band around it.

The confidence intervals are wide (±14 points freeflow, ±12 BPR heavy) and
that is honest rather than fixable by arithmetic: 30 OD pairs is a small
sample for a proportion near 0.8. Narrowing it needs more *pairs*, not more
seeds -- the pair-resampling term dominates. What makes the freeflow verdict
firm despite the width is that the interval's upper end (86.7%) is still
below the bar.

Phase 4c's qualitative claim that **freeflow is the harder condition
survives** (73.3% vs 80.7%), and for the reason given there: under heavy
congestion the cost surface flattens, so many more routes sit within 10% of
optimal. The effect is smaller than the 86.7-vs-96.7 gap suggested.

## Where the failure lives: path length, confirmed and quantified

Phase 4c asserted, from four pairs, that the failures are all long-hop. The
larger independent sample **confirms it, strongly, under both conditions.**
Fractions are over the 50 solves in each tercile (10 pairs x 5 seeds):

| condition | stratum | hops | pairs | frac <=1.10 | median stretch | worst |
|---|---|---|---|---|---|---|
| freeflow | short | 6-28 | 10 | **88.0%** | 1.000 | 1.446 |
| freeflow | medium | 35-47 | 10 | **84.0%** | 1.000 | 1.224 |
| freeflow | **long** | **49-88** | 10 | **48.0%** | **1.126** | 1.565 |
| BPR heavy | short | 6-36 | 10 | **94.0%** | 1.000 | 1.123 |
| BPR heavy | medium | 37-50 | 10 | **90.0%** | 1.000 | 1.356 |
| BPR heavy | **long** | **53-80** | 10 | **58.0%** | **1.079** | 1.283 |

The long tercile is where essentially the entire shortfall lives, and it is
the only stratum whose *median* pair misses the bar under freeflow. Short
and medium pairs are solved optimally at the median under both conditions.

A counterfactual that sizes the effect: if the long tercile merely performed
as well as the short one, the overall figure would be **86.7% freeflow**
(110/150 -> 130/150) and **92.7% BPR heavy** (121/150 -> 139/150). So the
long tail alone accounts for the whole BPR-heavy shortfall -- fixing it
would convert that condition to a pass -- but under freeflow it does *not*:
even with a perfect long tercile, freeflow would still land at 86.7% and
still miss. **Freeflow has a second, smaller problem spread across short and
medium pairs, and no amount of long-hop-specific work would close it.**
That is a more useful statement for the paper than "long pairs are hard".

### Pair-level structure: how much is the pair, how much is the seed?

| | freeflow | BPR heavy |
|---|---|---|
| pairs meeting the bar on **all 5** seeds | 18/30 | 22/30 |
| pairs meeting it on **no** seed | 4/30 | 1/30 |
| pairs that are **seed-dependent** | **8/30** | **7/30** |

Hop counts of the never-solved pairs: 35, 49, 81, 82 (freeflow) and 49 (BPR
heavy). Hop counts of the seed-dependent pairs: 18, 25, 26, 46, 59, 67, 67,
88 (freeflow) and 25, 53, 56, 73, 75, 76, 78 (BPR heavy).

Roughly a quarter of pairs are coin-flips. A pair is not simply "solvable"
or "not"; for a meaningful minority the colony converges to a good corridor
or a mediocre one depending on the seed, which is the mechanism behind the
per-seed spread above.

That variance is bimodal rather than gentle, and it is not confined to long
pairs. Freeflow pair 9 is a **26-hop** pair -- comfortably in the short
tercile -- whose five stretches are `{1.000, 1.000, 1.000, 1.233, 1.446}`:
the exact optimum on three seeds and a catastrophic corridor lock-in on the
other two. (The sample's single worst solve, 1.565, is a long pair, 88 hops,
which likewise scores ~1.035 on three of its five seeds.) So the tail is not
"ACO gets long pairs slightly wrong"; it is "on some pairs the colony
occasionally locks onto a badly wrong corridor and never leaves it", and
long pairs simply offer more opportunities for that to happen.

Other diagnostics, both conditions: ant completion is stable at 85-87%
across every seed (the dead-node memo's contribution is not seed-sensitive),
median iterations 90-98 (freeflow) and 103-131 (BPR heavy), and MMAS's own
convergence criterion fires within budget on 79% of freeflow and 69% of
BPR-heavy solves -- so most failures are settled local optima rather than
unfinished searches, exactly as Phase 4c found.

## What Phase 4d deliberately did NOT do

No tuning. `configs/aco.yaml` and `algorithms/aco.py` are untouched by this
phase; the only source changes are the snapshot-cache concurrency fix
(below), the measurement script, and the tests. The temptation with a 73.3%
against a 90% bar is to go hunting for the 17 points, and the failure
analysis even points somewhere specific (a candidate-list restriction or a
2-opt daemon on the iteration-best path, both aimed at the long tercile).
That work would be a new phase with its own held-out measurement, not a
patch applied to this one -- tuning against the sample you are reporting is
precisely the error Phase 4d exists to correct.

`suggested_budget` was not raised either. Phase 4c established the budget
curve is flat from 150, and 79%/69% of solves converge by MMAS's criterion
inside the budget, so the failures are not a budget artefact.

## Wall-clock (NOT a performance figure)

300 solves in 11.3 minutes on 10 worker processes. That number is for
planning sweeps only: it is a heavily parallel run on a machine whose
throughput swings up to 3.4x with thermal state, and the measurement-
conditions caveat above forbids reading it as a speed result.
`scripts/bench_aco.py` remains the only speed measurement.

## Snapshot-cache concurrency fix (shipped with Phase 4d)

Phase 4c's 30-pair run crashed halfway through with `PermissionError` out of
the BPR snapshot cache. The cause was in the cache write, not in joblib:

```python
tmp = path.with_suffix(".json.tmp")   # SAME temp path for every writer
...
tmp.replace(path)
```

`path.with_suffix(".json.tmp")` is a pure function of the cache key, so all
12 workers computing the same cold snapshot wrote to one shared temp file
and then raced to rename it. Two distinct failures follow, and on Windows
both are reachable where POSIX tolerates them:

1. two writers interleave inside one temp file, so what lands at the final
   path can be spliced or truncated;
2. `os.replace` fails with `ERROR_SHARING_VIOLATION` while *any* process
   holds a handle on the source or the destination -- including an innocent
   concurrent *reader* of the cache.

Reproduced deterministically on this machine: 6 writer + 4 reader processes
released onto a cold cache path produced **5 `PermissionError [WinError 32]`
failures out of 6 writers**.

The fix (`src/bioroute/congestion/base.py`, used by both `BPRProvider` and
`SUMOProvider`, which had identical code):

* `write_json_atomic` -- a temp file unique per writer
  (`pid.thread.uuid8`), `fsync` before the replace, `os.replace` with
  bounded exponential-backoff retry on sharing violations, and a
  "did another writer already land a valid file?" check that treats losing
  the race as success (snapshots are deterministic given their cache key,
  so every concurrent writer produces the same bytes);
* `read_json_cache` -- validates on read. Absent, unparseable, truncated or
  structurally incomplete all collapse to `None` ("recompute"), never a
  partially-believed snapshot; the sharing-violation window on the *read*
  side gets its own short retry so a worker is not sent off to recompute a
  10-increment BPR assignment for nothing;
* `prewarm_snapshots` -- the belt-and-braces half. Materialises every needed
  snapshot **serially, before any parallelism**, so workers only ever read.
  `scripts/measure_aco_criterion.py` calls it, and **Phase 8's runner must
  do the same**.

Regression-tested by `tests/test_congestion.py`, with real concurrency
rather than a happy-path round trip (which passes against the old code):
6 spawned writer processes + 4 reader processes on a cold cache, 8 threads
driving `BPRProvider.snapshot()` on a cold cache, five corrupt-file shapes,
and a pre-warm test that asserts every cache file is readable afterwards.

## Effect on the Phase 4c "not tried" list

* held-out OD sample -- **done** (this section);
* multi-seed repetition -- **done** (5 seeds; it changed the verdict's size,
  and nearly its sign on BPR heavy);
* `k_stall` / `branching_factor_threshold` re-sweep -- still not tried;
* long-hop-targeted work (candidate lists, 2-opt daemon) -- still not tried,
  and now quantified as worth at most +13 points freeflow / +12 BPR heavy.
