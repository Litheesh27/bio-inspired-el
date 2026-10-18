# Does Physarum's adaptation improve its own path extraction at all?

**Status: PRE-REGISTRATION (predictions only). No datum had been collected when
everything above the `## METHOD` heading was written, and nothing above it has
been edited since.**

Measured by `scripts/measure_physarum_stopping_rule.py`.
Follows directly from Phase 9b-mech (`docs/tuning/physarum_warm_start_mechanism.md`).

---

## 0. WHERE THIS COMES FROM

Phase 9b-mech established, on a 358-cell census, that

> the max-flux walk works on a near-uniform conductivity field and STOPS
> WORKING AS THE FIELD CONVERGES onto a thin corridor,

and that the walk is **scale-invariant in `D`**, so only the field's *shape*
can matter. Supporting numbers, all from that document:

* Convergence alone, with no closure anywhere, drops walk completion from
  **69.8%** (completes on *some* iteration) to **43.0%** (completes on the
  *final* iteration), n = 358.
* **82.6%** of cold's walk successes and **94.6%** of warm's *first occur on
  iteration 1*.
* Cold's walk gets significantly worse as its own field converges
  (23 up / 69 down, p = 1.2e-06).

CLAUDE.md records the open consequence:

> extraction quality and the convergence criterion are in direct tension —
> `max|dD| < eps` runs the field to exactly the state the walk handles worst.

## 1. THE TRAP IN THE OBVIOUS QUESTION — declared before measuring

The convenient framing is "find a better stopping rule", which would turn a
limitation into a contribution. It presupposes something nobody has measured.

Phase 9b-mech measured walk **completion**. It did not measure walk **cost**.
Completion and quality are different quantities and could easily peak at
opposite ends of the run: a near-uniform field is easy to walk *to the target*
without the walked route being any good.

So the question underneath the stopping rule is:

> **Q1. Does Physarum's adaptation improve its own extraction AT ALL?**

### What "the uniform field" is, and why it is the right control

The control is the max-flux walk on `D = D_init` uniform with **zero adaptation
steps applied**. Note what that field is: with `D` constant, the conductance
`w = D/L` is proportional to `1/L`, and `L` is the snapshot's minimum
congested travel time over the node pair. So the walk is following an
**electrical flow on a travel-time-resistive network**, solved once. It
contains **none** of the Tero adaptation — no `dt`, no `mu`, no reinforcement.
If that beats the adapted field, the max-flux walk is a static
electrical-flow heuristic wearing Physarum's name.

### The control is FREE, and that is a fact about the model, not a shortcut

`step()` solves the pressures with the `D` that was in place *before* the
step's own update (it advances `self._D` after the solve, which is exactly why
`measure_physarum_warm_mechanism.replay_walk` takes `D_pre`). Therefore
**iteration 1 of a fresh solve IS the zero-adaptation uniform-field control**,
bit-for-bit. This is asserted rather than assumed: `--uniform-check` builds the
control explicitly (converge a router, then overwrite `_D` with `D_init`, solve,
walk) and requires the resulting walk to be identical to iteration 1's.

## 2. PRE-REGISTERED PREDICTIONS

All comparisons are **paired on identical cells**. Medians + IQR, `n` on every
number, discordant counts on every rate comparison. Completion rate and
conditional-on-completion stretch are **different populations** and are
reported as such.

### Q1 — adaptation vs the unadapted field

Three comparators per cell, on the *same* solve:

* `iter1` — the uniform-field control (iteration 1).
* `best_adapted` — the best walk over iterations **2..K** (K = the iteration the
  run actually stopped at). This is a **best-of-many** statistic and is
  therefore **biased in adaptation's favour**; that bias is declared here, and
  the iteration-by-iteration series in Q2 is the unbiased view.
* `final` — the convergence iteration, i.e. what the shipped router's field
  looks like when it stops.

| quantity | prediction | confidence |
|---|---|---|
| completion: `iter1` vs `final` | `iter1` **much higher** | ~certain (9b-mech already implies it) |
| completion: `iter1` vs `best_adapted` | `iter1` **lower** — best-of-many must win on a binary | ~certain, and uninformative; recorded so it cannot be presented as a finding |
| **stretch (conditional on completion): `iter1` vs `best_adapted`** | **`best_adapted` BETTER.** This is the substantive prediction. | **moderate** |
| stretch: `iter1` vs `final` | `final` better where it completes | moderate |
| `walk_best_iteration` | **> 1 on a majority of cells** | moderate |

**Reasoning for the substantive prediction, stated so it can be wrong:** the
Tero model's whole claim is that `D` concentrates on the shortest path, so flux
at convergence should rank the optimal next hop more reliably than a one-shot
resistive flow does. Iteration 1's flow has no optimality property at all — a
1/L-weighted electrical flow spreads current over many parallel routes.

**If `iter1` stretch is equal to or better than `best_adapted` stretch, Q1's
answer is that adaptation contributes nothing to extraction, and I will report
that as a negative result and stop looking for a stopping rule.** Writing this
down now so the verdict cannot be fitted afterwards.

