# Phase 9a working notes

Checkpointed as the work proceeded, so nothing lives only in context.
Source of truth: `data/results/final_14a535c02b83.parquet` (read-only).

## Self-check against the facts handed down with the spec (all REPRODUCED)

| Fact | Spec said | Measured |
|---|---|---|
| shape | 5,520 x 38 | 5,520 x 38 |
| unique `run_id` | 5,520, 0 dup | 5,520, 0 dup |
| per-algorithm | aco 3900 / physarum 780 / dijkstra 420 / astar 420 | identical |
| OD pairs / cells | 30 / 780 | 30 / 776 non-failed cells (780 incl. the 2 all-failed S4 cells) |
| failed | 28 (0.51%), one reason | 28, one reason (S4 infeasible), all S4 |
| stretch >= 1.0 | all four columns | 0 violations in all four |

Nothing contradicted. One clarification: 780 is the DESIGNED cell count;
after dropping `failed=True` rows, 776 distinct cells survive, because the
2 infeasible S4 cells lose every row they had (2 cells x 14 rows = 28).

## Structural facts that drove methodological choices

- Classical routers and S0 are COLD-ONLY by design, so warm/cold pairing
  exists only for aco (1,790 pairs) and physarum (358 pairs) on S1-S6.
- `stretch` (the pre-disruption solve) is IDENTICAL across all 7 scenarios
  and both warm/cold for a given (algorithm, od, traffic, seed): verified
  300/300 ACO groups and 60/60 Physarum groups have exactly 1 distinct
  value. So the pre-disruption quality table MUST deduplicate on
  (algorithm, od_source, od_target, traffic_level, seed) -- pooling all
  rows would count one initial solve up to 13 times and shrink every CI
  by a factor that is pure bookkeeping.
- `walk_best_stretch` is NULL when the max-flux walk produced no path at
  all (542 of 776 successful Physarum rows are non-null). So the
  model-only series has TWO denominators and both are reported:
  shares are over ALL rows (a no-path walk is not an exact match),
  median/IQR/CI are over rows where the walk produced a path.

## Reproduced headline statistics (before writing any code that reports them)

- Physarum warm vs cold, paired on (od, scenario, traffic, seed), n=358:
  median ratio cold/warm 2.968x iterations (warm faster 350/358,
  Wilcoxon p=3.22e-59) and 6.871x wall-clock (336/358, p=9.04e-52).
  Warm `recovery_iterations` == 1 on 48.3%; cold's MINIMUM is 73.
  Per-iteration cost 0.0511 s warm vs 0.0538 s cold -- the gain is fewer
  iterations, not cheaper ones.
- ACO warm vs cold (not in the spec's fact list, measured here): n=1,790,
  median ratio cold/warm 1.258x iterations (p=1.5e-184) and 1.558x
  wall-clock. Medians 69 warm vs 110 cold, matching the spec's note.
- ACO congestion effect (SUMO only): S0 within-10% light 81.3% vs heavy
  58.0%; paired Wilcoxon on `stretch` at (od, seed), n=97 nonzero
  differences, p=0.03576 -- this is the test behind Phase 8e's p=3.6e-02.
  The binary within-10% indicator flips light-only 37 vs heavy-only 2.
- Physarum provenance over 776 successful rows: full_graph_fallback 315
  (40.6%), max_flux_walk 232 (29.9%), theta_subgraph_fallback 229 (29.5%).
- Physarum S0 pooled exact-match 90.0% heavy / 86.7% light vs walk-only
  23.3% / 16.7%.
- `post_disruption_walk_stretch` median 1.062 warm (n=130) vs 1.199 cold
  (n=213).

## A CONTRADICTION FOUND (reportable)

Phase 8e's RESULT 4 -- "warm start improves the model's OWN extraction
QUALITY: `post_disruption_walk_stretch` median 1.062 warm vs 1.199 cold
(n=130/213)" -- DOES NOT SURVIVE PAIRING, and the sign reverses.

That comparison is unpaired across two differently-composed samples:

  share of Physarum recoveries where the walk produced a path at all:
    warm 36.3% (130/358)   cold 59.5% (213/358)

So the warm side's 130 values are a SUBSET of easier cells. Paired on
identical (od, scenario, traffic, seed):

  n=126 cells where BOTH sides produced a walk path
    warm median 1.0587 | cold median 1.0127
    cold better on 44, warm better on 12, 70 ties, Wilcoxon p=2.7e-04
  87 cells where ONLY cold produced a path -> their cold median is 1.3713
  4 cells where ONLY warm did                -> warm median 1.3952

The 87 cold-only cells are hard ones and drag cold's UNPAIRED median up
to 1.199; on the cells both can do, cold's walk is slightly BETTER.

Honest statement: warm start makes the max-flux walk LESS likely to
complete a path at all (36.3% vs 59.5%), and on the cells where both
complete, cold's walk is marginally better. Phase 8e's Result 4 is a
composition artifact. Warm start's real, robust win is SPEED (paired 2.97x
iterations / 6.87x wall-clock) -- not extraction quality.

`warm_cold_table` now carries `n_warm_only` / `n_cold_only` so the
coverage gap is visible in the table next to the medians, and
`median_warm` / `median_cold` there are PAIRED medians (over the common
cells), deliberately not the unpaired ones.

## Other numbers worth keeping

- Provenance exact-match shares differ sharply even though the median
  stretch is 1.000 in all three classes:
  full_graph_fallback 95.2% (n=315) | theta_subgraph_fallback 94.3%
  (n=229) | max_flux_walk 74.6% (n=232). The medians are identical; the
  exact-match shares are not. Another reason pooling erases the effect.
