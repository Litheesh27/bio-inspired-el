<!-- Snapshot of the approved implementation plan.
     Source of truth for all phase specs and exit criteria.
     Originally at ~/.claude/plans/ (local-only); copied here so it is
     version-controlled and survives machine loss. -->

# Self-Healing Routing on Bengaluru's Road Network

## Context

**The research question.** On a real, organically-grown Indian city road network under realistic simulated traffic, how much *recovery speed* do bio-inspired routing methods actually buy you after a road closure — and what is the *path-quality cost* of that speed?

**The contribution** is a unified three-way benchmark (Dijkstra/A\* vs. Ant Colony Optimization vs. Physarum polycephalum tube reinforcement) on a genuinely new dataset, not a new algorithm. Existing literature has Physarum-vs-Dijkstra resilience work, Physarum-vs-ACO-vs-Dijkstra static comparisons, and maze/lab disruption experiments — but always on synthetic graphs, the Tokyo rail network, or physical mazes, and never combined into one consistent benchmark under dynamic simulator-generated congestion on an Indian city.

**Current state.** `C:\Users\lithe\OneDrive\Desktop\bio_el` is an empty git repo on `main` with zero commits. Python 3.10.11 (default) and 3.14.0 are installed; only `numpy`, `scipy`, `matplotlib` are present. `osmnx`, `networkx` and SUMO are **not** installed.

**Decisions locked in with the user:**
| Decision | Choice |
|---|---|
| Congestion source | SUMO primary, behind a `CongestionProvider` interface, with an analytic BPR fallback so downstream phases never block |
| Study area | Central Bengaluru ~4×4 km (MG Road / Cubbon Park / Shivajinagar / Richmond Town) — irregular organic topology |
| Recovery protocol | Warm-start (retained pheromone/conductivity) as the main experiment, **plus** a cold-start control |
| Primary deliverables | **Results visualization (web dashboard) + figures + benchmark suite.** The research paper is a later, separate effort — deferred out of this plan, but every phase below is built so the paper can be written from its outputs without rework. |

**A methodological caveat to bake into the measurement design now.** Dijkstra will win on raw wall-clock recovery — a recompute on a 5k-node graph is milliseconds, while ACO and Physarum take seconds. Any framing that hides this is dishonest and a reviewer will catch it later. The defensible findings live in *iterations-to-adapt*, *time-to-first-valid-path*, *post-disruption path quality*, and *degradation behaviour under repeated disruption*. Phase 6 encodes this by measuring first-valid-path separately from full re-convergence, which is precisely the speed-vs-quality trade the project set out to quantify.

---

## Target repository layout

```
bio_el/
├── CLAUDE.md                  # Phase 0 — persistent project instructions
├── README.md, requirements.txt, pyproject.toml, .gitignore
├── configs/
│   ├── area_central_blr.yaml       # bbox, network filters
│   ├── algorithms.yaml             # ACO / Physarum hyperparameters
│   └── experiments/{baseline,disruption}.yaml
├── src/bioroute/
│   ├── config.py                   # YAML -> dataclasses, seed management
│   ├── network/{acquire,graph,ids}.py
│   ├── congestion/{base,sumo,bpr,snapshot}.py
│   ├── algorithms/{base,classical,aco,physarum}.py
│   ├── disruption/{scenarios,apply}.py
│   ├── metrics/{schema,collectors}.py
│   ├── experiments/{matrix,runner}.py
│   └── viz/{figures,maps,export}.py
├── scripts/00_setup_check.py … 05_export_web.py
├── web/                       # results dashboard (published as an Artifact)
├── figures/                   # publication-quality PNG/SVG output
├── data/{raw,network,sumo,results}/    # raw+sumo gitignored, network cached
└── tests/
```

---

## Phase 0 — Environment, scaffold, and CLAUDE.md

**Goal:** a reproducible environment and the project's standing instructions.

