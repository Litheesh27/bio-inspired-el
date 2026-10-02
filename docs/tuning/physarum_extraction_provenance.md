# Phase 8b — where Physarum's reported path actually comes from

Version-controlled origin for the Phase 9 reporting caveat on Physarum's
stretch figures. The headline number: **on a 30-OD-pair freeflow sample at the
tuned budget (300), only 10 of 30 (33%) of Physarum's reported paths were
produced by the model's own max-flux walk. 20 of 30 (67%) came from a
fallback, and 4 of 30 (13%) came from `full_graph_fallback`, which is plain
Dijkstra on the full graph.**

## The problem this measures

`PhysarumRouter.best_cost()` is a minimum over **every iteration and every
extraction method**. The three methods are not equivalent:

| method | what it is |
|---|---|
| `max_flux_walk` | the model's own answer: greedy walk on the pressure/conductivity field, directed-legal at every step |
| `theta_subgraph_fallback` | Dijkstra restricted to tubes with `D > theta` — the model chose the *corridor*, Dijkstra chose the path inside it |
| `full_graph_fallback` | Dijkstra on the whole graph — **the model contributed nothing to this path** |

So a reported Physarum stretch of exactly 1.0 can mean "Dijkstra's answer,
obtained via the fallback". Until Phase 8b nothing in the results
distinguished the cases, because the only recorded column
(`extraction_method`) held the **last iteration's** method, which is a
different quantity from the method that produced the reported path.

Phase 8a first spotted this on 6 undisrupted pairs (3 of 6 fallback-won, 1 of
6 last-vs-winning disagreement). This note is the proper measurement.

## Instrumentation