### Q2 — is there a stopping rule?

Per-iteration series of (a) walk completion and (b) walk stretch, pooled over
cells and per cell. Where does each peak? Is the argmax concentrated enough for
a fixed rule?

* Prediction: completion peaks at iteration 1 and decays monotonically.
* Prediction: if stretch improves at all, its peak is **late and dispersed**,
  so a fixed-iteration rule will not capture it, and completion and quality
  peak at opposite ends — i.e. **no single fixed rule can win both**.
* `walk_best_iteration()` is verified before use: my replay's own
  first-argmin-over-walk-costs must equal it, and my min walk cost must equal
  `walk_best_cost()`, on every cell. A mismatch raises.

### Q3 — what does stopping early cost the REPORTED result?

`best_cost` is a running min over every iteration and every extraction method,
so `RouterResult.trace[k-1]` is exactly "the pooled best cost if we had stopped
at iteration k". That makes the cost of early stopping readable off the trace
with **no extra computation and no re-definition**.

Reported: pooled stretch at `k = walk_best_iteration` vs at `k = K`, paired;
the count of cells where stopping early strictly worsens the pooled answer; and
the share of cells whose pooled optimum is only reached after
`walk_best_iteration`.

* Prediction: **the pooled cost is barely affected.** Phase 8b showed `theta =
  0.05` while `D` starts at 1.0, so for ~18 iterations the theta subgraph is
  essentially the whole graph and the "fallback" is plain Dijkstra — which is
  exact. So the pooled running min should already be at its final value very
  early, and early stopping should be nearly free *in pooled terms*. If so, the
  honest reading is not "early stopping is safe" but **"the pooled number was
  never Physarum's to begin with"**, which is Phase 8c's finding again.

### Q4 — warm vs cold, or a property of the solve?

* Prediction: **a property of the solve.** Cold recovery's iteration 1 is a
  uniform field (the state the walk handles best); warm recovery's iteration 1
  is an already-converged field (the state it handles worst). So the
  iteration-1 advantage should be large for cold and absent for warm, with no
  separate warm/cold mechanism needed beyond "where in the convergence
  trajectory the field is".

## 3. WHAT I WILL CALL EACH VERDICT

* **Adaptation helps extraction** only if `best_adapted` stretch beats `iter1`
  stretch on a paired test over cells where both complete, with the discordant
  counts reported, AND the per-iteration series shows the improvement is not an
  artefact of taking a best-of-many.
* **Adaptation does not help extraction** if `iter1` stretch is statistically
  indistinguishable from or better than `best_adapted`. Then there is no
  stopping rule to propose, and the correct contribution is the negative
  result.
* **A stopping rule is worth proposing** only if (i) adaptation helps, (ii) the
  peak is concentrated enough for a fixed rule, and (iii) Q3 shows the pooled
  cost of adopting it is small. All three, or it is not a fix.
* **UNDERPOWERED / UNDERDETERMINED** are permitted verdicts and will be said
  plainly.

## 4. SAMPLING (declared before running)

The sweep's own config (`configs/experiments/disruption.yaml`, hash
`14a535c02b83`), its own graph, snapshots, OD draw (seed 8080) and
`scenario_seed` 1337. `viz.traces.load_context` loads all of it.

* **Part 1 (Q1, Q2, Q3)** — a **census** of the undisrupted solve:
  30 OD pairs x 2 SUMO levels (light, heavy) = **60 solves**. Every iteration
  of every solve is recorded: walk completion, walk path cost, the router's
  running pooled best, and the extraction method.
* **Part 2 (Q4)** — 30 OD pairs x {S1, S6} x SUMO heavy x {warm, cold} =
  **120 recovery runs** (240 solves). S1 is the smallest closure and S6 the one
  that strands nodes by construction, so they bracket the scenario axis; the
  warm/cold contrast is uniform across scenarios in 9b-mech's table, so
  spending the hour on all 30 OD pairs rather than all 6 scenarios buys more
  power on the quantity under test.

Budget: the whole thing must finish inside the hour. If the timing pilot says
Part 2 does not fit, Part 2 shrinks and that is disclosed here rather than
quietly substituted.

## 5. BEHAVIOURAL-IDENTITY CONSTRAINT

`src/bioroute/algorithms/physarum.py` is **NOT modified** (`git diff --stat` on
it must be empty). The walk is **not re-implemented a third time**: this script
imports `replay_walk` from `scripts/measure_physarum_warm_mechanism.py`, which
Phase 9b-mech already validated against the router's own
`last_extraction_method()` on ~40,000 iterations. The same assertion runs on
every iteration here, and the walk's cost is additionally reconciled against
`walk_best_cost()` / `walk_best_iteration()` on every cell.

---

## METHOD

*(appended after the pre-registration above)*

See `scripts/measure_physarum_stopping_rule.py`.

