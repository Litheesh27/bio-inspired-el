# bioroute

**Self-healing routing on Bengaluru's road network: ACO and Physarum vs.
classical shortest-path algorithms.**

A three-way benchmark of Dijkstra/A\*, Ant Colony Optimization (MAX–MIN Ant
System), and a *Physarum polycephalum* tube-reinforcement model, routing on
the real Central Bengaluru road network under SUMO-simulated congestion. It
measures both routing quality and recovery speed after simulated road
closures.

The contribution is the unified benchmark on a new dataset, not a new
algorithm. Existing work compares these methods on synthetic graphs, the
Tokyo rail network, or physical mazes; none combines all three on a real
Indian city's organically-grown topology under simulator-generated dynamic
congestion.

| | |
|---|---|
| Study area | Central Bengaluru, ~4×4 km — 2,856 nodes / 7,015 directed edges |
| Congestion | SUMO 1.27.1, light and heavy demand, on a shared free-flow baseline |
| Experiment | 30 OD pairs × 7 scenarios × 2 traffic levels × {warm, cold} = **5,520 runs** |
| Failure rate | 0.51% (28 runs), all one cause, all recorded rather than dropped |

---

## Results

**[Interactive dashboard →](https://claude.ai/artifact/2ZPysszxaGhKW2a5712vbH)**
(private; source in `web/`)

Full tables with confidence intervals: **[`docs/results/summary.md`](docs/results/summary.md)**

### Headline findings

**Warm start's benefit is fewer iterations, not cheaper ones.** Retaining the
conductivity field across a closure lets Physarum recover in **2.97× fewer
iterations** and 6.87× less wall-clock (paired on identical cells, n=358,
p<1e-51). The per-iteration cost is identical warm vs cold (0.051 s vs
0.054 s), which is what proves the saving is adaptation rather than
arithmetic. The distribution is bimodal: **48.3% of warm recoveries converge
in exactly one iteration**, which cold start never does — its minimum is 73.

**Physarum's apparent competence is substantially its fallback's.** The model
extracts a path by walking the maximum-flux tube; when that walk dead-ends it
falls back, and one of those fallbacks is plain Dijkstra. Pooled, Physarum
matches the optimum on 90% of undisrupted pairs. Using only its own
extraction: **23.3%**. Median stretch is exactly 1.000 inside *every*
provenance class, so a pooled number cannot separate "the model found the
optimum" from "the fallback returned Dijkstra's answer". Results here are
therefore always reported as two labelled series and never pooled.

**The sign of the congestion effect belongs to the congestion model, not to
the algorithm.** Under analytic BPR, congestion *helps* ACO. Under SUMO it
*hurts* it (within-10%: 81.3% light vs 58.0% heavy, paired p=0.036). Any
claim about behaviour "under congestion" has to name which model produced it.

**Network fragility is super-additive.** The share of candidate closures
rejected for disconnecting the OD pair rises 2.6% → 14.5% → 31.7% for 1, 3
and 5 simultaneous path closures — far faster than linearly. A whole closed
corridor (7.4%) is *safer* than five scattered edges, because one detour
routes around all of it.

**Dijkstra wins on wall-clock, and that is reported.** It recomputes an exact
path in one iteration and ~31 ms. The bio-inspired methods take seconds. The
defensible metrics are iterations-to-adapt, time-to-first-valid-path, and
post-disruption path quality — the speed-vs-quality trade this project exists
to quantify.

### Figures

All in `figures/`, as 300 dpi PNG and editable-text SVG. Regenerate every one
with `scripts/04_make_figures.py` (figures 2, 3, 4, 7) and
`scripts/06_make_traces.py` (figures 1, 5, 6).

| Figure | Shows |
|---|---|
| `fig01_convergence` | Cost vs iteration across three distance strata |
| `fig02_recovery_by_scenario` | Recovery iterations by closure severity, warm vs cold |
| `fig03_stretch_pre_post` | Path quality before and after closure |
| `fig04_pareto_recovery_vs_quality` | Recovery speed vs post-disruption quality |
| `fig05_routes_before_after` | The three routes on real street geometry |
| `fig06_congestion_heatmap` | Per-edge congestion, light vs heavy |
| `fig07_warm_vs_cold` | The bimodality, and paired warm/cold deltas |

**Two kinds of figure, deliberately distinguished.** Figures 2, 3, 4 and 7 are
computed from the committed results file. Figures 1, 5 and 6 are *regenerated*
— the results file stores path lengths but not the paths, and no per-iteration
traces — so they are re-run under the same config, graph, snapshots and seeds.
That regeneration is cross-checked against the results file and reproduces
64/64 comparable values exactly. Every caption says which kind it is.

### The evidence file

`data/results/final_14a535c02b83.parquet` — 5,520 rows × 38 columns, committed
to git. Every figure and every number derives from it and nothing else. Each
row carries its algorithm, OD pair, scenario, traffic level, start mode, seed,
config hash and timestamp, so any result is reconstructible.

---

## Reproducing

### Setup

1. Create the Python 3.10 virtual environment (3.10 specifically —
   `osmnx`/`geopandas`/`shapely` wheels are unreliable on newer versions):

   ```
   py -3.10 -m venv .venv
   ```

2. Install dependencies and the package itself (editable):

   ```
   .venv/Scripts/python.exe -m pip install -r requirements.txt
   .venv/Scripts/python.exe -m pip install -e .
   ```

3. Install [Eclipse SUMO](https://www.eclipse.org/sumo/) for Windows (manual —
   the installer needs admin rights and a GUI). Set `SUMO_HOME` to the install
   directory (this project used `C:\Sumo`) and restart your terminal.
   `sumolib`/`traci` are not pip packages; they ship with SUMO and are imported
   by adding `$SUMO_HOME/tools` to `sys.path`.

4. Run the setup check:

   ```
   .venv/Scripts/python.exe scripts/00_setup_check.py
   ```

   Prints PASS/FAIL for every requirement — Python version, each dependency,
   the `bioroute` package, `SUMO_HOME`, the `netconvert`/`sumo` binaries,
   `sumolib`, and writable data directories — and exits non-zero if anything
   is missing.

### Pipeline

```
.venv/Scripts/python.exe scripts/01_build_network.py     # OSM -> cached graph
.venv/Scripts/python.exe scripts/02_run_sumo.py          # SUMO run + edge-ID join
.venv/Scripts/python.exe -m pytest tests/ -v             # correctness
.venv/Scripts/python.exe scripts/03_run_experiments.py --config configs/experiments/disruption.yaml --workers 12 --resume
.venv/Scripts/python.exe scripts/04_make_figures.py      # figures 2,3,4,7 + tables
.venv/Scripts/python.exe scripts/06_make_traces.py       # figures 1,5,6
.venv/Scripts/python.exe scripts/05_export_web.py        # dashboard bundle
```

The full sweep takes about 10 hours at 14 workers. It is **resumable**:
`--resume` skips every run already on disk, so it can be stopped with Ctrl+C
and restarted at any worker count without losing completed work.

One test fails on purpose. `test_aco_exit_criterion_30_od_pairs_no_heuristic`
asserts ACO reaches stretch ≤1.10 on ≥90% of pairs; it reaches 80%. The
assertion is a standing marker for a genuinely missed exit criterion (see
`docs/tuning/aco_sweep.md`) and must not be weakened.

### Viewing the dashboard locally

`web/index.html` fetches `web/data/bundle.json`, which browsers block over
`file://`. Serve it instead:

```
cd web
..\.venv\Scripts\python.exe -m http.server 8000
```

then open <http://localhost:8000>.

---

## Layout

```
bio_el/
├── CLAUDE.md                  # phase log, locked decisions, invariants — read this first
├── configs/                   # area, congestion, SUMO, ACO, Physarum, experiments
├── src/bioroute/
│   ├── network/               # OSM acquisition, graph cache, SUMO<->OSMnx edge join
│   ├── congestion/            # CongestionProvider ABC, BPR fallback, SUMO, snapshots
│   ├── algorithms/            # Router ABC, Dijkstra/A*, ACO (MMAS), Physarum
│   ├── disruption/            # scenarios S0-S6, non-mutating application
│   ├── metrics/               # result schema, collectors, Parquet sharding
│   ├── experiments/           # matrix construction, parallel resumable runner
│   └── viz/                   # stats, figures, maps, traces, dashboard export
├── scripts/00_setup_check.py … 06_make_traces.py
├── web/                       # dashboard page + data bundle
├── figures/                   # publication-quality PNG + SVG
├── docs/
│   ├── PLAN.md                # the approved phase plan
│   ├── results/summary.md     # all result tables with CIs
│   └── tuning/                # hyperparameter sweeps and calibration records
├── data/results/final_*.parquet   # the experimental evidence (tracked)
└── tests/
```

`CLAUDE.md` carries the detailed reasoning behind every design decision,
including three bugs found during development that would each have silently
corrupted published results: a dimensional error in SUMO travel-time
aggregation, a singular Laplacian that made Physarum report Dijkstra's paths
as its own, and the extraction-provenance problem above.

## Limitations

Single city and single bounding box. Synthetic SUMO demand rather than
calibrated traffic counts. Physarum is run per-OD-pair rather than as a
multi-commodity flow. Absolute travel times are optimistic for Bengaluru —
the shared baseline re-annotates free-flow speeds from the generated network,
so ratios are comparable but absolute seconds are not realistic.