`PhysarumRouter.best_path_extraction_method()` records the extraction method
in force at the moment `best_cost` **last improved**, reset alongside
`best_path`/`best_cost` (so a disruption cannot leave the new run's answer
attributed to the old run's extraction). It is surfaced as the nullable
Parquet column `best_path_extraction_method`, populated only by Physarum;
null for ACO and the classical routers means "this algorithm has no
path-extraction step".

`extraction_method` (last iteration's method) is **kept unchanged**. The two
answer different questions — how the run ended vs. where the reported answer
came from — and neither is derivable from the other or from
`extraction_counts()`.

This is instrumentation only. Verified by rerunning the identical 30-pair
measurement against the pre-change code at `84c6e1f`: **0 differences across
all 30 pairs on every numerical field** (`physarum_cost`, `physarum_path`,
`stretch`, `iterations`, `extraction_counts`, `last_extraction_method`,
`dijkstra_cost`). Phase 5's exit criterion is unmoved at 26/30 (87%).

## Measurement

- Graph: cached Central Bengaluru graph (`configs/area_central_blr.yaml`).
- Congestion: `FreeFlowProvider` (freeflow — the condition Phase 5's exit
  criterion is stated on).
- Budget: 300 (`default_iteration_budget` in `configs/physarum.yaml`),
  `variant: tero`, seed 0.
- OD pairs: the same 30-pair draw as
  `tests/test_physarum.py::od_pairs_30` (`random.Random(2024)`), so these
  numbers sit directly alongside Phase 5's 26/30. **This is the tuning draw,
  not a held-out sample** — see the caveat at the end.
- Denominator: Dijkstra on the same graph and the same snapshot.

### Provenance of the reported path, all 30 pairs

| `best_path_extraction_method` | pairs | share |
|---|---|---|
| `theta_subgraph_fallback` | 16 | 53.3% |
| `max_flux_walk` | 10 | 33.3% |
| `full_graph_fallback` (= plain Dijkstra) | 4 | 13.3% |

### Split by whether the pair matched Dijkstra exactly

Among the **26 exact (stretch 1.0) matches** — i.e. Phase 5's exit-criterion
successes:

| method | pairs | share of the 26 |
|---|---|---|
| `theta_subgraph_fallback` | 16 | 61.5% |
| `full_graph_fallback` | 4 | 15.4% |
| `max_flux_walk` | 6 | 23.1% |

Among the **4 pairs with stretch > 1.0** (1.0048, 1.0935, 1.1254, 1.1734):
all 4 came from `max_flux_walk`.

**This is the result Phase 9 has to carry.** The two statements are both true
and they pull in opposite directions:

1. Only **6 of 30 pairs (20%)** are a genuine, unassisted model win: the
   max-flux walk reaching Dijkstra's optimum by itself.
2. **4 of the 26 exact matches (15%) are literally Dijkstra's own path**, and
   another 16 (62%) are Dijkstra's path within a model-chosen corridor.

Every pair where the model answered on its own terms and was *wrong* shows up
in the stretch distribution; every pair where it fell back *in whole or part*
shows up as a 1.0. So the undisrupted "87% exact match" figure is not a
measure of the max-flux walk's accuracy, and must not be reported as one.

### `extraction_method` vs `best_path_extraction_method`

The two columns **disagreed on 17 of 30 pairs (57%)**, far more often than the
1-in-6 Phase 8a saw. The walk succeeded on at least one iteration on 23 of 30
pairs but produced the reported path on only 10, and the gap is not a
matter of the walk barely ever working — e.g.:

```
6461987668 ->10010980677  walk ok on 115/118 iters, best from full_graph_fallback
10019824661->2228056195   walk ok on 135/165 iters, best from theta_subgraph_fallback
2334256422 ->4044110220   walk ok on 111/147 iters, best from theta_subgraph_fallback
```

Mechanism: the fallbacks are Dijkstra-exact *by construction*, so the first
iteration on which a fallback fires usually sets a `best_cost` the walk can
then only tie, never beat (`cost < best_cost` is strict). In particular
`theta_subgraph_fallback` fires in the first ~18 iterations on most pairs,
while `D` is still uniformly above `theta` — at which point the theta
subgraph is essentially the whole graph and that fallback is close to plain
Dijkstra too. So the reported `best_cost` is typically locked in during early
relaxation, long before the model has actually differentiated anything.

## What Phase 9 should do with this

1. Report Physarum's stretch distribution **stratified by
   `best_path_extraction_method`**, never pooled. The pooled median is
   dominated by fallback rows and is close to a restatement of Dijkstra.
2. State the fallback share explicitly wherever the 87% exact-match figure
   appears.
3. Treat `max_flux_walk`-only rows as the model's own performance. On this
   sample that is 10 pairs: 6 exact, 4 at stretch 1.005–1.173.
4. The same breakdown must be recomputed on the Phase 8 sweep's own 30 pairs
   (draw seed 8080) and across scenarios and traffic levels; the column is in
   the schema precisely so that costs nothing extra. Expect the disrupted
   cells to look different — Phase 8a's S6 census showed the fallbacks firing
   for structural reasons there too.

## Caveats

- **This is the tuning draw (seed 2024), measured in-sample and at a single
  seed.** CLAUDE.md's Phase 4d entry is explicit that in-sample single-seed
  numbers are not reportable in this project. Physarum is deterministic given
  its fixed `D_init` (Phase 8 measured 0/5 pairs varying across
  initialisation seeds), so the seed objection is weaker here than it was for
  ACO — but the *pair draw* objection stands in full. These figures exist to
  size the caveat and to justify the column; the number Phase 9 publishes
  must come from the sweep's own held-out pairs.
- Freeflow only. Phase 4d found freeflow is the harder condition for ACO;
  nothing here says which way Physarum's fallback share moves under
  congestion.
- Fallback use is not a bug and not a failure. A physical tube network can
  legitimately fail to yield a directed-legal walk (the undirected
  relaxation's preferred flow direction can be a one-way street the wrong
  way). The fallback is honest Phase 5 design; the dishonesty would be
  reporting its output as the model's.

## Reproducing

The measurement script is not checked in (it is a one-off harness). It is a
loop over the `random.Random(2024)` 30-pair draw calling
`run_to_convergence(PhysarumRouter(...), budget=300)` on a
`FreeFlowProvider` snapshot and tallying
`best_path_extraction_method()` / `last_extraction_method()` /
`extraction_counts()` per pair. The committed equivalents are:

- `tests/test_physarum.py::test_physarum_exit_criterion_freeflow` — the 26/30
  figure on the same pairs.
- `tests/test_physarum.py` Phase 8b tests — the semantics of the new field,
  including a real (unmocked) graph on which the winning method and the last
  iteration's method differ.

---

# Phase 8c — how accurate is the max-flux walk *on its own*?

**Headline: the walk's own exact-Dijkstra-match rate is 36.7% (draw 2024) /
46.7% (draw 8080) under freeflow and 3.3% / 16.7% under BPR heavy, against a
pooled rate of 83-87% in every one of those four conditions. Part of Phase 8b's 33% figure WAS a
bookkeeping artifact — worth 5 pairs under freeflow — but only part: after
correcting for it the walk still misses by ~40-50 points under freeflow and
~70-80 under congestion. Verdict (b), with the artifact quantified.**

## Why Phase 8b's 33% could not answer this

Phase 8b measured *provenance* — which method produced the reported path —
and that is genuinely what it says it is. But it is not the walk's accuracy,
because the contest it reports is rigged twice over:

1. `theta: 0.05` while `D_init: 1.0`, so for roughly the first 18 iterations
   every tube is above threshold and the "theta subgraph" is essentially the
   whole graph. `theta_subgraph_fallback` is therefore close to plain
   Dijkstra at exactly the moment it first fires, and it sets `best_cost`
   before the flux field has differentiated anything.
2. `best_cost` improves on a **strict** `cost < best_cost`. Once the optimum
   is in, the walk can only ever TIE — and a tie is recorded as a loss.
   Phase 8b logged the walk succeeding on 115 of 118 iterations and still
   scoring zero credit.

So the question "what would Physarum report if the max-flux walk were its
only extraction method?" was unmeasured. Phase 8c measures it.

## Instrumentation and protocol

`PhysarumRouter` gained a second, parallel tracker — `walk_best_cost()`,
`walk_best_path()`, `walk_best_iteration()`,
`walk_first_success_iteration()`, `walk_success_count()` — updated only when
`_extract_path` returned `max_flux_walk`, after the pooled tracker, and read
by nothing the model does. `best_cost`, the reported path, `theta` and
`best_path_extraction_method` are all **unchanged**; the model was not
touched.

*Behaviour-preservation was verified, not assumed.* The shipped `physarum.py`
at `e4527df` was loaded as a second module alongside the new one and both
were run **interleaved in one process** on identical inputs: the A-B-C-D-E
toy graph with a spur (undisrupted, then warm and then cold recovery) and the
first 12 pairs of the 2024 draw on the real graph at budget 300 — 16 cases.
Compared field by field with exact float equality via `repr`: the full
per-iteration best-cost trace, `best_path`, `best_cost`, `iterations`,
`converged`, `time_to_first_valid_path_iters`, `extraction_counts()`,
`last_extraction_method()`, `best_path_extraction_method()`, `n_solves()`,
`active_component()`, the `state()` key set, and element-by-element the final
conductivity vector `D` and pressure vector `p`. **0 differences across all
16 cases.**

Measurement protocol (`scripts/measure_physarum_walk.py`, checkpointed to
`data/results/phase8c_physarum_walk.jsonl` after every pair):

- Graph: the Phase 1 cached graph — the same one Phase 5's 26/30 and Phase
  8b's provenance figure were measured on.
- Draws, both: **2024** (the Phase 5 / 8b pairs, reproduced bit-exactly from
  `tests/test_physarum.py::od_pairs_30`, in-sample, kept only for
  comparability) and **8080** (the Phase 8 sweep's own stratified draw;
  `sample_od_pairs` enforces disjointness from 2024 and 4242 — the overlap
  check printed 0 ordered and 0 unordered).
- Conditions: freeflow and BPR heavy (`time_bin=1`), snapshots pre-warmed.
- Budget 300, seed 0. 120 pairs measured, **0 failed runs**.

## Results

Walk stretch statistics are over the pairs where the walk produced a path;
the exact-match and `<= 1.10` *rates* use all 30 pairs as the denominator, so
a walk that never reached the target counts as a miss (it is one).

| draw | condition | walk gave ANY path | **WALK-ONLY exact** | pooled exact | walk median | walk worst | walk `<=1.10` | pooled worst |
|---|---|---|---|---|---|---|---|---|
| 2024 | freeflow  | 23/30 (76.7%) | **11/30 (36.7%)** | 26/30 (86.7%) | 1.0002 | 2.1872 | 17/30 (56.7%) | 1.1734 |
| 2024 | BPR heavy | 16/30 (53.3%) | **1/30 (3.3%)**   | 25/30 (83.3%) | 1.2773 | 3.1213 | 3/30 (10.0%)  | 3.1213 |
| 8080 | freeflow  | 25/30 (83.3%) | **14/30 (46.7%)** | 26/30 (86.7%) | 1.0000 | 1.5369 | 20/30 (66.7%) | 1.5369 |
| 8080 | BPR heavy | 21/30 (70.0%) | **5/30 (16.7%)**  | 26/30 (86.7%) | 1.0411 | 3.0845 | 13/30 (43.3%) | 1.9818 |

Conditional on the walk producing a path at all, its exact-match rate is
47.8% / 6.2% / 56.0% / 23.8% — so "the walk dead-ends" and "the walk finishes
on the wrong path" are both first-order failure modes, not one dominating the
other:

| draw / condition | exact | wrong path | no path at all |
|---|---|---|---|
| 2024 freeflow  | 11 | 12 | 7 |
| 2024 BPR heavy | 1  | 15 | 14 |
| 8080 freeflow  | 14 | 11 | 5 |
| 8080 BPR heavy | 5  | 16 | 9 |

Both failure modes track path length. Median Dijkstra hop counts, 8080
freeflow: 30 (exact) / 49 (wrong path) / 44 (no path); 8080 BPR heavy: 15 /
37 / 55. Walk exact-match by distance stratum (n=10 each, indicative only):

| 8080 | short | medium | long |
|---|---|---|---|
| walk, freeflow  | 7/10 | 2/10 | 5/10 |
| walk, BPR heavy | 5/10 | 0/10 | 0/10 |
| pooled, freeflow | 9/10 | 9/10 | 8/10 |
| pooled, BPR heavy | 7/10 | 10/10 | 9/10 |

The pooled rate is flat across strata (it is Dijkstra-backed); the walk's is
not. Under BPR heavy the walk gets *nothing* right beyond the short stratum.
Freeflow's medium band (2/10) being worse than its long band (5/10) breaks
monotonicity, so hop count is not the whole story at n=10 per cell.

### The tie rate — the crux

| draw / condition | walk ties the pooled best | of the fallback-attributed pairs, walk matched the pooled cost |
|---|---|---|
| 2024 freeflow  | 15/30 (50.0%) | **5 of 20 (25.0%)** |
| 2024 BPR heavy | 6/30 (20.0%)  | **0 of 24 (0.0%)**  |
| 8080 freeflow  | 18/30 (60.0%) | **5 of 17 (29.4%)** |
| 8080 BPR heavy | 9/30 (30.0%)  | **0 of 21 (0.0%)**  |

The second column is the artifact, isolated: pairs where the walk reached the
pooled optimum and was denied the credit purely by the strict `<`. Under
freeflow that is 5 pairs per draw — which is exactly the difference between
Phase 8b's "6 of the 26 exact matches came from the walk" and the walk's true
11 exact matches on the same sample. **Under congestion it is zero on both
draws:** every fallback-attributed pair there is one where the walk really
did not find the pooled answer.

### When the walk reaches its best

Iteration (1-based) on which `walk_best_cost` last improved, over the pairs
where the walk produced a path:

| draw / condition | min | p25 | median | p75 | max | in by iter 18 | run length (median iters) |
|---|---|---|---|---|---|---|---|
| 2024 freeflow  | 1 | 19 | 34 | 40 | 60 | 5/23 (21.7%) | 119.5 |
| 2024 BPR heavy | 1 | 19 | 26 | 39 | 50 | 4/16 (25.0%) | 101 |
| 8080 freeflow  | 1 | 18 | 33 | 38 | 66 | 7/25 (28.0%) | 115 |
| 8080 BPR heavy | 1 | 1  | 6  | 33 | 40 | 13/21 (61.9%) | 99.5 |

The walk's best almost always arrives **after** the theta window the
fallbacks win in (72-78% of freeflow cases improve past iteration 18), and
always long before the run ends. The 10 fallback-denied ties specifically
landed at iterations 25-65 (2024: 36, 38, 40, 40, 45; 8080: 25, 33, 40, 40,
65).

**So a delayed-fallback policy would work — for the credit, and for nothing
else.** Deferring the fallbacks past ~iteration 70, or preferring the walk's
own result on a tie, would reassign those 10 pairs from the fallback to the
walk and move Phase 8b's provenance share from 33% to roughly 50% under
freeflow. It would not change a single reported cost (they are ties, by
definition), it would not help the wrong-path or no-path pairs at all, and
under BPR heavy it would recover exactly nothing. It is a reporting fix, not
a quality fix — and since it changes `best_cost` semantics it is deliberately
NOT applied here.

## Verdict

**(b), with the (a)-component quantified.**

- Phase 8b's 33% did understate the walk, and by a measurable amount: on the
  same 30 pairs the walk's exact-match rate is 11/30 (36.7%), not the 6/26
  that provenance credited it with. Five of those pairs are ties the strict
  `<` hid. That correction is real and the figure should be used.
- But it is nowhere near the whole gap. Corrected, the walk is still at
  36.7% / 46.7% (freeflow) and 3.3% / 16.7% (BPR heavy) against a pooled
  83-87%. Under congestion the artifact contributes **zero** pairs — the
  entire 70-80 point deficit there is the walk genuinely failing.
- (c) in the strict sense is therefore false in one direction: this is not a
  "somewhere in between" where the two explanations split the difference.
  Under freeflow the artifact explains roughly a third of the exact-match
  deficit (5 pairs of the 12-15 the walk misses relative to pooled); under
  congestion it explains none of it.

**Physarum's apparent competence on this network does substantially depend on
its fallback.** `theta_subgraph_fallback` is not a cosmetic tidy-up: it is
carrying the exact-match rate from ~37-47% to ~87% under freeflow and from
~3-17% to ~83-87% under congestion.

### Why this is a result and not a defect

The mechanism is specific and reportable. The Tero model is a scalar
conductivity field on an *undirected* medium; a greedy max-flux walk on it is
only guaranteed to arrive when every locally-preferred flow direction is also
a legal move. On a planar lab maze — the setting virtually every Physarum
shortest-path result is reported in — that holds trivially. Central Bengaluru
is not that: mean out-degree ~2.5, one-way streets, cul-de-sacs, and an
irregular organic topology, so the pressure field routinely prefers a
direction that has no directed edge, the walk dead-ends (17-47% of pairs) or
takes a wrong turn it cannot undo (`visited` is never cleared during the
walk). Congestion makes it worse, which is the opposite of ACO's behaviour
(Phase 4d: congestion flattens ACO's cost surface and *helps* it) — here
congestion re-weights the undirected minimum-cost projection away from the
directed path that is actually available.

That is the honest finding: **lab-maze Physarum extraction does not transfer
to real city topology, and the measured size of the shortfall is this
project's contribution on the point.** It is not something to tune away, and
the fallback is not cheating — a physical tube network genuinely cannot
always yield a directed-legal walk. What would be dishonest is reporting the
pooled 87% as the model's own accuracy.

## What Phase 9 must do with this

1. The 87% exact-match figure may be reported only as **"Physarum with its
   Dijkstra-backed corridor fallback"**, with the walk-only rate beside it.
   The 8080 numbers (46.7% freeflow / 16.7% BPR heavy) are the ones that
   transfer, since 2024 is the tuning draw.
2. Keep the **tie correction** visible: the provenance share understates the
   walk by 5 pairs per freeflow draw, so a figure stratified on
   `best_path_extraction_method` alone still under-credits the walk. Say so
   where that figure appears.
3. **Not measured here, and it matters:** these figures are freeflow + BPR
   heavy on the Phase 1 `config_speeds` graph (chosen for comparability with
   Phase 5 and 8b). The sweep runs SUMO light/heavy on `sumo_speeds`, and the
   Phase 6 schema records `best_path_extraction_method` but **not**
   `walk_best_cost` — surfacing the walk-only figure across the sweep's 420
   cells needs one more nullable column, which is a Phase 9 schema decision
   and was deliberately out of scope for Phase 8c.
4. Disrupted cells are also unmeasured here. Phase 8a already showed the
   fallbacks firing for structural reasons under S5/S6, so the walk-only rate
   there is likely worse, not better.

## Reproducing

    SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe scripts/measure_physarum_walk.py
    .venv/Scripts/python.exe scripts/measure_physarum_walk.py --report-only

Committed equivalents: `tests/test_physarum.py` Phase 8C block — the walk
tracker's semantics, including the scripted "walk ties the pooled optimum and
is denied credit by the strict `<`" case, the unmocked dead-end case on the
one-way trap graph, the `walk_best >= pooled_best` structural invariant, and
the reset-on-disruption behaviour.
