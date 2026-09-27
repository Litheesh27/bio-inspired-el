# bioroute — Self-Healing Routing on Bengaluru's Road Network

## Project

A three-way benchmark of Dijkstra/A*, Ant Colony Optimization, and a
Physarum polycephalum tube-reinforcement model, routing on the real
Central Bengaluru road network under SUMO-simulated congestion, measuring
routing quality AND recovery after simulated road closures.

**Research question:** on a real irregular Indian city road network under
realistic traffic, how much recovery speed do bio-inspired routing methods
buy, and what is the path-quality cost?

The full plan (all phase specs, exit criteria, execution model) is
version-controlled at `docs/PLAN.md`.
Read it alongside this file — this file is the dense summary + invariants;
that file has the detail.

## Locked decisions

| Decision | Choice |
|---|---|
| Congestion source | SUMO primary, behind a `CongestionProvider` interface, with an analytic BPR fallback so downstream phases never block |
| Study area | Central Bengaluru ~4x4 km (MG Road / Cubbon Park / Shivajinagar / Richmond Town) — irregular organic topology |
| Recovery protocol | Warm-start (retained pheromone/conductivity) as the main experiment, plus a cold-start control |
| Primary deliverables | Results visualization (web dashboard) + figures + benchmark suite. Research paper is deferred, but every phase is built paper-ready |

## Phases

- [x] Phase 0 — Environment, scaffold, and CLAUDE.md
- [x] Phase 1 — Road network acquisition (cached graph, 2,000-8,000 nodes)
- [x] Phase 2a — Congestion interface + BPR fallback (`CongestionProvider`,
      `TrafficSnapshot`, `FreeFlowProvider`, `BPRProvider`)
- [x] Phase 2b/2c — SUMO pipeline + SUMO<->OSMnx edge-ID join (>=85% coverage).
      SUMO 1.27.1 is installed at C:\Sumo. Achieved 97.53% ID-based edge-match
      coverage (no geometric fallback needed). See `SUMOProvider` in
      `congestion/sumo.py` and the join logic in `network/ids.py`.
