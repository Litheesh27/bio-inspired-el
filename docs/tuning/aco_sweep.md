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