1. Create a **Python 3.10 venv** (`py -3.10 -m venv .venv`). Do *not* use 3.14 — `osmnx`/`geopandas`/`shapely` wheel availability is unreliable there.
2. `requirements.txt`: `osmnx>=2.0`, `networkx`, `numpy`, `scipy`, `pandas`, `pyarrow`, `matplotlib`, `pyyaml`, `joblib`, `tqdm`, `pytest`, `requests`. SUMO's `sumolib`/`traci` come from the SUMO install, added via `SUMO_HOME/tools` on `sys.path`.
3. Install SUMO for Windows (Eclipse SUMO installer), set `SUMO_HOME`. Verify `netconvert --version` and `sumo --version`.
4. `scripts/00_setup_check.py` — prints versions, SUMO presence, writable data dirs. Exits non-zero with a clear message on failure. This is the script to re-run whenever anything looks broken.
5. `.gitignore`: `.venv/`, `data/raw/`, `data/sumo/`, `data/results/*.parquet`, `__pycache__/`.
6. **Write `CLAUDE.md`** containing: the one-paragraph research question; the phase table with checkboxes; the locked decisions above; the repo layout; key invariants (*never compare stretch across differently-weighted graphs*; *every run carries a seed*; *cached artifacts are keyed by config hash*); and the commands to rebuild the network, run SUMO, run experiments, and regenerate figures.

**Exit criteria:** `python scripts/00_setup_check.py` passes, including SUMO. First commit.

---

## Phase 1 — Road network acquisition

**Goal:** one canonical, deterministic graph of the study area, cached on disk.

- `network/acquire.py`: fetch the bbox **twice, from the same Overpass snapshot** —
  (a) raw OSM XML → `data/raw/central_blr.osm.xml` (this is what `netconvert` consumes),
  (b) an OSMnx `MultiDiGraph` via `ox.graph_from_bbox(..., network_type="drive", simplify=True)`.
  Both must come from one fetch so way IDs line up; save the Overpass timestamp into the cache metadata.
- `network/graph.py`:
  - take the **largest strongly connected component** (essential — Dijkstra/ACO/Physarum all assume reachability),
  - `ox.add_edge_speeds` / `ox.add_edge_travel_times` for free-flow `travel_time`,
  - normalize `osmid` to always be a `list[int]` (OSMnx gives an int for unmerged edges, a list for simplified ones),
  - persist as GraphML keyed by a config hash; reload if present.
- Print a network report: node count, edge count, total km, degree distribution, mean edge length.

**Exit criteria:** cached graph in the **2,000–8,000 node** range. This bound is what keeps Phase 5's per-iteration sparse Laplacian solve tractable. If the bbox yields more, shrink it before continuing — do not proceed and hope.

---

## Phase 2 — Congestion layer (SUMO + fallback)

**Goal:** `TrafficSnapshot` objects mapping every graph edge `(u,v,k)` → congested travel time, for a light and a heavy demand level.

**2a. The interface first.** `congestion/base.py` defines `CongestionProvider.snapshot(level, time_bin) -> TrafficSnapshot`. Build `congestion/bpr.py` (analytic BPR volume-delay with synthetic OD demand, incremental all-or-nothing assignment) **before** the SUMO work. It is ~100 lines and unblocks Phases 3–9 immediately.

**2b. SUMO pipeline** (`congestion/sumo.py`, driven by `scripts/02_run_sumo.py`):
1. `netconvert --osm-files data/raw/central_blr.osm.xml -o data/sumo/net.net.xml --roundabouts.guess --ramps.guess --junctions.join --tls.guess-signals`.
   **Deliberately omit `--geometry.remove`** — it merges edges and destroys the way-ID traceability Phase 2c depends on.
2. `randomTrips.py` at two `--period` values → light and heavy demand; `duarouter` → routes.
3. Run headless `sumo` with an `<edgeData>` additional file at 300 s intervals → `data/sumo/edgedata_{level}.xml`, giving per-edge `traveltime` and `density` per bin.

**2c. The critical join** (`network/ids.py`) — the single highest-risk piece of this project:
- SUMO edge IDs derive from OSM way IDs: `123456`, `-123456` (reverse direction), `123456#2` (split segment). Strip the sign and `#n` suffix via regex to recover the way ID.
- Build `way_id → [sumo_edge_ids]` and `way_id → [(u,v,k)]` (using the normalized `osmid` lists from Phase 1), then invert to `(u,v,k) → [sumo_edge_ids]`.
- Edge travel time = **length-weighted mean** of matched SUMO edges, using the ID sign to respect direction where available.
- Unmatched edges fall back to free-flow `travel_time` and are counted.

