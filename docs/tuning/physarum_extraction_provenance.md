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
