# Why does warm start make Physarum's max-flux walk *less* likely to complete?

**Status: PRE-REGISTRATION (predictions only). No data had been collected when
this section was written — results are appended below it, after the fact, and
nothing above the `## METHOD` heading has been edited since.**

Measured by `scripts/measure_physarum_warm_mechanism.py`.
Author: Phase 8e follow-up (mechanism test).

---

## 0. THE FACT BEING EXPLAINED (already measured, not in question)

Phase 8e RESULT 4, from the sweep's own Parquet
(`data/results/final_14a535c02b83.parquet`, config hash `14a535c02b83`):

* Physarum's max-flux walk completes a path during recovery on **36.3% of
  warm** recoveries vs **59.5% of cold** ones.
* On the 126 cells where BOTH complete, cold's walk is marginally BETTER
  (`post_disruption_walk_stretch` median 1.0127 cold vs 1.0587 warm, cold
  better on 44 and warm on 12, Wilcoxon p = 2.7e-04).
* Pairing is sound: pre-disruption `walk_best_stretch` is byte-identical warm
  vs cold (max |diff| 0.0 over 250 cells), as it must be — before the closure
  the two modes are the same run.

Re-confirmed from the Parquet before writing this file (this is the *fact*, not
a prediction), walk-completion rate during recovery per scenario,
cold / warm:

| scenario | cold | warm | gap |
|---|---|---|---|
| S1 | 0.633 | 0.417 | 0.217 |
| S2 | 0.550 | 0.350 | 0.200 |
| S3 | 0.600 | 0.317 | 0.283 |
| S4 | 0.517 | 0.350 | 0.167 |
| S5 | 0.667 | 0.350 | 0.317 |
| S6 | 0.583 | 0.383 | 0.200 |

(n = 60 cells per scenario per mode.)

## 1. THE EXPLANATIONS ON TRIAL

### M — "misdirected retained field" (the CLAUDE.md mechanism, authored by the user)

> the retained conductivity field is tuned to the OLD optimum, so after a
> closure it keeps driving flux down now-severed corridors and the walk
> dead-ends; a cold field starts uniform and explores freely.

This is a **spatial, closure-specific** claim. The damage is caused by *where*
the closure is relative to the retained high-conductivity corridor.

### R — "global decay / less usable conductivity" (the stated rival)

By the time the closure lands, the warm field has converged: under the Tero
rule `D <- D + dt*(f(|Q|) - D)` with `f(Q) = |Q|^mu/(1+|Q|^mu)`, every
off-corridor tube has decayed toward ~0. So the warm graph simply has *less*
usable conductivity everywhere and the walk dead-ends for reasons that have
**nothing to do with where the closure is**. Cold starts at a uniform
`D_init = 1.0` and so has the maximum possible usable conductivity.

### C — "field concentration / flat off-corridor potential" (third explanation, added here)