**Exit criteria:** a coverage report showing **≥85 % of graph edges matched** to SUMO edges, and a side-by-side map of free-flow vs. congested times that looks plausible (arterials slow, side streets don't). Below 85 %, add geometric matching (edge-midpoint nearest-neighbour + bearing agreement within 30°) before moving on. If SUMO is still not delivering, continue on BPR and revisit — the interface makes this cheap.

---

## Phase 3 — Classical baseline

**Goal:** ground truth, and the `Router` contract every algorithm implements.

- `algorithms/base.py` — `Router` ABC: `solve(graph, source, target, weights) -> Result`, `apply_disruption(removed_edges)`, `step()` for iterative methods, plus a serializable `state` so warm-start is explicit rather than incidental.
- `algorithms/classical.py` — Dijkstra (`networkx.shortest_path` with the congested weight) and A\* with a **great-circle / max-speed admissible heuristic**. Assert A\* cost equals Dijkstra cost on every OD pair; a mismatch means the heuristic is inadmissible.
- Dijkstra recompute is also the baseline recovery mechanism (it has no other mode).

**Exit criteria:** unit tests on a hand-built toy graph with known optimal paths; A\*-vs-Dijkstra cost equality across 100 random OD pairs on the real network.

---

## Phase 4 — Ant Colony Optimization

**Goal:** an ACO that converges reliably on a 5k-node irregular graph — the part most likely to need tuning.

- **MAX–MIN Ant System** (τ bounds), not vanilla AS. On a graph this size vanilla AS stagnates or never converges; the τ_min/τ_max bounds are what make dynamic re-adaptation possible at all.
- Transition rule `p ∝ τ^α · η^β` with `η = 1/travel_time`; `α=1`, `β` swept over {2,3,5}, `ρ=0.1`, `m=30` ants.
- **Robustness details that decide whether this works:** per-ant tabu set; `max_steps = 3 × graph diameter`; ants that dead-end or exceed the budget are discarded, not repaired; if an entire iteration fails, log it and keep going.
- Deposit on the iteration-best path, `Δτ = Q / L_best`.
- Convergence: iteration-best path unchanged for `k=20` iterations **and** pheromone branching factor below threshold.
- **Disruption response, two configurable strategies** — (a) do nothing beyond zeroing the severed edges, (b) local pheromone reset in an n-hop neighbourhood of the break. Comparing these two is itself a reportable result.

**Exit criteria:** on ≥90 % of a 30-OD-pair sample, ACO reaches stretch ≤ 1.10 within the iteration budget. Tune `β` and `ρ` until this holds — and record the tuning sweep, since it belongs in the paper's reproducibility section.

---

## Phase 5 — Physarum tube-reinforcement model

**Goal:** the Tero et al. adaptive-conductivity model on the congested graph.

- Per edge: conductivity `D_ij`, cost `L_ij` = current congested travel time, conductance `w_ij = D_ij / L_ij`.
- Each iteration: solve the weighted-Laplacian system `L p = b` (`+I0` at source, `−I0` at sink, **ground `p_sink = 0`** to make the singular system solvable) with `scipy.sparse.linalg.spsolve` on CSR.
- Flux `Q_ij = w_ij (p_i − p_j)`; adapt `D_ij ← D_ij + dt·(f(|Q_ij|) − D_ij)` with `f(Q) = |Q|^μ / (1 + |Q|^μ)`, `μ ≈ 1.8`. Also implement the simpler `D ← |Q|` (Bonifaci) variant as a config switch.
- **Path extraction:** greedy maximum-flux walk from source with a visited set; if it stalls, fall back to shortest path on the `D > θ` thresholded subgraph. Document which fired — extraction method silently changing between runs would corrupt the stretch metric.
- **Warm-start disruption:** zero `D` on severed edges and drop them from the Laplacian, **retain all other conductivities**, keep iterating. This is the mechanism the whole resilience claim rests on.
- Model is single source–sink; run one instance per OD pair rather than faking multi-commodity flow.
- **Performance:** ~300 iterations × ~20 ms sparse solve × 30 OD pairs × scenarios is the dominant cost. Parallelize across OD pairs with `joblib`; reuse the previous iteration's pressures as a CG warm start if `spsolve` proves too slow.

**Exit criteria:** on an uncongested graph, Physarum converges to the **exact** Dijkstra path on ≥80 % of OD pairs (stretch = 1.0). This is the model's known theoretical behaviour — failing it means the implementation is wrong, not that the finding is interesting.

---

## Phase 6 — Metrics and measurement harness

**Goal:** metric definitions precise enough that the paper's numbers are defensible.

`metrics/schema.py` defines one flat result record (→ Parquet). `metrics/collectors.py` computes:

| Metric | Definition |
|---|---|
| `stretch` | `cost_alg / cost_dijkstra` on the **same** weighted graph. Always ≥ 1.0 — a value below 1.0 is a bug, so assert it. |
| `iters_to_converge` / `wallclock_to_converge` | To the stability criterion |
| `time_to_first_valid_path` | First iteration producing *any* path avoiding severed edges — **measured separately**, because a method may find a valid route fast and a good one slowly |
| `recovery_iterations` / `recovery_wallclock` | Disruption → re-stabilized valid path |
| `post_disruption_stretch` | vs. Dijkstra recomputed **on the disrupted graph** |
| `peak_memory_kb` | `tracemalloc` peak |
| `failed` | No valid path within budget (reported, never silently dropped) |

Every record carries: algorithm, OD pair, scenario, traffic level, warm/cold, seed, config hash, wall-clock timestamp.

**Exit criteria:** a 3-algorithm × 5-OD-pair smoke run writes a well-formed Parquet file with no nulls in required columns.

---

## Phase 7 — Disruption scenarios

**Goal:** closures that are severe enough to matter and valid enough to measure.

Six scenarios in `disruption/scenarios.py`, each selecting edges relative to the *current optimal path* (removing random far-away edges would change nothing):

| ID | Scenario |
|---|---|
| S1 | Single random edge on the optimal path |
| S2 | Highest-betweenness edge on the path (worst case) |
| S3 | 3 simultaneous path edges |
| S4 | 5 simultaneous path edges |
| S5 | **Corridor closure** — every edge of one named OSM way |
| S6 | **Intersection closure** — a node and all incident edges |
| S0 | No disruption (routing-quality control) |

Hard constraint: after removal, source and target must still be connected. Resample on failure and **log the rejection count** — a high rejection rate is itself a finding about network fragility, and belongs in the paper.

**Exit criteria:** each scenario produces reproducible edge sets under a fixed seed; a map figure per scenario confirming the removals land where intended.

---

## Phase 8 — Experiment matrix and runner

**Goal:** the full benchmark, resumable and reproducible.

- **30 OD pairs**, stratified 10/10/10 by short / medium / long straight-line distance, fixed seed.
- Matrix: 30 OD × 7 scenarios × 2 traffic levels × 3 algorithms × {warm, cold} × 5 seeds (ACO stochastic; 3 inits for Physarum) ≈ a few thousand runs.
- `experiments/runner.py`: `joblib.Parallel` over OD pairs, per-run try/except so one failure never kills the sweep, incremental Parquet append, and **resume-from-completed** keyed by run hash.
- `scripts/03_run_experiments.py --config configs/experiments/disruption.yaml`.

**Exit criteria:** full sweep completes; `failed` rate under 5 %; results Parquet loads cleanly into pandas.

---

## Phase 9 — Analysis, figures, and the results visualization

**Goal:** the first primary deliverable.

**Statistics.** Report **median and IQR**, not means — runtimes and recovery iterations are heavily right-skewed. Use **paired** comparisons (same OD pair, same scenario, same traffic level), Wilcoxon signed-rank for algorithm pairs, and bootstrap CIs on the medians.

**Figures** (`viz/figures.py`, `viz/maps.py`) — load the `dataviz` skill before writing any chart code:
1. Convergence curves (cost vs. iteration) per algorithm
2. Recovery-iteration distributions by scenario severity
3. Stretch-factor boxplots, pre- vs. post-disruption
4. Pareto scatter: recovery speed vs. post-disruption path quality — **this is the money figure**, the direct answer to the research question
5. Network maps: the three routes overlaid, before and after disruption
6. Congestion heatmap, light vs. heavy
7. Warm-start vs. cold-start delta (isolates genuine adaptation from "the algorithm is just slow")

**Web dashboard** (`web/`, `viz/export.py`): a self-contained HTML page published as an **Artifact**, reading a pre-computed JSON bundle. Interactive OD-pair selector, the three routes drawn on the Bengaluru street geometry, a disruption toggle showing re-routing, convergence traces, and the summary tables. Load the `artifact-design` skill before writing it. Keep the bundle well under the 16 MB limit by downsampling convergence traces to ~100 points and simplifying edge geometry.

**Exit criteria:** all seven figures regenerate from Parquet with one command; the dashboard is published and the URL is in hand. **This is where the current plan ends.**

---

## Deferred — research paper

Out of scope for now, by your call. Noting it here only so the phases above stay paper-ready and we don't have to redo work later:

- Phase 2 must **report its edge-match coverage rate** rather than silently patching gaps — that number goes in Methodology.
- Phase 4 must **keep its hyperparameter sweep results**, not just the winning values — that's the reproducibility section.
- Phase 6 records carry seed + config hash on every row, so results are reconstructible months from now.
- Phase 9's paired statistics (Wilcoxon, bootstrap CIs) are computed and stored, not just plotted.
- Known limitations to write up when we get there: single city and single bbox; synthetic SUMO demand rather than calibrated counts; Physarum run per-OD-pair rather than multi-commodity; and the wall-clock caveat above.

---

## Execution model — who runs what

**Division of labour.** I stay on Opus as orchestrator: I hold the research design, write the phase specs, and review what comes back. Everything mechanical goes to **Sonnet subagents**, one per phase. My turns stay short — write a spec, spawn, read the result, review a diff — while the subagents do the token-heavy implementation work.

I can set each subagent's model; I cannot change my own from inside the session (that's your `/model`). Reasoning effort comes from the agent definition, not the spawn call, so I control model choice but not effort level per call.