- [x] Phase 3 — Classical baseline: `Router` ABC, Dijkstra, A* (cost equality vs. Dijkstra on 100 OD pairs)
- [~] Phase 4 — ACO (MMAS) implemented and tested, exit criterion NOT yet
      measured. Headline mode is PURELY LOCAL (`use_goal_heuristic: false`,
      eta = 1/travel_time); the goal-directed variant (eta = 1/(tt + h(v)),
      A*'s heuristic) is a reported ablation, not the default — giving ACO
      target-location information Physarum never gets would make the
      three-way comparison unfair. Plain MMAS had a 0% ant completion rate
      on this graph (mean out-degree ~2.5, cul-de-sacs), fixed by
      backtracking construction. Local mode plateaus near stretch 1.157 on
      some OD pairs regardless of tuning — a real negative result to report.
      See `docs/tuning/aco_sweep.md`.
- [ ] Phase 4b — Vectorize ACO. BLOCKING Phase 8: measured 0.84 s/iteration
      (dict-keyed pheromone, pure-Python ant loops) => 504 s per solve at
      budget 600 => 588 h serial for Phase 8's 4,200 ACO runs. Rewrite with
      numpy arrays + CSR adjacency (target 10-20x); combined with joblib on
      14 cores this brings the sweep to ~5 h. Algorithm and tuned
      hyperparameters must not change.
- [x] Phase 5 — Physarum tube-reinforcement model. Exit criterion PASSED:
      26/30 (87%) exact stretch-1.0 matches under freeflow. Laplacian built
      on an undirected projection (grounded at the sink); extracted paths are
      always directed-valid in the original MultiDiGraph. Convergence uses
      max|dD| < eps ONLY — path-stability fired early on wrong paths because
      D decays in near-lockstep across edges early on. ~3-8 ms per solve.
- [ ] Phase 6 — Metrics and measurement harness (Parquet schema, no nulls in required columns)
- [ ] Phase 7 — Disruption scenarios S0-S6 (reproducible under fixed seed, connectivity preserved)
- [ ] Phase 8 — Experiment matrix and runner (full sweep, failed rate < 5%, resumable)
- [ ] Phase 9 — Analysis, figures, and results dashboard (7 figures + published Artifact dashboard)

## Repo layout

```
bio_el/
├── CLAUDE.md, README.md, requirements.txt, pyproject.toml, .gitignore
├── configs/
│   ├── area_central_blr.yaml       # bbox, network filters (Phase 1)
│   ├── algorithms.yaml             # ACO / Physarum hyperparameters (Phase 4/5)
│   └── experiments/{baseline,disruption}.yaml   (Phase 8)
├── src/bioroute/
│   ├── config.py                   # YAML -> dataclasses, seed mgmt, config hash (Phase 1)
│   ├── network/{acquire,graph,ids}.py            (Phase 1, Phase 2c)
│   ├── congestion/{base,bpr,sumo,snapshot}.py     (Phase 2)
│   ├── algorithms/{base,classical,aco,physarum}.py (Phase 3, 4, 5)
│   ├── disruption/{scenarios,apply}.py            (Phase 7)
│   ├── metrics/{schema,collectors}.py             (Phase 6)
│   ├── experiments/{matrix,runner}.py             (Phase 8)
│   └── viz/{figures,maps,export}.py               (Phase 9)
├── scripts/00_setup_check.py ... 05_export_web.py
├── web/                        # results dashboard (published as an Artifact)
├── figures/                    # publication-quality PNG/SVG output
├── data/{raw,network,sumo,results}/   # raw+sumo ignored; network cached; final results parquet tracked
└── tests/
```

Every `src/bioroute/**` package has an `__init__.py`. Every module currently
contains ONLY a docstring stating which phase implements it and what it will
contain — no placeholder logic. Read a module's docstring before writing to
it; it tells you the intended contract.

## Invariants (non-negotiable)

- `stretch = cost_alg / cost_dijkstra` is always computed against Dijkstra on
  the *same* weighted graph. Never compare stretch across differently-
  weighted graphs. `stretch < 1.0` is a bug — assert it.
- Every experiment run carries an explicit seed; nothing depends on unseeded
  RNG.
- Cached artifacts (graph, SUMO output, results) are keyed by a config hash;
  changing a config must invalidate the cache.
- Algorithms never see the congestion source directly — only a
  `TrafficSnapshot` from a `CongestionProvider`. This is what keeps the
  SUMO/BPR swap free.
- Failures are recorded (`failed=True`), never silently dropped from results.
- The Phase 8 sweep output is committed to git as
  `data/results/final_<confighash>.parquet`. It is the experimental evidence
  every figure derives from, and re-running the sweep to recover it costs
  hours. Partial/resume artifacts stay gitignored — only the final file is
  tracked (see `.gitignore`).
- Python 3.10 venv at `.venv`. Always use `.venv/Scripts/python.exe` (not
  the system `python`/`py`).

## Commands

```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt   # install deps
.venv/Scripts/python.exe -m pip install -e .                  # editable install of bioroute
.venv/Scripts/python.exe scripts/00_setup_check.py            # Phase 0: env + SUMO check

.venv/Scripts/python.exe scripts/01_build_network.py          # Phase 1: prints node/edge counts (expect 2k-8k nodes)
.venv/Scripts/python.exe scripts/02_run_sumo.py                # Phase 2: prints edge-match coverage (expect >=85%)
.venv/Scripts/python.exe -m pytest tests/ -v                   # Phases 3-7: toy-graph correctness
.venv/Scripts/python.exe scripts/03_run_experiments.py --smoke        # Phase 8: 5 OD pairs end to end
.venv/Scripts/python.exe scripts/03_run_experiments.py --config configs/experiments/disruption.yaml   # Phase 8: full sweep
.venv/Scripts/python.exe scripts/04_make_figures.py             # Phase 9: all figures from Parquet
.venv/Scripts/python.exe scripts/05_export_web.py               # Phase 9: dashboard JSON bundle
```

`scripts/01` through `05` do not exist yet; each is created by the phase
noted above. Currently only `00_setup_check.py` exists and is expected to
fail its SUMO checks (SUMO is not installed on this machine) while every
other check passes.

## Honest-reporting note

Dijkstra will beat both bio-inspired methods on raw wall-clock recovery — a
recompute on a 5k-node graph is milliseconds, ACO and Physarum take seconds.
This is expected and must be reported, not hidden. The interesting, defensible
metrics are iterations-to-adapt, time-to-first-valid-path, and
post-disruption path quality — that is the actual speed-vs-quality trade
this project quantifies.