A cousin of R but mechanistically distinct, and distinguishable from both M
and R. In cold mode `D` is uniform, so the pressure field is a smooth
potential over the whole graph and the max-flux walk is effectively gradient
descent on it — it rarely dead-ends anywhere. In warm mode `D` is
*concentrated* on a thin corridor; off the corridor `w = D/L` is ~0, the
pressure field is nearly flat, and the walk's ranking of off-corridor
candidates is decided by vanishingly small flux differences. The walk
therefore wanders and dead-ends **anywhere off the retained corridor** — which
is spatially related to the *corridor*, not to the *closure*. Total `D` is
irrelevant under C (scaling `D` uniformly does not change the walk's argmax);
its *concentration* is what matters.

C matters because it predicts the same NULL spatial result as R on
measurement 1 while having a completely different fix, and because the three
explanations imply three different things about whether the effect is a bug or
a property of the model.

## 2. PRE-REGISTERED PREDICTIONS

All comparisons are **paired on identical `(od, scenario, traffic)` cells**
(Physarum has one seed, measured — see `configs/experiments/disruption.yaml`).
Medians + IQR. `n` reported for every number.

### Measurement 1 — spatial locality of the dead-end

For every recovery iteration on which the walk FAILS, record the walk's
terminal node and its **hop distance** (unweighted BFS on the undirected
projection of the surviving graph) to the nearest endpoint of any removed
edge. Summarise each cell by the dead-end of its FIRST post-disruption
iteration and by the cell's median over all failing iterations.

Nulls, all computed per cell so they share the cell's own geometry:
  * `d_random`: median hops from 200 uniformly drawn graph nodes to the nearest
    removed endpoint. The structural baseline.
  * `d_walkpath`: median hops from the failing walk's OWN non-terminal path
    nodes to the nearest removed endpoint. Controls for the walk's spatial
    envelope — a walk that lives near the closure has a terminal near the
    closure for free.
  * `d_source`, `d_target` for orientation.

| | M predicts | R predicts | C predicts |
|---|---|---|---|
| warm `d_closure` (terminal) | **small**, median <= 3 hops | ~ `d_random` | ~ `d_random` |
| warm `d_closure` vs cold `d_closure` | warm significantly **smaller** | no difference | no difference |
| warm terminal vs its own `d_walkpath` | terminal **closer** than the walk's typical node | no difference | no difference |

**M is refuted on this measurement if warm's terminal-to-closure distance is
statistically indistinguishable from `d_random` and from cold's.**

### Measurement 2 — where the retained conductivity mass sits

At the moment of disruption (immediately after `apply_disruption`, before the
first recovery `step()`), read `state()["D_by_pair"]` and compute:

  * `sev_mass_pre`: share of the PRE-disruption total `D` that sat on the
    pairs the closure severs. (Measured pre-disruption, because
    `apply_disruption` forces severed pairs to `D = 0` by design, so measuring
    it after would read 0 by construction.)
  * `near_share_h`: share of surviving total `D` within `h ∈ {1,2,3}` hops of a
    removed endpoint, warm vs the cold structural baseline (cold is uniform, so
    its share is exactly `n_tubes_within_h / n_tubes`).
  * `near_excess_h = near_share_h(warm) / near_share_h(cold)`.

| | M predicts | R predicts | C predicts |
|---|---|---|---|
| `near_excess` | **> 1** (mass piled against the closure) | ~1 or unsystematic | ~1 or unsystematic (mass piles on the *corridor*, which need not be near the closure beyond the on-path closure's own position) |
| does `near_excess` / `sev_mass_pre` predict warm walk failure across cells? | **yes**, failing cells higher | no | no |
| does total/median `D` predict failure? | no | **yes** | no |
| does `D` concentration (Gini, top-k share) predict failure? | not specifically | no | **yes** |

Note on a confound I am declaring in advance: for S1-S4 and S6 the closure is
ON the pre-disruption optimal path by construction, and the retained field is
concentrated on that path, so `near_excess > 1` is close to tautological.
**`near_excess > 1` on its own is therefore NOT evidence for M.** The
discriminating quantity is whether it *predicts failure across cells*, and
whether measurement 1 finds the dead-ends there.

### Measurement 3 — is the warm field simply smaller? (the rival, head-on)

Distribution of `D` warm vs cold at the moment of disruption: total, median,
IQR, share of tubes below `theta = 0.05`, Gini. Then: do `total_D` / `median_D`
predict warm walk failure *across warm cells* (where they vary), independently
of any spatial measure?

* R predicts yes and predicts warm `total_D` << cold `total_D` (cold's is
  exactly `n_tubes * 1.0`).
* M and C predict warm `total_D` << cold's too — **so the level difference
  alone does not discriminate anything** and is pre-declared as uninformative.
  What discriminates is whether it *predicts failure*.

### Measurement 4 — does the effect decay over recovery?

Per-iteration walk success over the recovery run, warm vs cold.

* M predicts warm's per-iteration walk success starts near 0 and **climbs** as
  `D` re-adapts away from the severed corridor, approaching cold's.
* R predicts warm's success tracks the recovery of **total** `D`, which under
  Tero does not re-grow globally — so little or no recovery.
* C predicts recovery only as the field re-concentrates on a *viable*
  corridor, i.e. possibly fast and possibly complete, so C and M are hard to
  separate here. **Declared in advance as weakly discriminating.**

### Measurement 5 — THE DECISIVE TEST: a placebo closure

Warm start, but close 1 edge (and its reverse) chosen to be FAR from the
pre-disruption optimal path (>= 8 hops from every path node) and
connectivity-preserving. The retained corridor is then intact and the field is
exactly as decayed and exactly as concentrated as in the real condition.

| | M predicts | R predicts | C predicts |
|---|---|---|---|
| warm walk completion under placebo | **high** (~ the pre-disruption rate): nothing is driving flux into a severed corridor | **low** (~ the real-closure rate): the field is just as decayed | **low** (~ the real-closure rate): the field is just as concentrated |

This is the one measurement on which M and {R, C} make opposite predictions
that are not tautological. **If the warm walk fails under a placebo closure at
roughly the same rate as under a real on-path closure, M is refuted** and the
cause is a property of the warm field itself, not of the closure's location.

### Measurement 6 — control: does a decayed field break the walk with NO closure at all?

Free. Record whether the walk completes on the FINAL pre-disruption iteration,
i.e. on the fully converged, fully decayed field with the graph intact.

* R (strong form) predicts the walk is already failing there.
* M predicts the walk succeeds there (the corridor it is driven down is still
  open).
* C predicts success there too: the concentrated field points down a *viable*
  corridor.

So a high success rate here refutes the strong form of R only, and does not
separate M from C.

## 3. WHAT I WILL CALL A REFUTATION

Written down now so the verdict cannot be fitted to the data afterwards:

* **M is supported** only if measurement 1 shows warm dead-ends closer to the
  closure than cold's AND than `d_random` AND than the walk's own path nodes,
  **and** measurement 5 shows the placebo closure does NOT reproduce the warm
  deficit.
* **M is refuted** if measurement 5 shows a placebo closure reproduces the warm
  deficit, OR if measurement 1 finds no spatial relationship between warm
  dead-ends and the closure.
* **R is supported** if total/median `D` predicts failure and measurement 5
  reproduces the deficit and measurement 6 shows the walk already failing
  pre-disruption.
* **C is supported** if measurement 5 reproduces the deficit, measurement 1
  finds no closure-locality, measurement 6 shows the walk succeeding
  pre-disruption, and concentration (not level) predicts failure.
* **UNDERDETERMINED** is a permitted and expected verdict, specifically
  between R and C, which differ only in whether *level* or *concentration* of
  `D` is the operative quantity. I will say so plainly rather than pick one.

## 4. SAMPLING (declared before running)

Population: the 716 successful disrupted Physarum rows (720 cells minus 4
failed), which is 30 OD pairs x 6 disrupted scenarios x 2 traffic levels x
{warm, cold}.

Sample: **6 OD pairs (2 per distance stratum, taken in the sweep's own draw
order) x all 6 disrupted scenarios x SUMO heavy x {warm, cold} = 72 recovery
runs**, plus 6 placebo cells and 6 initial solves. Heavy only, and one traffic
level, because spatial locality of a dead-end is a topological quantity and
the Parquet table above shows the warm deficit is uniform across scenarios;
spending the budget on scenarios x OD pairs rather than on a second traffic
level buys more power on the quantity under test. Budget constraint: the run
must finish well inside an hour.

**This is a small sample and I expect several of these comparisons to be
underpowered. Where they are, I will say "underpowered" rather than report a
direction.**

## 5. BEHAVIOURAL-IDENTITY CONSTRAINT

`src/bioroute/algorithms/physarum.py` is NOT modified. The walk's terminal
node is not exposed by the router, so the measurement **re-implements the walk
loop outside the router**, reading `_last_p`, `_D`, `_L`, `_G`, `_pair_index`,
`_node_idx` read-only after each `step()`. The re-implementation is validated
on every single iteration by asserting that it reaches the target exactly when
the router reports `last_extraction_method() == "max_flux_walk"`, and that its
path cost equals the router's walk-best when it improves. `git diff --stat` on
`physarum.py` must be empty.

---

## METHOD

*(appended after the pre-registration above; see `scripts/measure_physarum_warm_mechanism.py`)*

The experiment has two parts.

**Part A — full-population evidence from the sweep's own Parquet** (n = 358
paired cells, the whole disrupted Physarum population). This needs no re-run
and bears directly on predictions R3 and M3.

**Part B — a re-run that instruments what the Parquet cannot carry**: the node
at which a failing walk dead-ends, the conductivity field at the moment of
disruption, the per-iteration walk-success series, and the placebo closure.
Sample as pre-registered: 6 OD pairs x 6 scenarios x SUMO heavy x {warm, cold}
+ 6 placebo cells.

### Behavioural identity — verified

`src/bioroute/algorithms/physarum.py` is unmodified (`git diff --stat` on it
is empty). `scripts/measure_physarum_warm_mechanism.py --check` compares its
own driver loop against `algorithms.base.run_to_convergence` on both modes of
one cell: **initial iterations / cost / trace / path and recovery iterations /
cost / trace / path and `walk_best_cost` all equal, 18/18 checks pass.** The
external walk replay is additionally asserted against the router's own
`last_extraction_method()` on EVERY iteration of EVERY run in this
experiment — a disagreement raises rather than being tolerated, so every
number below was produced by a replay that matched the router's verdict on
every one of its ~40,000 iterations.

---

## RESULTS — PART A (full population, n = 358 paired cells)

### A1. The fact, re-derived and strengthened

Paired on identical `(od, scenario, traffic)` cells, warm vs cold walk
completion during recovery:

* warm **36.3%**, cold **59.5%**, n = 358 paired cells.
* **Discordant pairs: 87 complete only COLD, 4 complete only WARM.**
  Exact McNemar p = 2.3e-21.

The discordance ratio (87:4) is a far stronger statement of the effect than
the two rates, and it is the right one here because the design is paired. Warm
start does not merely lower the average: it is almost never the mode that
rescues a walk cold cannot complete.

### A2. The warm deficit does NOT scale with closure size or scenario — prediction R3 holds, M3 fails

| scenario | median edges closed | cold walk ok | warm walk ok | n per mode |
|---|---|---|---|---|
| S1 | 2 | 0.633 | 0.417 | 60 |
| S2 | 1 | 0.550 | 0.350 | 60 |
| S3 | 5 | 0.600 | 0.317 | 60 |
| S4 | 8 | 0.534 | 0.362 | 58 |
| S5 | 8 | 0.667 | 0.350 | 60 |
| S6 | 6 | 0.583 | 0.383 | 60 |

The gap is 0.17-0.32 everywhere, with no trend. Spearman correlation of walk
completion with the number of edges actually closed is **+0.151 (p = 0.004)
warm and +0.143 (p = 0.007) cold** — the same small POSITIVE correlation in
both modes, i.e. closing more edges makes the walk slightly *more* likely to
complete, and does so equally whether the field was retained or reset.

A closure eight times larger severs eight times more of the retained corridor.
If the warm deficit were caused by the retained field pointing down severed
corridors, the deficit should grow with how much of that field is severed. It
does not move at all. **This is the first piece of evidence against M, from the
full population rather than a sample.**

### A3. A decayed field does not, by itself, break the walk — the strong form of R fails

Conditioning on whether the walk completed at all during the INITIAL solve
(identical warm and cold by construction — verified, `w_pre == c_pre` on all
358 cells):

| initial-solve walk | n paired cells | cold recovery ok | warm recovery ok | discordant warm-only / cold-only | p |
|---|---|---|---|---|---|
| succeeded | 250 | 0.812 | 0.512 | 2 / 77 | 1.1e-20 |
| failed | 108 | 0.093 | 0.019 | 2 / 10 | 3.9e-02 |

Two things follow. First, the warm deficit is **concentrated in the cells where
the walk works at all** (0.300 gap vs 0.074), so it is not a floor effect.
Second, 69.8% of cells have a walk that completes during the undisrupted
solve, so a converged Physarum field is not inherently unwalkable — which
rules out the strong form of R ("the field is just too decayed to walk"). It
does **not** rule out R's weaker form, because the initial solve's walk
succeeds at *some* iteration, not necessarily the fully-decayed last one. Part
B measures the last iteration specifically.

---

## RESULTS — PART B, PILOT (the pre-registered sample: 36 paired cells + 6 placebo)

Checkpointed verbatim before the census below was run, so the census cannot be
mistaken for the only run. **Parquet cross-check: 216/216 values match** (`stretch`,
`post_disruption_stretch`, `recovery_iterations` on every non-placebo cell) —
the re-run reproduces the sweep exactly, as Phase 9b's 64/64 did.

* The effect reproduces: warm 18/36 (0.500) vs cold 27/36 (0.750), discordant
  **0 warm-only / 9 cold-only**, exact p = 0.0039.
* **M1 FAILS.** Warm terminal -> nearest closed edge median **16 hops**
  (IQR 12-21, n=19 failing cells); cold **13** (IQR 10.5-18). Paired warm vs
  cold: 0 cells warm-closer, 3 cold-closer, 15 ties, p = 0.109 — **no
  difference, and the point estimate goes the wrong way.** Both are closer than
  a random node (24 hops, p ~ 1e-3), but so are the walks' OWN non-terminal
  nodes (warm 13, cold 12) — and the warm terminal is significantly FARTHER
  from the closure than the warm walk's own path nodes (16 vs 13, p = 0.038).
  So the "closer than random" is the walk's spatial envelope, not the dead-end.
* **M2 FAILS, AND REVERSES.** `near2_excess` (retained D mass within 2 hops of
  the closure, over the uniform baseline) is **40.1 in cells where the warm
  walk SUCCEEDS vs 10.7 where it FAILS** (p = 1.4e-05), and `sev_mass_pre`
  (share of retained D the closure severs) is **0.071 in successes vs 0.000 in
  failures** (p = 0.0016). The warm failures are the cells where the closure
  barely touched the retained corridor.
* **M6 EXTENDED — the single sharpest number.** Whether the walk completes on
  the **final pre-disruption iteration, graph fully intact**, predicts warm
  recovery walk completion on **34/36 cells (94.4%, Fisher p = 7.2e-08)**.
  For cold the same predictor agrees on only 27/36 (75%): cold rescues 9 of the
  18 intact-fail cells, warm rescues 1.
* **M5 PLACEBO.** A 1-pair closure >= 8 hops from every node of the retained
  corridor reproduces the warm outcome: on the 4 OD pairs whose six real
  closures gave a unanimous warm verdict, the placebo agreed **4/4**. Paired
  per `(od, traffic, scenario)`: discordant 1 placebo-only / 1 real-only,
  p = 1.0.
* **M4 null.** Warm walk-ok rate first 10 iters 0.300 -> last 10 iters 0.500,
  but 35/36 cells are TIES (p = 0.317): no detectable re-adaptation.
* Field level, paired: warm `D_total` 18.0 vs cold 4052 (p = 2.9e-11), warm
  Gini 0.991 vs cold 0.000, warm share-of-tubes-below-`theta` **0.991** vs cold
  0.000. Pre-declared as non-discriminating; recorded for completeness.
* **CONFOUND, declared.** Warm walk completion is dominated by OD path length
  (`reference_hops` ok-median 15 vs fail-median 52, p = 0.0022), and every
  cross-cell field descriptor is correlated with it. Within the long tercile
  the reversal survives (`near2_excess` 19.6 ok vs 11.2 fail, p = 0.0025);
  the short and medium terciles are degenerate (12/0 and 1/11) and reported as
  UNDERPOWERED rather than as a direction.

### B-pilot addendum — the LEVEL of `D` is provably not the cause (refutes R's literal form)

Not pre-registered as a measurement, because it is not an empirical question —
the model answers it outright, and the probe only verifies the algebra.

Scaling every conductivity by a constant `c` scales every `w = D/L` by `c`,
hence the reduced Laplacian by `c`; with the injection vector `b` fixed the
pressures scale by `1/c`; so the flux the walk ranks candidates by,
`Q = w * (p_u - p_v)`, is **exactly unchanged**. The max-flux walk is therefore
*scale-invariant in `D`*, and "the warm field has less usable conductivity
everywhere" cannot by itself make the walk dead-end anywhere it would not have
dead-ended with a thousand times more conductivity.

Verified numerically (`--scale-check`): rescaling the retained field by 1e-3
and 1e3 at the moment of disruption, the first post-disruption walk is
**bit-identical on 6/6 OD pairs** (`x0.001_identical` and `x1000_identical`
true everywhere, including on the three pairs whose walk dead-ends).

This kills the LEVEL reading of R. What can still matter is the field's
*shape* — the relative distribution of `D`, i.e. explanation C.

---

## RESULTS — PART B, CENSUS (358 paired cells = every disrupted Physarum cell, + 60 placebo)

**Sample expanded from the pre-registered pilot, and this is disclosed rather
than quietly substituted.** The pilot showed a cell costs ~2-4 s, so the
budget allowed the whole population instead of a sample: all 30 OD pairs x 6
disrupted scenarios x both SUMO levels x {warm, cold} = **358 paired cells**
(the 2 missing cells are the same infeasible S4 cells the sweep itself failed
on), plus a placebo cell for all 30 OD pairs x both levels = 60 paired. This
is a **census of the population**, so the expansion involves no selection
choice and no outcome-dependent stopping. No prediction was changed. Every
pilot number below is reproduced at larger n.

**Parquet cross-check: 2148/2148 values match** (`stretch`,
`post_disruption_stretch`, `recovery_iterations` on all 358 cells x 2 modes x
3 fields). The re-run IS the sweep.

### B1. M5 — THE DECISIVE TEST. A closure nowhere near the corridor reproduces the warm deficit.

| condition | warm walk completes | cold walk completes | gap | discordant warm-only / cold-only | p |
|---|---|---|---|---|---|
| real closures (S1-S6) | **130/358 = 0.363** | 213/358 = 0.595 | 23.2 pp | 4 / 87 | 2.3e-21 |
| **placebo** (1 node pair, >= 8 hops from EVERY node of the retained corridor) | **26/60 = 0.433** | 42/60 = 0.700 | 26.7 pp | 0 / 16 | 3.1e-05 |

The placebo closure cannot possibly be "driving flux down a now-severed
corridor" — the corridor is untouched and the pre-disruption optimal path is
still open end to end. It reproduces the warm deficit in full, and if anything
slightly larger.

Paired against each scenario on identical `(od, traffic)` cells, warm:

| vs | n | placebo ok | scenario ok | discordant placebo-only / scenario-only | p |
|---|---|---|---|---|---|
| **S1** (1 path pair, ON the corridor) | 60 | **0.433** | **0.417** | 3 / 2 | **1.00** |
| S2 | 60 | 0.433 | 0.350 | 6 / 1 | 0.125 |
| S3 | 60 | 0.433 | 0.317 | 8 / 1 | 0.039 |
| S4 | 58 | 0.414 | 0.362 | 5 / 2 | 0.453 |
| S5 | 60 | 0.433 | 0.350 | 7 / 2 | 0.180 |
| S6 | 60 | 0.433 | 0.383 | 5 / 2 | 0.453 |

**S1 and the placebo are the same closure size and differ only in location —
one directly on the retained corridor, one at least 8 hops away — and their
warm walk-completion rates are statistically indistinguishable (41.7% vs
43.3%, p = 1.00).** That is the mechanism's central claim tested as directly as
this design can test it, and it fails.

Pooled over all six scenarios the placebo is slightly BETTER than a real
closure (34 placebo-only vs 10 real-only, p = 3.9e-04), so closing the
corridor does cost the walk *something* — on the order of **5-8 pp of a 23-27
pp gap**, driven by the multi-edge scenarios and absent against S1. Call that
the honest upper bound on M's contribution: **roughly a quarter of the effect
at most, and not via the spatial route M describes** (see B2).

### B2. M1 — no spatial relationship between warm dead-ends and the closure

Hop distance from the walk's terminal node to the nearest closed edge, over
the cells whose walk fails:

| | n | median | IQR |
|---|---|---|---|
| warm terminal -> closure | 239 | 12 | 6-17 |
| cold terminal -> closure | 263 | 12 | 6-17 |
| the failing walk's OWN non-terminal nodes -> closure (warm) | 239 | 10 | 6.25-15 |
| random graph node -> closure (null) | 358 | 24 | 20-27.4 |

* **warm vs cold, paired: 24 cells warm-closer, 20 cold-closer, 191 ties,
  p = 0.356. No difference.**
* **terminal vs the walk's own path nodes, warm: 112 / 109 / 18 ties,
  p = 0.79. The dead-end is no closer to the closure than any other node the
  walk visited.**
* Both modes' terminals are much closer than a random node (12 vs 22,
  p ~ 1e-36), but so are the walks' own path nodes — that is the walk's
  spatial envelope (it starts at the source and heads toward the target, and
  the closure is on the line between them by construction), not clustering at
  the closure.