| Phase | Runs on | Why |
|---|---|---|
| 0 Scaffold + CLAUDE.md | Sonnet | Boilerplate; I supply the CLAUDE.md content |
| 1 Network acquisition | Sonnet | Straight OSMnx API work |
| 2 Congestion + SUMO join | Sonnet, **escalate to Opus if coverage < 85 %** | Mechanical against my spec; the fallback matching strategy is a judgment call |
| 3 Classical baseline | Sonnet | Trivial, but defines the `Router` ABC everything else implements |
| 4 ACO | Sonnet | Implementation is routine; **I review the hyperparameter sweep** |
| 5 Physarum | Sonnet | **I review the Laplacian grounding and path-extraction code** — subtle and silently wrong if botched |
| 6 Metrics | Sonnet | Schema work |
| 7 Disruption scenarios | Sonnet | Schema work |
| 8 Experiment runner | Sonnet | Parallelism and resume logic |
| 9 Figures + dashboard | Sonnet | Loads `dataviz` and `artifact-design` skills itself |

**Parallelism.** Phases are mostly sequential, but two pairs are independent and get spawned together: **{4 ACO, 5 Physarum}** once Phase 3 has landed `algorithms/base.py`, and **{6 Metrics, 7 Scenarios}**. That is seven dispatches rather than ten serial hops.