The experiment has two parts, both on the sweep's own config/graph/snapshots/OD
draw (`14a535c02b83`, baseline hash `285f18f9a225`, OD seed 8080,
`scenario_seed` 1337), loaded through `viz.traces.load_context`.

**Part 1 — Q1/Q2/Q3, a census of the undisrupted solve**: 30 OD pairs x 2 SUMO
levels = **60 solves**, every iteration recorded (walk completion, walk path
cost, the router's pooled running min, extraction method).
**Part 2 — Q4**: 30 OD pairs x 6 disrupted scenarios x 2 SUMO levels x
{warm, cold}.

### Behavioural identity and instrument validity — verified

* `git diff --stat src/bioroute/algorithms/physarum.py` is **empty**.
* The walk is **not** re-implemented: `replay_walk` is imported from
  `scripts/measure_physarum_warm_mechanism.py`. Its agreement with the
  router's own `last_extraction_method()` is asserted on **every iteration of
  every run**, so a disagreement raises rather than being tolerated.
* **`walk_best_iteration()` is verified before being relied on, not assumed.**
  Each solve reconciles the replay's own min walk cost, first strict argmin
  iteration, first success iteration and success count against
  `walk_best_cost()` / `walk_best_iteration()` /
  `walk_first_success_iteration()` / `walk_success_count()`:
  **60/60 Part-1 solves agree on all four fields.**
* **The uniform-field control is iteration 1, and that identity is tested, not
  argued.** `--uniform-check` converges a router, overwrites `_D` with
  `D_init`, re-solves and walks: the resulting walk is **identical to a fresh
  router's iteration 1 on 6/6 OD pairs** (node-for-node, same completion
  verdict).
* **Parquet cross-check: 60/60 Part-1 `stretch` values match the sweep's own
  Physarum rows to 1e-6.** The re-run IS the sweep.

---

## RESULTS — PART 1 (census, n = 60 undisrupted solves)

### Q1a. COMPLETION — adaptation makes the walk complete LESS often (9b-mech, reproduced)

All three rates are over the **same 60 solves**, read off one run each:

| field the walk is run on | completes | note |
|---|---|---|
| **iteration 1 — uniform `D`, ZERO adaptation** | **34/60 = 0.567** | the control |
| best of iterations 2..K (an **oracle**, best-of-many) | 42/60 = 0.700 | biased in adaptation's favour by construction |
| **final iteration — what the router ships** | **26/60 = 0.433** | |
| any iteration | 42/60 = 0.700 | |

Paired: iteration 1 vs final, discordant **13 iter1-only / 5 final-only**,
exact p = 0.096. Iteration 1 vs the oracle, discordant 0 / 8, p = 0.0078 —
which is uninformative, because a best-of-many cannot lose a binary.

### Q1b. QUALITY — AND HERE THE SIGN REVERSES. Adaptation improves the walk's PATHS, decisively.

Walk stretch **conditional on completion**. The marginal medians are over
differently-composed samples and are **not** comparable (project standing
rule); the paired tests are.

| comparator | n completing | median | IQR |
|---|---|---|---|
| iteration 1 (uniform, zero adaptation) | 34 | **1.4367** | 1.1479-1.5933 |
| best of iterations 2..K (oracle) | 42 | 1.0928 | 1.0000-1.3277 |
| final iteration | 26 | **1.0059** | 1.0000-1.2962 |

**Paired, on the cells where BOTH complete:**

* **iteration 1 vs best-of-2..K, n = 34: median 1.4367 vs 1.1116. Adapted
  better on 26, uniform better on 0, 8 ties. Wilcoxon p = 8.3e-06.**
* **iteration 1 vs the FINAL iteration, n = 21: median 1.3509 vs 1.0000.
  Final better on 15, iteration 1 better on 3, 3 ties. p = 0.0156.**
* Monotone within a run: first vs **last** completing iteration, n = 41,
  median 1.3983 -> 1.1382, improved on 23, worsened on 7, 11 ties, p = 0.0175.

Exact-match (stretch == 1.0) **conditional on completing**: iteration 1
**2/34 = 5.9%**, best-of-2..K 12/42 = 28.6%, final iteration
**12/26 = 46.2%**.

And unconditionally — the honest "which field would you ship" table, where a
non-completion counts as a loss:

| field | completes (n=60) | exact-match over ALL 60 | within-10% over ALL 60 |
|---|---|---|---|
| iteration 1 (uniform) | 0.567 | **2/60 = 0.033** | 6/60 = 0.100 |
| best of 2..K (oracle) | 0.700 | 12/60 = 0.200 | 21/60 = 0.350 |
| final iteration (shipped) | 0.433 | **12/60 = 0.200** | 16/60 = 0.267 |

### Q1 VERDICT — stated before anything else, as the brief demanded