* First post-disruption iteration only: warm 11 hops (n = 235), cold 13
  (n = 182). Warm is, if anything, FARTHER.

M predicted warm dead-ends within ~3 hops of the closure and significantly
closer than cold's. Measured: 12 hops, identical to cold.

The one spatial signal that IS there belongs to explanation C: the warm
terminal sits a median of **2 hops (IQR 1-5)** from the retained
top-5%-conductivity corridor. The walk follows the retained corridor and
dead-ends where it falls off it. (The cold figure, 0 hops, is degenerate and
must not be compared: with a uniform field the "top 5% of D" is an arbitrary
slice of the whole graph.)

### B3. M2 — the closure-relative conductivity predictors are null or REVERSED

Warm cells, walk ok (n = 130) vs walk failed (n = 228), Mann-Whitney:

| descriptor | ok median | fail median | p | direction vs M |
|---|---|---|---|---|
| `sev_mass_pre` (share of retained D the closure severs) | 0.0666 | **0.0000** | 1.8e-12 | **REVERSED** |
| `near2_excess` (D mass within 2 hops of closure / uniform baseline) | 25.44 | 9.21 | 3.6e-17 | **REVERSED** |
| `near2_D_share` | 0.2727 | 0.1050 | 3.7e-14 | **REVERSED** |
| `D_total` | 11.50 | **24.99** | 2.7e-18 | reversed vs R |
| `D_median` | 0.0000 | 0.0000 | 3.5e-04 | no usable signal |
| `D_gini` | 0.9933 | 0.9877 | 2.2e-16 | reversed vs C |
| `D_share_below_theta` | 0.9943 | 0.9877 | 2.6e-18 | reversed vs R |
| `reference_hops` | **28** | **51** | 8.9e-21 | — the real predictor |