- ACO post-disruption stretch is paired-WORSE cold than warm but only
  barely: median 1.0193 warm vs 1.0240 cold, 490 warm-better vs 347
  cold-better, 953 ties, p=7.1e-05. Warm start helps ACO on quality AND
  speed (1.258x iterations, 1.558x wall-clock, n=1,790).
- ACO median s/iteration is 0.43 warm and 0.54 cold; Physarum 0.051 warm
  vs 0.054 cold. Physarum's per-iteration cost is flat warm-vs-cold, which
  is the proof the gain is fewer iterations.

## Deliverables (Phase 9a, complete)

- `src/bioroute/viz/stats.py` -- load/validate, median_iqr,
  bootstrap_median_ci (two-stage clustered), paired_frame,
  paired_wilcoxon, quality_table, recovery_table, warm_cold_table,
  provenance_table, scenario_table, congestion_effect_table,
  failure_table, headline_numbers, compute_all.
- `src/bioroute/viz/figures.py` -- fig02 / fig03 / fig04 / fig07 plus a
  `FIGURES` registry and `make_figures()`.
- `scripts/04_make_figures.py` -- `--parquet --outdir --format --only
  --n-boot --no-tables`. Writes the four figures (png+svg),
  `docs/results/summary.md` and `data/results/stats_14a535c02b83.json`.
- `tests/test_stats.py` -- 23 tests, all passing.

Full regeneration (10,000 bootstrap resamples) takes ~25 s.

## OPEN ITEM for the owner

`.gitignore` ignores `data/results/*` with an exception only for
`final_*.parquet`, so `data/results/stats_14a535c02b83.json` is NOT
tracked. It regenerates in ~7 s, so this may be intended; if the dashboard
(Phase 9b) should consume a committed bundle, add
`!data/results/stats_*.json`. Not changed here -- `.gitignore` is not a
file Phase 9a owns.

## Figure-level decisions the spec left open

1. fig02 is split into warm and cold PANELS instead of pooling. Pooling
   would average Physarum's 1-iteration warm recoveries with its
   73-iteration cold floor into a box describing neither.
2. fig02/fig03/fig04 all use a log x or y where the series span two orders
   of magnitude; each axis says "log scale" in its label.
3. fig04 uses TWO panels, iterations and wall-clock, rather than choosing
   one speed axis. CLAUDE.md's honest-reporting note requires showing that
   Dijkstra wins wall-clock by ~3 orders of magnitude, and a dual y-axis
   was ruled out.
4. fig04 draws warm as a filled circle and cold as a hollow square, so
   colour carries algorithm identity alone. Dijkstra and A* coincide
   exactly at (1 iteration, 1.000) and are annotated rather than nudged
   apart.
5. `quality_table` emits a `traffic = all` row in addition to light/heavy.
   Legitimate for stretch (each value is normalised by Dijkstra on its own
   level) and explicitly not for costs.
6. `scenario_table` covers S1-S6 only; S0 has no post-disruption
   measurement.
7. Exclusion counts are PER GROUP, not a single table-wide 28.

## Post-review layout fixes (coordinator round 2)

Layout only. `data/results/stats_14a535c02b83.json` is BYTE-IDENTICAL
before and after (sha256 5dcd936a2eb714...): no number moved.

1. fig04 -- the caption's lines overlapped each other and a dead band sat
   between the axes and the legend. Cause: matplotlib's `wrap=True`
   re-wraps text against the figure box but keeps single-line spacing, and
   the legend/caption were placed at NEGATIVE figure coordinates, so
   `bbox_inches="tight"` pulled in whatever gap that left. Fix: a new
   `_caption()` helper wraps with `textwrap.fill` and sets
   `linespacing=1.55`, and fig04 now uses explicit `subplots_adjust`
   margins (top 0.875 / bottom 0.335) with the legend and caption in their
   own bands INSIDE the figure. Height 4.8 -> 5.9 in; the plot area gains
   the space the dead band used to take.
2. fig07 panel (c) -- the "6.87x" annotation sat on the Physarum
   wall-clock whisker. Cause: a fixed annotation x of 160 while the
   per-pair ratios reach 1,230x, so no fixed x inside the data range is
   safe. Fix: annotations moved into the empty gap ABOVE each box at the
   left margin (y - 0.34), xlim now derives from the data
   (`ratio_max * 2.2`, ticks 1/10/100/1000) and the y-axis gains half a
   slot of headroom so the first annotation is not clipped.
3. The same `_caption()` helper was applied to fig02, fig03 and fig07 for
   consistency -- the overlap was a latent defect in all four, not a
   fig04-specific one; only fig04 had enough caption lines to show it.

## Table 5 dagger marker (coordinator round 3)

Added at the coordinator's request. Rendering only -- the stats JSON is
still byte-identical (sha256 5dcd936a2eb714...).

Table 5's model-only rows now read `Physarum (model only) +DAGGER+`, and
the note immediately under the table opens with "+DAGGER+ shares the
solve with the row above -- identical recovery columns are by
construction." (the literal character is U+2020).

Why the marker rather than prose alone: a reviewer scans the table before
reading the note, and six pairs of duplicated recovery numbers form the
copy-paste suspicion first; a marker inside the row they are looking at
prevents the misreading instead of correcting it afterwards. The earlier
free-standing paragraph saying the same thing was REMOVED, so the
explanation now appears exactly once.

Blanking the duplicated cells with `--` was considered and rejected: `--`
already means "null" everywhere else in summary.md, and a second meaning
for one glyph would be worse than the problem it solves.