**Physarum's adaptation DOES improve its own extraction, and by a large
margin.** The uniform, zero-adaptation field — an electrical flow on a
travel-time-resistive network, containing none of the Tero model — walks to the
target *more often* (56.7% vs 43.3%) but produces **much worse routes**: median
stretch **1.44 vs 1.01**, exact-match **5.9% vs 46.2%** conditional on
completing, and **3.3% vs 20.0%** unconditionally. Paired on the same solves it
loses **26-0** against the adapted field and **15-3** against the final
iteration.

**So the max-flux walk is NOT a static electrical-flow heuristic.** The
negative result the brief asked me to be ready to report is **not** what the
data says, and the pre-registered substantive prediction was right. What IS
true is that completion and quality move in **opposite directions** with
adaptation — which is why 9b-mech's completion-only measurement looked like a
pure indictment of convergence and is not one.

### Q2. IS THERE A BETTER STOPPING RULE? NO — and the ceiling is the sharp part.

Where things peak (n = 42 solves whose walk ever completes):

* First successful walk iteration: median **1** (IQR 1-1), == 1 on
  **34/42 = 81.0%** — 9b-mech's figure, reproduced.
* `walk_best_iteration` (first strict argmin of walk cost): median **26.5**
  (IQR 12.25-33.5), == 1 on only **8/42 = 19.0%**. As a fraction of the run it
  is median **0.262** of K (IQR 0.111-0.357). **So first-success and
  best-quality are different iterations, and `walk_best_iteration` is
  dispersed** — K itself is median 98 iterations (IQR 90-110).
* Pooled per-iteration completion falls monotonically from 0.567 (iters 1-3) to
  0.433 (iter 60) while the median completing walk's stretch falls from 1.4367
  to 1.0059. **The two curves cross; neither has an interior optimum.**

A **fixed** stopping rule, scored over all 60 solves:

| stop at k | walk completes | exact-match | median stretch given completion |
|---|---|---|---|
| 1 | 0.567 | 2/60 = 0.033 | 1.4367 |
| 10 | 0.550 | 2/60 = 0.033 | 1.3509 |
| 25 | 0.500 | 3/60 = 0.050 | 1.1752 |
| 40 | 0.400 | 11/60 = 0.183 | 1.0079 |
| **50** | 0.450 | **12/60 = 0.200** | 1.0071 |
| 60 | 0.433 | 12/60 = 0.200 | 1.0059 |
| convergence (median K = 98) | 0.433 | **12/60 = 0.200** | 1.0059 |

**No fixed k beats `max|dD| < eps` on quality.** The best fixed rule ties it.

And the **oracle's ceiling**, which is the decisive number:

* **All 12 exact-match walks in the whole census are already exact at the final
  iteration** (exact at *some* iteration: 12; exact at the *final* iteration:
  12; overlap 12). **A perfect per-cell stopping oracle buys ZERO additional
  exact matches.**
* Paired on the 26 solves where the final iteration completes, the oracle over
  all iterations is better on only **4**, ties on **22**, identical median
  (1.0059).
* What the oracle *does* buy is **16 extra completions** — the cells where the
  walk completes at some iteration but not the final one. Those 16 are poor:
  median stretch **1.2479**, **0 exact**, 5 within 10%, and their
  `walk_best_iteration`s are scattered (1, 1, 1, 1, 4, 11, 11, 17, 24, 26, 27,
  27, 28, 29, 30, 31). Net effect of a perfect oracle: within-10% 0.350 vs the
  shipped rule's 0.267 (**+8.3 pp**), exact-match unchanged at 0.200.

**Q2 VERDICT: there is no stopping rule worth proposing.** Note also that
"stopping" is the wrong lever anyway — the router **already** retains
`walk_best_path()` / `walk_best_cost()` across every iteration, so the oracle
is available for free without stopping anything. The measured ceiling of that
oracle is +8.3 pp of within-10% and +0.0 pp of exact match, bought by
completing 16 more mediocre paths.

### Q3. WHAT STOPPING EARLY COSTS THE REPORTED RESULT — more than I predicted. My P4 was WRONG.

`trace[k-1]` is exactly "the pooled best cost had the run stopped at iteration
k", so this is read off the sweep's own quantity with no redefinition.

Fixed stop at k, pooled stretch over all 60 solves:

| stop at k | median pooled stretch | pooled exact-match | p90 |
|---|---|---|---|
| 1 | 1.0251 | 28/60 = 0.467 | 1.6698 |
| 10 | 1.0049 | 29/60 = 0.483 | 1.6207 |
| 25 | 1.0000 | 35/60 = 0.583 | 1.4155 |
| 40 | 1.0000 | 51/60 = 0.850 | 1.0201 |
| 60 | 1.0000 | 53/60 = 0.883 | 1.0062 |
| convergence | 1.0000 | **53/60 = 0.883** | 1.0062 |

Stopping at each cell's own `walk_best_iteration`, paired (n = 42 solves whose
walk ever completes):

* pooled exact-match **22/42 stopped-early vs 35/42 at convergence**;
* strictly worse pooled on **13/42** cells, better on **0**, 29 ties,
  Wilcoxon **p = 0.00147**;