**Why Phase 0 matters more than it looks.** Each subagent starts cold. `CLAUDE.md` plus this plan file are their entire briefing, so the Phase 0 CLAUDE.md must carry the invariants, the layout and the commands — otherwise every later phase re-derives context and the token saving evaporates.

## What you need to do manually

1. **Install SUMO** — the Eclipse SUMO Windows installer needs admin rights and a GUI, so I can't script it. Afterwards set `SUMO_HOME` (typically `C:\Program Files (x86)\Eclipse\Sumo`) and restart the terminal. Currently not installed and `SUMO_HOME` is unset.
2. **Create the venv**, or approve me doing it: `! py -3.10 -m venv .venv` — Python 3.10, not the 3.14 you also have installed.
3. **Permissions worth pre-approving** so phases don't stall on prompts: `pip install`, `python scripts/*`, `pytest`, `netconvert`, `sumo`, `duarouter`, and network access to `overpass-api.de` for the OSM download. I can wire these into `.claude/settings.json` in Phase 0 via the `update-config` skill if you want.

Nothing else needs your hands until Phase 9, when I'll hand you the dashboard URL.

## Verification

Per phase, in order — each phase's exit criteria gate the next:

```bash
python scripts/00_setup_check.py        # Phase 0: env + SUMO
python scripts/01_build_network.py      # Phase 1: prints node/edge counts (expect 2k-8k nodes)
python scripts/02_run_sumo.py           # Phase 2: prints edge-match coverage (expect >=85%)
pytest tests/ -v                        # Phases 3-7: toy-graph correctness
python scripts/03_run_experiments.py --smoke   # Phase 8: 5 OD pairs end to end
python scripts/03_run_experiments.py           # Phase 8: full sweep
python scripts/04_make_figures.py       # Phase 9: all figures from Parquet
python scripts/05_export_web.py         # Phase 9: dashboard JSON bundle
```

I'll run each of these myself as the gate on the corresponding subagent's work — a phase isn't done because the agent says so, it's done when its exit criterion command passes.

**Correctness checks that catch real bugs, not just crashes:**
- A\* cost == Dijkstra cost on 100 random OD pairs (Phase 3)
- Physarum reaches stretch 1.0 on ≥80 % of uncongested OD pairs (Phase 5)
- `stretch >= 1.0` asserted on every record (Phase 6)
- Every post-disruption path contains zero severed edges (Phase 7)
- A fixed seed reproduces identical results across two runs (Phase 8)