In the cells where the warm walk fails, the median share of retained
conductivity the closure severed is **exactly zero**. M's causal story cannot
operate on a corridor the closure did not touch.

**Every one of these is confounded by OD path length**, and that confound is
declared, not buried. Within `reference_hops` terciles (cut at 33 / 55 hops):

| tercile | n | warm ok | cold ok | walk ok on the INTACT converged field |
|---|---|---|---|---|
| short | 130 | 0.715 | 0.815 | 0.862 |
| medium | 114 | 0.158 | 0.360 | 0.158 |
| long | 114 | 0.167 | 0.579 | 0.211 |

Within-tercile tests of the closure-relative predictors (warm):
`sev_mass_pre` p = 4.1e-11 (short, reversed) / 0.127 (medium) / 0.093 (long);
`near2_excess` p = 4.0e-06 (short, reversed) / 0.978 / 0.412. So after
controlling path length these predictors are **null in two of three terciles
and reversed in the third**. In no stratum do they point the way M predicts.

### B4. M3 / R — the level of D is not the cause, by proof and by measurement

Paired, at the moment of disruption (pre-declared as non-discriminating
between the explanations; reported for completeness):

| descriptor | warm median | cold median | n | p |
|---|---|---|---|---|
| `D_total` | 23.5 | 4052.0 | 358 | 2e-60 |
| `D_median` | 0.000 | 1.000 | 358 | 2e-60 |
| `D_gini` | 0.988 | 0.000 | 358 | 2e-60 |
| share of tubes below `theta` | **0.988** | 0.000 | 358 | 2e-60 |
| `near2_excess` | 11.89 | 1.000 | 358 | 5.4e-52 |

