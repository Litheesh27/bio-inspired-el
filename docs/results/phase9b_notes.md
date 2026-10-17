# Phase 9b notes — regenerated artifacts for figures 1, 5, 6

These three figures cannot come from `data/results/final_14a535c02b83.parquet`:
the Parquet is one flat row per run, with no per-iteration trace, no node
lists, and nothing about the snapshot itself. Phase 9b therefore RE-RUNS a
small seeded subset under the sweep's own config / graph / snapshots and plots
that. **Every artifact and caption here is illustrative, derived from a
re-run — it is not the sweep's measured evidence.**

## Reproduction context (verified)

- config: `configs/experiments/disruption.yaml`, `load_experiment_config` +
  `with_area_hash(cfg, config_hash(area_cfg))` -> config_hash **14a535c02b83**
  (matches the Parquet). Without `with_area_hash` the hash is `078ba9094218`,
  which is a silent trap worth recording.
- graph: `load_sweep_graph(cfg, area_cfg)` ->
  `data/network/central_blr_9caede2d05c4_285f18f9a225_sumospeeds.graphml`,
  baseline `sumo_speeds`, baseline_hash **285f18f9a225**.
- snapshots: `prepare_resources(...)`, conditions
  `{sumo, light, 0}` and `{sumo, heavy, 0}`.
- OD pairs: `sample_od_pairs(G, cfg.od)` — 30 pairs, seed 8080, 10/10/10.
- scenario seed 1337; budgets aco 150 / physarum 300 / classical 1.

## OD-pair choices

Figure 1 (S0, SUMO heavy), one pair per stratum, chosen for ACO seed spread
measured in the Parquet (std of `stretch` over 5 seeds):

| stratum | pair | straight-line | Dijkstra hops | ACO stretch min/med/max | Physarum provenance |
|---|---|---|---|---|---|
| short  | 254665681 -> 10019824654   | 1233 m | 28 | 1.000 / 1.000 / 1.225 | max_flux_walk |
| medium | 11387476249 -> 11915994482 | 2532 m | 50 | 1.020 / 1.123 / 1.260 | theta_subgraph_fallback |
| long   | 13247265224 -> 459793811   | 3512 m | 79 | 1.118 / 1.265 / 1.459 | full_graph_fallback |

Chosen because all three show real seed spread (Phase 4d's point) AND they
happen to cover all three Physarum provenance classes, which the figure can
then label honestly.

Figure 5: pair **429415654 -> 10038577016**, scenario **S4**, SUMO heavy,
warm start. From the Parquet this is the one cell where all three algorithms
genuinely disagree after the closure:
Dijkstra/A* post_disruption_stretch 1.000 (42 hops), ACO 1.4218 (22 hops,
identical across all 5 seeds), Physarum 1.6127 — and Physarum's path is
attributed to **`max_flux_walk`**, i.e. the model's own extraction, so the map
is not showing Dijkstra's answer relabelled. 8 edges removed (5 path edges,
both directions + parallel edges).
Note: it is a SHORT-stratum pair (873 m apart) that still needs 42 hops —
the organic topology, worth saying in the caption.

## What was built

- `src/bioroute/viz/traces.py` — regeneration driver (`load_context`,
  `collect_convergence[_set]`, `collect_routes`, `congestion_ratios`,
  `congestion_bundle`), the JSON artifact IO, the Parquet cross-check, and
  figure 1's plotter.
- `src/bioroute/viz/maps.py` — figures 5 and 6 plus the geometry layer
  (`edge_points`, `path_points`, `draw_basemap/removed/route/endpoints`,
  `zoom_to`, `_set_geo_aspect`).
- `scripts/06_make_traces.py` — CLI. `--figures 1,5,6`, `--from-artifacts`
  (re-render with zero recomputation), `--no-render`. Exits 1 on any
  cross-check mismatch.
- `tests/test_traces.py` — 7 tests, toy graph only, no real-network data.

## Cross-check against `final_14a535c02b83.parquet`

**64/64 values matched to 1e-6, zero mismatches.**
- figure 1: 48 values (3 pairs x 4 algorithms x config seeds, `path_cost`
  and `stretch` each).
- figure 5: 16 values (4 algorithms x `path_cost`, `stretch`,
  `post_disruption_cost`, `post_disruption_stretch`).
Wall-clock is deliberately NOT compared — CLAUDE.md Phase 4b records that
this machine's throughput swings up to 3.4x with thermal state.