* medians both 1.0000, but **p90 1.5323 vs 1.1560**.

**I predicted this would be nearly free, on the grounds that `theta = 0.05`
while `D` starts at 1.0 so the early "fallback" is plain Dijkstra. That
prediction is REFUTED.** The pooled optimum is reached by iteration 1 on only
**30/60 = 50%** of solves (median first iteration at the final pooled value:
2.5, IQR 1-28.25), so on the other half the reported answer genuinely arrives
later. Early stopping roughly **halves** the pooled exact-match rate (0.467 at
k=1 vs 0.883 at convergence) and inflates the p90 by ~0.5.

Provenance of the reported path over the 60 solves: `theta_subgraph_fallback`
28, `max_flux_walk` 18, `full_graph_fallback` 14 — i.e. 70% not from the
model's own extraction, reproducing Phase 8b/8c on the sweep's own graph.

### Part 1 by OD path length (9b-mech's dominant confound, carried forward)

`reference_hops` terciles, cuts at 33 / 54 hops, n = 20 each:

| tercile | iter1 completes | final completes | iter1 median stretch | best-2..K median stretch |
|---|---|---|---|---|
| short | 0.900 | 0.900 | 1.1948 (n=18) | **1.0000** (n=20) |
| medium | 0.300 | 0.200 | 1.4249 (n=6) | 1.0751 (n=10) |
| long | 0.500 | 0.200 | 1.5917 (n=10) | 1.2822 (n=12) |

The Q1 quality result holds in **every** tercile and is largest on long pairs.
It is not a path-length artefact.

### Q2 aside — the ONE thing a fixed cut-off does buy, and it is compute, not quality

Labelled as an aside because it is not an answer to any of Q1-Q4 and must not
be presented as one. K is median **98** iterations (IQR 90-111, max 158), but
the walk's median stretch has already plateaued by ~iteration 40-60
(1.0079 at k=40, 1.0059 at k=60, 1.0059 at convergence):

| stop at k | pooled exact | walk exact | walk completes | iterations saved |
|---|---|---|---|---|
| 40 | 51/60 | 11/60 | 24/60 | 60.5% of all iterations |
| 50 | 52/60 | 12/60 | 27/60 | 50.6% |
| **60** | **53/60** | **12/60** | 26/60 | **40.7%** |
| 80 | 53/60 | 9/60 | 20/60 | 21.6% |
| convergence | 53/60 | 12/60 | 26/60 | 0% |

**A fixed stop at iteration 60 is exactly equivalent to `max|dD| < eps` on this
census — identical pooled exact-match (53/60) and identical walk exact-match
(12/60) — while cutting 40.7% of the iterations.** Stopping at each cell's own
`walk_best_iteration` would cut 75.5%, but that is the variant Q3 shows costs
13/42 cells their pooled answer.

This is a cost optimisation of a 3-8 ms solve, measured on **60 undisrupted
solves only**, and it changes no reported number. It is not the "stopping rule"
the hypothesis was after, and nothing in Phase 9 should be re-run for it.

---

## RESULTS — PART 2, Q4 (census: 358 paired recovery cells)

Sample expanded from the pre-registered `{S1, S6}` x heavy to the whole
population, and that is **disclosed rather than quietly substituted**: the
timing pilot measured 0.3 s/solve, so the budget allowed all 30 OD pairs x 6
disrupted scenarios x both SUMO levels x {warm, cold} = **358 paired cells**
(the 2 missing are the same infeasible S4 cells the sweep itself failed on).
This is a **census of the population**, so the expansion involves no selection
choice and no outcome-dependent stopping. No prediction was changed.

* **Parquet cross-check: 2148/2148 values match** (`stretch`,
  `post_disruption_stretch`, `recovery_iterations` on all 358 cells x 2 modes x
  3 fields).
* Reconciliation against the router's walk trackers: **358/358 warm and
  358/358 cold** agree on all four fields.

### Q4a. COLD recovery's iteration 1 reproduces Part 1 exactly; WARM's does not, because warm's iteration 1 is not a uniform field

`apply_disruption(warm=False)` resets `D` to `D_init`, so **cold recovery's
iteration 1 is the same uniform-field control Part 1 measured** — and it
behaves identically:

| | Part 1 (undisrupted, n=60) | cold recovery (n=358) | warm recovery (n=358) |
|---|---|---|---|
| iteration-1 walk completes | 0.567 | **0.492** | 0.344 |
| iteration-1 walk stretch, median | **1.4367** | **1.3388** | **1.0737** |
| K (iterations run), median | 98 | **98** (IQR 90-111) | **42** (IQR 1-51) |

Warm start retains an already-converged field, so its iteration 1 is a
*converged* field, not a uniform one — and its iteration-1 walk is correspondingly
much better. **Paired on the 105 cells where both modes' iteration-1 walk
completes: warm median 1.0617 vs cold 1.2712, warm better on 61, cold better on
25, 19 ties, Wilcoxon p = 1.7e-04.**