So yes, the warm field is ~170x smaller and has 98.8% of its tubes below
`theta` — but the **scale-invariance result above proves that cannot be the
cause** (uniform rescaling of D leaves the walk bit-identical, verified 6/6),
and cross-cell `D_total` points the WRONG WAY (failures have more of it).
**R is refuted in both its forms: analytically on level, empirically on
direction.**

### B5. M6 — WHAT ACTUALLY PREDICTS IT: whether the converged field was walkable BEFORE the closure

The walk completes on the **final pre-disruption iteration, graph fully
intact**, on **154/358 = 43.0%** of cells — against 69.8% on *some*
pre-disruption iteration. So convergence alone, with no closure anywhere,
already costs the walk ~27 pp.

Cross-tab of that one closure-free predictor against recovery outcome:

| mode | intact ok & rec ok | intact ok & rec fail | intact fail & rec ok | intact fail & rec fail | agreement | Fisher p | odds |
|---|---|---|---|---|---|---|---|
| **warm** | 120 | 34 | **10** | 194 | **314/358 = 0.877** | 1.4e-50 | 68.5 |
| cold | 134 | 20 | **79** | 125 | 259/358 = 0.723 | 2.2e-21 | 10.6 |

**Warm recovery walk success is 87.7% predictable from a measurement taken
before the closure existed.** The closure is almost irrelevant to it. The
entire warm-vs-cold difference lives in one cell of that table: of the 204
cells whose converged field was unwalkable, **cold rescues 79 and warm
rescues 10**.