Re-run timings (not performance figures): fig 1 160.8 s, fig 5 12.5 s,
fig 6 6.4 s; ~3 min of compute in total plus ~6 s of snapshot pre-warm per
invocation.

## Artifacts written (all gitignored)

| file | size |
|---|---|
| `data/results/traces/traces_14a535c02b83.json` | 97.0 KB |
| `data/results/traces/routes_14a535c02b83.json` | 6.8 KB |
| `data/results/traces/congestion_14a535c02b83.json` | 444.2 KB |

Total ~548 KB, ~3% of the dashboard's 16 MB budget. Convergence series are
downsampled to **at most 150 points**, not the plan's ~100: the ACO budget
is exactly 150, so at this ceiling every ACO trace is carried at FULL
resolution and the downsampler is a no-op for them. Only a Physarum run
that burns its 300-iteration budget is thinned. The downsampler always
keeps the first and last point, so a thinned curve still agrees with the
Parquet at the two values a reader checks.

## Finding: the "61% of SUMO edges see no vehicle" figure does NOT transfer

Figure 6 measures per-GRAPH-edge ratios. Only **24.2% (light) / 12.1%
(heavy)** of the 7,015 graph edges sit at exactly 1.0, and those counts
(1,616 / 812) match the snapshot's own `n_fallback_edges` EXACTLY — i.e.
the ratio-1.0 edges are precisely the edges with no traveltime observation
at all. The snapshot meta's `frac_sumo_edges_zero_traffic` is 0.682 /
0.611, confirming CLAUDE.md's 61% as a per-SUMO-edge statistic.
The two are not interchangeable: netconvert splits ways while OSMnx merges
them, so one graph edge aggregates several SUMO segments and shows a
non-unit ratio if ANY of them carried traffic. The caption states both
numbers and the distinction. **A caption that had simply repeated "61%
unaffected" would have been wrong by 5x on this figure.**

Ratio distribution (pooled, for the colour scale): median 1.30/1.36,
p90 2.08/2.35, p99 6.0/16.6, max 20/59. Scale therefore clipped at the
pooled p95 = 3.0x (declared in the colourbar label, marked by its extend
arrow, and the share above the clip is printed per panel: 4.0% / 5.9%).
A p99.5 clip would have pushed ~95% of the network into the bottom sixth
of the ramp and the map would have read as "no congestion anywhere".

## Decisions the spec left open

1. **Downsample ceiling 150, not ~100** — see above.
2. **Figure 1 x-axis is log-scaled.** Almost all improvement happens in
   the first ~30 iterations; on a linear axis the three panels are a
   vertical drop followed by 120 iterations of flat line.
3. **Figure 1 y-ranges differ per panel** (short reaches 5.7x, long 3.6x).
   Sharing y would flatten the medium and long panels. All three share the
   same floor of 1.0 and the caption says the ranges differ.
4. **No separate "optimum" line in figure 1.** Dijkstra IS the
   denominator, so its level is the optimum; a third line at 1.0 was the
   same line drawn three times. A* is dashed on top of Dijkstra, whose
   path it traces exactly — otherwise A* paints over it and Dijkstra
   vanishes from a figure that lists it in the legend.
5. **Figure 5 applies a declared ~2-point display-space offset per
   series.** Four routes share long corridors; in an earlier draft ACO's
   route appeared to STOP HALFWAY because Physarum's ran over its tail,
   which reads as a failed solve. The offset is in points, never in
   lon/lat, so the equal-area aspect and the underlying coordinates are
   untouched. It is noted in the module docstring.
6. **Latitude-corrected aspect, not `set_aspect("equal")`.** One degree of
   longitude is cos(lat) of one degree of latitude; equal aspect on raw
   degrees would stretch the map east-west by ~3%.
7. **Figure 5 uses one ACO seed (0).** All five seeds produce the
   identical path in this cell (verified in the Parquet), so five lines
   would be five copies of one. Figure 1 shows all five, where they differ.
8. **Physarum provenance is in the panel title, not the legend.** One
   Physarum run per panel means one provenance per panel, and
   method-level legend entries printed the with-fallback swatch twice.
9. **`congestion_ratios` skips edges missing from the snapshot** rather
   than imputing 1.0. An imputed 1.0 would draw a coverage gap as a
   confidently uncongested road. Pinned by a test.
10. **`_disruption_config` is imported from `experiments/runner.py`**
    (a private) rather than reconstructed, so the closure is drawn under
    byte-identical settings to the sweep's. A reimplementation that
    drifted would illustrate a cell that does not exist.