(Warm's K is median 42 because **173/358 = 48.3% of warm recoveries converge in
exactly 1 iteration** and cold's does so 0/358 times — Phase 8e RESULT 1's
bimodality, reproduced here independently.)

### Q4b. At the FINAL iteration warm and cold are INDISTINGUISHABLE — which deflates Phase 8e RESULT 4 the rest of the way

Paired on identical `(od, scenario, traffic)` cells, n = 358:

| quantity | warm | cold | paired test |
|---|---|---|---|
| walk completes at recovery **iteration 1** | 0.344 | **0.492** | discordant 18 warm-only / 71 cold-only, p = 1.3e-08 |
| walk completes at **any** iteration | 0.363 | **0.595** | discordant 4 / 87, p = 2.3e-21 |
| walk completes at the **final** iteration | **0.355** | **0.360** | discordant 7 / 9, **p = 0.804 — no difference** |
| **final-iteration** walk stretch (n = 120 both complete) | 1.0615 | 1.0323 | 14 warm-better / 21 cold-better / 85 ties, **p = 0.694 — no difference** |

**The entire warm-vs-cold walk gap lives in the early, near-uniform iterations
and vanishes once both fields are adapted.** Phase 8e RESULT 4 measured
`post_disruption_walk_stretch`, which is `walk_best_cost()` — a min over *all*
iterations — so cold's edge there is bought on iterations the model has barely
run.

The 87 cells where only cold's walk ever completes are exactly that:
**first success on iteration 1 on 70/87 (80.5%)**, median best walk stretch
**1.3713**, **1 exact match**, 7 within 10%. Cold's walk-completion advantage
is 87 cells of mostly poor paths found on a field that has done no adaptation.

**9b-mech CONSEQUENCE 2 therefore strengthens into something sharper.** It said
a cold `max_flux_walk` attribution is "most likely an iteration-1,
near-uniform-field result". Measured here: those results are not merely
unadapted, they are **quantitatively bad** (median stretch 1.34-1.37 vs 1.03-1.06
at the final iteration), and at the only point of comparison where both modes'
fields are genuinely adapted there is **no warm/cold difference at all**
(p = 0.69, p = 0.80).

### Q4c. Q1's answer holds inside the recovery, in both modes

Paired iteration 1 vs best-of-2..K, walk stretch:

| mode | n (both complete) | iter1 | best 2..K | adapted better / iter1 better / ties | p |
|---|---|---|---|---|---|
| cold | 176 | 1.3388 | 1.1691 | **120 / 1 / 55** | **3.2e-21** |
| warm | 82 | 1.0214 | 1.0013 | 27 / 5 / 50 | 9.98e-04 |

So adaptation improves the walk's paths in the undisrupted solve (26/0), in cold
recovery (120/1) and in warm recovery (27/5). **Q1's answer is a property of the
solve, not of the start mode**, exactly as pre-registered — and warm's weaker
effect is explained by its field already being adapted at iteration 1, so there
is less left to gain.

### Q4d. Q2's answer holds too, and the fixed-rule tables are worse in recovery

Cold recovery: no fixed k beats convergence. Walk exact-match over all 358 peaks
at **42/358 = 0.117** at k = 50-60; the final iteration already gives
44/358 = 0.123. The oracle over all iterations gives 56/358 = 0.156 at the cost
of 84 extra completions whose median stretch is poor.
Warm recovery: `walk_best_iteration` is median **1** (IQR 1-2), == 1 on
96/130 = 73.8%, because half of warm's runs are one iteration long. There is
nothing to stop early.

### Q4e. Q3's answer splits cleanly by mode, and the reason is mechanical

| | cold recovery | warm recovery |
|---|---|---|
| pooled optimum already reached by iteration 1 | 211/358 = 0.589 | **327/358 = 0.913** |
| stopping at `walk_best_iteration` strictly worsens pooled | **67/213**, p = 1.1e-12 | 4/130, p = 0.068 |
| pooled exact-match, k=1 vs convergence | 199/358 = 0.556 -> **315/358 = 0.880** | 264/358 = 0.737 -> 282/358 = 0.788 |

Early stopping is expensive for cold (which must re-converge) and nearly free
for warm (which has already converged). Same quantity, opposite answers — so
**any statement about the cost of early stopping must name the start mode.**

### Q4f. An unplanned provenance finding: the theta-subgraph fallback is ALSO a near-uniform-field artefact

Provenance of the reported path over the 358 recovery cells:

| | `max_flux_walk` | `theta_subgraph_fallback` | `full_graph_fallback` |
|---|---|---|---|
| cold | 95 | **201** | 62 |
| warm | 119 | **0** | **239** |