### B6. M4 — the effect does not decay over recovery; cold's advantage DECAYS instead

| mode | walk-ok rate, first 10 iters | last 10 iters | first vs last (paired) |
|---|---|---|---|
| warm | median 0.000 | median 0.000 | 8 up / 3 down / 347 ties, p = 0.18 |
| cold | median 0.050 | median 0.000 | 23 up / **69 down** / 266 ties, **p = 1.2e-06** |

Warm shows no re-adaptation at all. **Cold's walk gets significantly WORSE as
its field converges** — and that is the positive finding this experiment
produced:

* Cold's median first successful walk is iteration **1** (IQR 1-1), and
  **82.6% of cold's walk successes first occur on iteration 1** — on the
  uniform field, before a single adaptation step.
* Of cold cells whose walk ever completes, only **60.6%** are still completing
  in their last 10 iterations; the warm figure is **97.7%** (warm's successes
  are already in the converged regime, so they have nowhere to fall).

Cold's walk advantage is therefore largely **credit banked on the uniform
initial field**, which is a field that has done no Physarum adaptation at all.
---

# VERDICT

Judged against the refutation criteria written down in section 3 before any
data was collected.

## M — "the retained field keeps driving flux down now-severed corridors" is REFUTED as the principal cause

The pre-registration said M is refuted if the placebo reproduces the warm
deficit, **or** if there is no spatial relationship between warm dead-ends and
the closure. **Both happened.**

1. A closure **>= 8 hops from every node of the retained corridor** reproduces
   the warm deficit in full: warm 43.3% vs cold 70.0% (n = 60 paired,
   discordant 0/16, p = 3.1e-05), against the real closures' 36.3% vs 59.5%.
2. The placebo and **S1** — identical closure size, one on the corridor and
   one 8+ hops away — give warm walk completion of **43.3% vs 41.7%**,
   n = 60 paired, p = 1.00.
3. Warm dead-ends sit a median **12 hops** from the nearest closed edge,
   **exactly cold's 12** (paired 24/20/191 ties, p = 0.356), and are **no
   closer to the closure than the warm walk's own other nodes** (p = 0.79).
4. In the cells where the warm walk fails, the median share of retained
   conductivity the closure severed is **exactly 0.000**; it is 0.067 where
   the walk succeeds (p = 1.8e-12). The sign is the **opposite** of M's.
5. On the full population (n = 358, no re-run needed) the deficit does not
   scale with closure size: Spearman of walk completion with edges closed is
   **+0.151 warm and +0.143 cold** — the same small positive value in both
   modes.

**M is not quite zero.** Pooled across all six scenarios the placebo is
significantly easier for the warm walk than a real closure (34 placebo-only vs
10 real-only, p = 3.9e-04), so severing the retained corridor does cost
something: **an upper bound of about 5-8 pp out of a 23-27 pp gap, ~a quarter
of the effect, and absent against S1.** But that residual is not the spatial
dead-ending M describes, because every spatial measurement is flat null. M
should be dropped from CLAUDE.md, not qualified.

## R — "the warm field is simply smaller" is REFUTED

* **Analytically.** The max-flux walk is scale-invariant in `D`: scaling every
  conductivity by `c` scales `w` by `c` and the pressures by `1/c`, leaving
  `Q = w*(p_u - p_v)` exactly unchanged. Verified numerically — rescaling the
  retained field by 1e-3 and 1e3 gives a **bit-identical first post-disruption
  walk on 6/6 OD pairs**, including on the three whose walk dead-ends. A
  uniform shortage of conductivity *cannot* cause a dead-end.
* **Empirically, with the sign against it.** Cross-cell, `D_total` is
  **higher** where the warm walk fails (24.99 vs 11.50, p = 2.7e-18), as is
  the share of tubes below `theta`. Both point the wrong way for R.

The rival as the user stated it — "less usable conductivity everywhere,
dead-ends unrelated to the closure" — got the **second half right and the
first half wrong**. The dead-ends are indeed unrelated to the closure; the
reason is not a shortage of conductivity.

## What the data DOES support: convergence, not the closure