**Warm recovery produces the theta-subgraph fallback ZERO times.** With `theta =
0.05` and a retained field in which ~98.8% of tubes are below `theta`
(9b-mech B4), the theta subgraph is near-empty and almost never connects s to t,
so warm falls straight through to `full_graph_fallback` = plain Dijkstra on
239/358 = 66.8% of cells. Cold starts at `D = 1.0`, where the theta subgraph is
essentially the whole graph and the "fallback" is Dijkstra anyway (Phase 8b).
So **both** of Physarum's fallbacks are artefacts of a near-uniform field, and
Phase 8b/8c's provenance finding is if anything understated: on warm recoveries
the reported Physarum path is Dijkstra's two times in three, by an entirely
different route than on cold ones.

### Observation outside Q1-Q4, flagged because it is in the Parquet already

Paired on all 358 cells, the **pooled** `post_disruption_stretch` is worse for
warm than cold: warm better on 11, cold better on 57, 290 ties,
Wilcoxon **p = 4.3e-07** (medians both 1.0000; mean 1.0669 vs 1.0311, p90 1.1680
vs 1.0127, exact-match 282/358 vs 315/358). This is derivable from
`final_14a535c02b83.parquet` without any re-run and is **not** something Phase 8e
reported. It is the quality side of RESULT 1's speed trade — warm start recovers
~3x faster in iterations and gives up some tail quality — and it is stated here
as an observation for whoever writes it up, not as a result of this experiment.

---

# VERDICT

Judged against the refutation criteria written in section 3 before any datum was
collected.

## Q1 — PRIMARY. **YES: Physarum's adaptation improves its own extraction, decisively.**

The uniform, zero-adaptation field — an electrical flow on a travel-time-resistive
network, containing none of the Tero model — completes the walk **more** often but
produces **much worse routes**:

* Undisrupted census (n = 60): walk stretch median **1.4367** at iteration 1 vs
  **1.0059** at the final iteration. Paired, iteration 1 loses **26-0** to the
  best adapted iteration (p = 8.3e-06) and **15-3** to the final iteration
  (p = 0.0156). Exact-match given completion: **5.9% -> 46.2%**. Unconditionally:
  **3.3% -> 20.0%**.
* Cold recovery census (n = 358): iteration 1 loses **120-1** to the best adapted
  iteration (p = 3.2e-21).
* Warm recovery census (n = 358): **27-5** (p = 9.98e-04), weaker only because
  warm's iteration 1 is already an adapted field.
* It holds in every `reference_hops` tercile and is largest on long pairs.

**So the max-flux walk is NOT a static electrical-flow heuristic, and the major
negative result the brief was prepared to accept is not what the data says.**
The honest shape of the finding is that **completion and quality move in
OPPOSITE directions with adaptation**, which is why 9b-mech's completion-only
measurement looked like a pure indictment of convergence and is not one. That
asymmetry is itself the reportable thing: the near-uniform field is *walkable*
precisely because it is *uninformative* — flux is spread over many routes, so
every node has a positive-flux successor, and the route the walk strings together
is 40%+ off optimal.

## Q2 — **NO. There is no stopping rule worth proposing, and the ceiling is what settles it.**

* No **fixed** k beats `max|dD| < eps` on quality, in any of the three censuses.
  The best fixed rule ties it (k = 50-60).
* **A perfect per-cell oracle buys ZERO additional exact matches** in the
  undisrupted census: all 12 exact-match walks are already exact at the final
  iteration. Paired on the 26 cells where the final iteration completes, the
  oracle wins on 4 and ties on 22 with an identical median.
* What an oracle buys is **extra completions of mediocre paths**: +16 cells at
  median stretch 1.2479 with 0 exact matches (undisrupted), +84 similar in cold
  recovery. Net: within-10% 0.267 -> 0.350 (**+8.3 pp**), exact-match unchanged.
* `walk_best_iteration` is **dispersed** (median 26.5, IQR 12.25-33.5, i.e. 0.26
  of K with IQR 0.11-0.36), so no fixed rule could capture it even if it were
  worth capturing.
* And **"stopping" is the wrong lever anyway**: the router already retains
  `walk_best_path()` / `walk_best_cost()` across all iterations, so the oracle
  is available for free without stopping anything. Its measured ceiling is the
  +8.3 pp above.

**The hypothesis "there is a better stopping rule" is not supported.** The one
thing a fixed cut-off does buy is compute (a stop at iteration 60 is exactly
equivalent on the undisrupted census while cutting 40.7% of iterations), and
that is a cost optimisation of a 3-8 ms solve, not a contribution.

## Q3 — **Stopping early costs the reported result MORE than I predicted. My prediction was REFUTED.**

I predicted it would be nearly free, because `theta = 0.05` while `D` starts at
1.0 makes the early fallback plain Dijkstra. Measured:

* Undisrupted: pooled exact-match **0.467 at k=1 vs 0.883 at convergence**; p90
  1.6698 vs 1.0062. Stopping at each cell's own `walk_best_iteration` is
  strictly worse on **13/42** cells (better on 0, p = 0.00147) and lifts p90 from
  1.1560 to 1.5323.
* The pooled optimum is reached by iteration 1 on only **50%** of undisrupted
  solves (median 2.5, IQR 1-28.25), so on the other half the reported answer
  genuinely arrives later.
* **Cold recovery: strictly worse on 67/213 cells, p = 1.1e-12.** Warm recovery:
  4/130, p = 0.068 — nearly free, because warm has already converged.

So a stopping rule that improved extraction would still have had to pay for it,
and the answer must name the start mode.

## Q4 — **It is a property of the solve's position on the convergence trajectory, not of warm vs cold.**

Pre-registered prediction, confirmed. Cold recovery's iteration 1 is the uniform
control and behaves like Part 1's (completion 0.492 vs 0.567; stretch 1.3388 vs
1.4367). Warm's iteration 1 is an already-converged field and its walk is
**significantly better than cold's** (paired n = 105, 1.0617 vs 1.2712,
61/25/19, p = 1.7e-04). And at the **final** iteration the two modes are
**indistinguishable** on both completion (0.355 vs 0.360, p = 0.804) and stretch
(n = 120, p = 0.694).

## What this means for the project

1. **Phase 9b-mech's headline sentence needs one clause added, not replacing.**
   "The max-flux walk works on a near-uniform field and stops working as the
   field converges" is true *of completion* and the opposite is true *of
   quality*. The accurate statement is: **as the field converges the walk
   completes less often and produces much better paths when it does.** Reporting
   only the first half misrepresents the model.
2. **The tension CLAUDE.md records is real but resolves in favour of the
   existing criterion.** `max|dD| < eps` runs the field to the state the walk
   *completes* worst on and *routes* best on, and no rule tested here beats it.
   The open item should be closed as TESTED AND NEGATIVE rather than left open.
3. **Phase 8e RESULT 4's "cold's walk is marginally better" should be retired,
   not just deflated.** Paired at the final iteration there is no difference
   (p = 0.69); cold's advantage is 87 cells of iteration-1 near-uniform-field
   walks at median stretch 1.37 with one exact match. 9b-mech called this "a win
   by a field that has not yet run the model"; this experiment measures how bad
   those wins are.
4. **A new no-pooling consequence.** A `max_flux_walk` attribution is not one
   kind of event: an early-iteration walk and a converged-field walk differ by
   ~0.35 in median stretch. Any Physarum walk-only series should say **which
   iteration regime** it came from, the same way Phase 9 already requires
   provenance labelling. And `theta_subgraph_fallback` turns out to be a
   near-uniform-field artefact too — it fires 201 times on cold recoveries and
   **0 times on warm ones**, where `full_graph_fallback` (plain Dijkstra) takes
   239/358 cells instead.
5. Nothing here touches Phase 8e RESULT 1 (warm start's 2.97x iteration /
   6.87x wall-clock paired advantage). No figure, no table and no number in
   `docs/results/summary.md` changes as a result of this experiment.

## Where this is UNDERDETERMINED or weak — stated plainly

* The undisrupted census is **60 solves**. The exact-match counts it turns on are
  small integers (12 exact matches), and the Q1 paired tests, while strongly
  signed, rest on n = 21-34. The two recovery censuses (n = 358 each) are what
  carries the weight, and they agree in direction.
* **"Adaptation improves quality" is not the same as "Physarum's adaptation is
  what finds the optimum."** The pooled reported path still comes from the
  model's own walk only 18/60 (undisrupted), 95/358 (cold) and 119/358 (warm) of
  the time. Q1 says the walk gets better as the field adapts; it does **not**
  overturn Phase 8c's finding that Physarum's reported competence is mostly the
  fallback's.
* The comparison "iteration 1 vs best-of-2..K" uses a best-of-many on one side.
  That bias is declared in the pre-registration, points **against** the measured
  conclusion on completion and **for** it on quality, and is why the
  final-iteration comparison (a single fixed iteration on both sides) is
  reported alongside it. Both agree.
* This experiment does not separate "more adaptation" from "more iterations of a
  Laplacian solve". Separating them needs the experiment 9b-mech already named:
  fix the OD pair and vary `dt`/`mu` so that fields of different concentration
  are reached in a comparable number of iterations. Still untested.

## Reproduce

```bash
SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe scripts/measure_physarum_stopping_rule.py --uniform-check --n-od 6
SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe scripts/measure_physarum_stopping_rule.py --part1 --levels heavy,light
SUMO_HOME="C:/Sumo" .venv/Scripts/python.exe scripts/measure_physarum_stopping_rule.py --part2 --levels heavy,light --scenarios S1,S2,S3,S4,S5,S6
.venv/Scripts/python.exe scripts/measure_physarum_stopping_rule.py --report-only
```

Raw per-cell records: `data/results/physarum_stopping_rule_part1.jsonl` (60 rows),
`data/results/physarum_stopping_rule_part2.jsonl` (718 rows).
`src/bioroute/algorithms/physarum.py` is unmodified throughout
(`git diff --stat` on it is empty).