The warm walk's failure is **87.7% predictable from a measurement taken before
the closure existed** — whether the walk completes on the final pre-disruption
iteration, on the fully converged field with the graph intact (n = 358, Fisher
p = 1.4e-50, odds 68.5). Convergence alone, with no closure anywhere, already
drops the walk from 69.8% (completes on *some* iteration) to **43.0%**
(completes on the *final* iteration) — which is essentially the warm recovery
rate of 36.3%.

And the complementary, well-powered confirmation: **cold's walk gets
significantly worse as its own field converges** (23 cells improve, 69
degrade, p = 1.2e-06 first-10 vs last-10 iterations), with **82.6% of cold's
walk successes occurring on iteration 1** — on the uniform field, before a
single adaptation step — and only 60.6% of its successful cells still
completing in their last 10 iterations.

So the mechanism the data supports is:

> **The max-flux walk works on a near-uniform conductivity field and stops
> working as the field converges onto a thin corridor. Cold start's advantage
> is not that it "explores freely" relative to the closure: it is that it
> REWINDS the field to the uniform state the walk can actually walk, and banks
> its successes in the first iterations before the field re-concentrates. Warm
> start keeps a field that was already unwalkable, and the closure's location
> is almost irrelevant.**

## Where the data is UNDERDETERMINED — stated plainly

**It cannot cleanly separate "concentration of `D`" (explanation C) from "OD
path length".** The pre-registration flagged this risk and the census confirms
it:

* `reference_hops` is the strongest single cross-cell predictor of warm walk
  failure (28 vs 51, p = 8.9e-21), and the field's concentration descriptors
  are mechanically correlated with it (a longer corridor means more high-`D`
  tubes, hence lower Gini).
* `D_gini` is *higher* where the walk succeeds (0.9933 vs 0.9877), i.e. the
  cross-cell sign is against C as well — but within the short tercile the same
  reversal persists (p = 1.1e-06) and the medium and long terciles are null
  (p = 0.036 / 0.425), which is exactly what a path-length proxy looks like.

So C's *geometry* is corroborated (the warm dead-end sits a median 2 hops from
the retained corridor — it falls off the corridor rather than stopping at the
closure), while C's *quantitative* claim that concentration per se causes
failure is **not demonstrated**. Separating them needs an experiment this one
does not contain: e.g. holding the OD pair fixed and varying `dt`/`mu` to
produce more and less concentrated converged fields on the SAME pair.

Also underpowered / null and reported as such rather than spun:

* **M4 for warm**: 347 of 358 cells are ties, p = 0.18. There is no detectable
  re-adaptation of the warm walk over recovery. (Cold's decline, by contrast,
  is well powered.)
* Within-tercile closure-relative tests in the medium and long terciles are
  null; the short tercile's significance is in M's *opposite* direction.

## Consequences for the project

1. **CLAUDE.md's Phase 8e RESULT 4 mechanism paragraph should be replaced, not
   softened.** The sentence "it keeps driving flux down now-severed corridors
   and the walk dead-ends; a cold field starts uniform and explores freely" is
   wrong in its first clause and accidentally right in its second — cold's
   benefit really is the uniform field, but not because of anything to do with
   the closure.
2. **Phase 8e RESULT 4's "cold's walk is marginally better" acquires a
   deflating explanation.** 82.6% of cold's walk successes land on iteration 1,
   on the uniform initial field. A cold "walk win" is therefore substantially a
   win by a field that has done no Physarum adaptation — which *strengthens*
   Phase 8c/9's hard requirement never to pool Physarum's stretch across
   provenance, and extends the same caution to the walk-only series: a
   `max_flux_walk` attribution on a COLD recovery is most likely an
   iteration-1, near-uniform-field result.
3. **The warm-start speed result (RESULT 1) is untouched.** Nothing here bears
   on iterations-to-recover; the warm/cold walk-completion gap is an extraction
   phenomenon, and warm start's measured 2.97x iteration / 6.87x wall-clock
   paired advantage stands.
4. **A real open question this produced**: because the walk degrades as the
   field converges, Physarum's extraction quality and its convergence criterion
   are in tension — `max|dD| < eps` runs the field to exactly the state the
   walk handles worst. Whether an earlier extraction (or extracting at the
   walk's best iteration, which `walk_best_iteration()` already records) would
   change anything is testable and untested. It is an option, not a finding.

## Reproduce

```bash
SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe scripts/measure_physarum_warm_mechanism.py --check        # behavioural identity, 18/18
SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe scripts/measure_physarum_warm_mechanism.py --scale-check  # D-level scale invariance, 6/6
SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe scripts/measure_physarum_warm_mechanism.py --all-od --traffic heavy,light
.venv/Scripts/python.exe scripts/measure_physarum_warm_mechanism.py --report-only
```

Raw per-cell records: `data/results/physarum_warm_mechanism.jsonl` (838 rows).
`src/bioroute/algorithms/physarum.py` is unmodified throughout
(`git diff --stat` on it is empty).
