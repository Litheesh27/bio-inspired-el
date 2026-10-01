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
- [x] Phase 2d — SUMO recalibration + ONE shared free-flow baseline, and a
      dimensional bug fix that had been silently corrupting every SUMO number.
      **THE BUG (most important thing in this entry):** `network/ids.py`'s
      `compute_edge_travel_times` averaged the matched SUMO segments' TRAVEL
      TIMES, which answers "how long to cross one matched segment", not "how
      long to cross this graph edge". netconvert splits ways while OSMnx merges
      them, so matched-SUMO-length / graph-edge-length has median 1.44 and p90
      8.9 here — the old rule understated most edges in proportion to how
      finely they were split, hitting busy arterials hardest. Result: **1,794
      of 7,015 edges (26%) implied speeds above the fastest limit anywhere in
      the network, the worst by 266x**, which silently broke A*'s admissibility.
      The A*-vs-Dijkstra test had been passing by luck of the sampled pairs.
      Fixed by working in PACE: `tt = graph_length * sum(tt_i) / sum(L_i)`.
      Edges-over-max 1,794 -> 242 (config_speeds) and 661 -> 1 (sumo_speeds);
      A* now matches Dijkstra 30/30 on every combination.
      `_AGGREGATION_VERSION = "v2-length-weighted-pace"` is in the SUMO cache key.
      **Shared baseline:** a NEW Phase-2 artifact
      `data/network/<name>_<hash>_sumospeeds.graphml` re-annotates speed_kph /
      travel_time from the generated net.net.xml (97.53% coverage; 173 edges
      keep config speeds, each stamped `speed_source`). Phase 1 is UNCHANGED
      and still builds independently — do not make the graph build depend on
      netconvert. Select via `baseline_graph` in `configs/congestion.yaml`
      (default `sumo_speeds`) / `load_baseline_graph`; the choice is stamped
      into every `TrafficSnapshot.meta`. Mean speed_kph 22.74 -> 51.49, so
      ABSOLUTE times on this graph are optimistic for Bengaluru — ratios are
      comparable, absolute values are not realistic. `config_speeds` remains
      available and `configs/area_central_blr.yaml` is untouched.
      **Demand:** `period_heavy` 0.6 -> 1.2. Teleports 30.5% -> 5.13%, ratio
      2.11x on OD paths. Heavy does NOT fully drain at sim_end 5000 (12.2%
      still running); draining it needs sim_end 12000 which RAISES teleports to
      7.4% for a 0.3% ratio change, so it was declined deliberately — a teleport
      falsifies edge travel times, an unfinished trip does not.
      **The 20x clip is still needed** — a zero-teleport, fully-drained light
      run still clips 1.53% and inflates 34% without it. Cause is SUMO's
      per-interval traveltime being dominated by one car at a red light, not
      gridlock. Clipped samples' median is 86-97x free-flow: a cleanly
      separated tail, not a threshold judgement call.
      **Ratios on the one denominator:** BPR 1.18x / 2.53x, SUMO 1.81x / 2.11x
      (OD path). BPR heavy is 0.034 OVER its 2.5 band and was deliberately NOT
      retuned. They bracket rather than agree and must not be merged into a
      single "congestion level": SUMO's floor is higher (signal delay in the
      numerator, none in the denominator), BPR's range is wider (synthetic
      demand loads shortest paths, exactly where an OD-path measurement walks).
      Heavy-vs-light contrast is modest (1.55x -> 1.85x per edge); 61% of SUMO
      edges see no vehicle even at heavy. See `docs/tuning/sumo_calibration.md`.
- [x] Phase 3 — Classical baseline: `Router` ABC, Dijkstra, A* (cost equality vs. Dijkstra on 100 OD pairs).
      A*'s heuristic is admissible only while snapshot times are >= the
      BASELINE GRAPH's free-flow times. True for FreeFlow and BPR always, and
      for SUMO on `sumo_speeds`; FALSE for SUMO on `config_speeds` (242 edges).
      See the docstring in `algorithms/classical.py` and Phase 2d above.
- [x] Phase 4 — ACO (MMAS) implemented, tuned and measured. **Exit criterion
      MISSED — see Phase 4d for the authoritative numbers and CIs.** The
      stretch-1.157 plateau noted below was a PRE-MEMO observation; Phase 4c
      superseded it (the plateau was largely ants re-entering dead subtrees).
      The goal-directed ablation buys nothing: identical to purely-local under
      freeflow, worse under BPR heavy, because h(v) is a free-flow great-circle
      bound that steers ants toward geometrically direct routes exactly when
      those are congested. So the methodologically fair default is also the
      better-performing one. Freeflow is the HARDER condition for ACO --
      congestion flattens the cost surface and more routes land within 10% of
      optimal, which is the opposite of the naive expectation and is a
      reportable result.
      Headline mode is PURELY LOCAL (`use_goal_heuristic: false`,
      eta = 1/travel_time); the goal-directed variant (eta = 1/(tt + h(v)),
      A*'s heuristic) is a reported ablation, not the default — giving ACO
      target-location information Physarum never gets would make the
      three-way comparison unfair. Plain MMAS had a 0% ant completion rate
      on this graph (mean out-degree ~2.5, cul-de-sacs), fixed by
      backtracking construction. Local mode plateaus near stretch 1.157 on
      some OD pairs regardless of tuning — a real negative result to report.
      See `docs/tuning/aco_sweep.md`.
- [x] Phase 4b — ACO performance. 0.186 -> 0.078 s/iteration sustained
      (2.4x), bit-for-bit identical results to Phase 4 (15 runs x 10 fields,
      zero differences; `random.Random` retained). Phase 8's 4,200 ACO solves:
      ~55 h serial, ~5.4 h at 10x parallel.
      Win came from REMOVING numpy from the per-candidate hot path: graph mean
      out-degree is 2.46, so numpy dispatch on 1-3 element arrays cost far more
      than the arithmetic. Plus a per-iteration weight cache (tau^alpha*eta^beta
      is constant within an iteration) and inlining the candidate loop.
      BENCHMARKING CAVEAT: this machine's throughput swings up to 3.4x with
      thermal state. Only ever compare variants interleaved within ONE process.
      An earlier "588 h" figure in this file was extrapolated from a regressed
      intermediate build measured across separate runs; it was wrong.
- [x] Phase 4c — Dead-node memo adopted, re-tuned, criterion first measured.
      Memo raises ant completion 36% -> 80% at matched settings and roughly
      halves construction work; quality gain is modest and tail-concentrated.
      Its cost is measured, not assumed: **~66% of memo prunes refuse a node
      that was still viable** given the ant's true tabu (585,445 prune events,
      12,031 audited by BFS), and a deterministic grid test pins memo-on paths
      as a strict SUBSET of memo-off. Documented as a deliberate lossy
      construction heuristic, not an optimization — deadness is context-
      dependent on the visited set. Implemented by not clearing the `visited`
      mark on backtrack, so `visited` does double duty at zero extra cost.
      Re-tune: beta_no_heuristic 1.0 -> 0.5, n_ants 40 -> 80, budget 600 -> 150.
      n_ants only helped POST-memo (at 26% completion extra ants bought extra
      waste, which is why the pre-memo sweep concluded more ants did not help).
      Phase 8 ACO projection 47 h -> 18.5 h serial, ~1.9 h at 10x parallel.
      Its 86.7%/96.7% criterion figures were in-sample and single-seed;
      **superseded by Phase 4d** — do not cite them.
- [x] Phase 4d — Cache race fixed; exit criterion re-measured cleanly.
      VERDICT: **Phase 4's exit criterion is MISSED.** 30 held-out pairs
      (draw seed 4242, zero overlap with tuning or Phase 4c pairs) x 5 seeds
      x 2 conditions = 300 solves, 0 failed, stretch >= 1.0 held on all 300.
        freeflow   73.3%, bootstrap 95% CI [58.7, 86.7]  -- clear miss, whole CI below 90
        BPR heavy  80.7%, bootstrap 95% CI [68.0, 92.0]  -- indistinguishable from the bar
      Phase 4c's 86.7%/96.7% were in-sample single-seed estimates at a
      favourable seed: 86.7% is EXACTLY the upper CI bound here, and 96.7% is
      outside the BPR-heavy CI entirely. Seed 1 alone gives BPR heavy exactly
      90.0% (a pass) while seed 4 gives 73.3% -- a single-seed run could
      honestly have reported either. THIS IS WHY SINGLE-SEED IN-SAMPLE
      NUMBERS ARE NOT REPORTABLE IN THIS PROJECT.
      Path-length stratification (terciles): freeflow 88.0 / 84.0 / **48.0**%,
      BPR heavy 94.0 / 90.0 / **58.0**%. The long tercile accounts for the
      ENTIRE BPR-heavy shortfall but NOT freeflow's -- freeflow would still
      miss at 86.7% even if long pairs matched short ones, so it has a second
      problem spread across short/medium pairs. Failure mode is "the colony
      locks onto a badly wrong corridor and never leaves it": 8/30 freeflow
      pairs are seed-dependent (one 26-hop pair gives {1.000, 1.000, 1.000,
      1.233, 1.446}), and MMAS's own convergence fires on 79%/69% of solves,
      so these are settled local optima, not unfinished searches.
      Cache fix: both providers wrote to a temp path that was a pure function
      of the cache key, so concurrent writers shared ONE temp file; Windows
      os.replace additionally fails while any process holds a handle on source
      OR destination, including a reader. Now: unique temp per writer, fsync,
      retry with backoff, validate-on-read (corrupt/truncated -> recompute,
      never half-believed), and `prewarm_snapshots()` in `congestion/base.py`.
      **PHASE 8'S RUNNER MUST CALL `prewarm_snapshots()` BEFORE PARALLELISING.**
- [x] Phase 5 — Physarum tube-reinforcement model. Exit criterion PASSED:
      26/30 (87%) exact stretch-1.0 matches under freeflow. Laplacian built
      on an undirected projection (grounded at the sink); extracted paths are
      always directed-valid in the original MultiDiGraph. Convergence uses
      max|dD| < eps ONLY — path-stability fired early on wrong paths because
      D decays in near-lockstep across edges early on. ~3-8 ms per solve.
- [x] Phase 6 — Metrics harness. 33-column `ResultRecord` + explicit pyarrow
      schema, three nullability tiers enforced by `validate()` before every
      append AND mirrored as `nullable=False` in Arrow. Entry point for Phase 8:
      `run_and_collect(router, G, source, target, snapshot, scenario, budget,
      warm, seed, ...)`, which accepts precomputed `reference_cost` /
      `disrupted_reference_cost` / `G_disrupted` so all three algorithms in one
      matrix cell share ONE Dijkstra denominator. `make_run_id(...)` is the
      deterministic resume key. Only `SchemaViolation` escapes — that means a
      malformed record and must stop the sweep; every run-level problem returns
      a `failed=True` row instead.
      TWO first-valid-path columns, not one: the recovery-group version is null
      for S0, so `initial_time_to_first_valid_path_iters` keeps that measurement
      on the control rows. `iteration_budget` and `recovery_converged` are
      recorded because `iterations` alone cannot distinguish converged-early
      from burned-the-budget — which matters: Phase 4d found MMAS's convergence
      fires on only 79%/69% of solves.
      Parquet: ONE SHARD PER WORKER by construction + `merge_shards`, with the
      Phase 4d atomic-write discipline (unique temp, fsync, backoff retry, read
      side retried too). `merge_shards` RAISES on an unreadable shard rather
      than silently dropping evidence. The concurrency test was verified to have
      teeth: the same contention against a reconstructed pre-Phase-4d writer
      crashed 5 of 6 writers; against the shipped path, 0 of 6.
- [x] Phase 7 — Disruption scenarios S0-S6. `generate_disruption(...) ->
      Disruption(scenario_id, removed_edges, ok, rejections, attempts, reason,
      meta)`. Seeds derived via **sha256, NOT builtin `hash()`** — `hash()` on
      strings is randomized per process by PYTHONHASHSEED, which would have made
      Phase 8 silently irreproducible across workers. Pinned by a test.
      Semantics (documented choices, not accidents): closure unit is the ordered
      node pair and ALL parallel edges on it close; the reverse direction closes
      too (`both_directions=True`), so an S1-S4 removed edge is on the path OR is
      the reverse of a path edge; S5 closes ONE chosen way from the seed edge's
      `osmid` list, not the union — the union would close 2-4 unrelated corridors
      and make S5's severity depend on how OSMnx happened to simplify.
      `apply_disruption` returns `nx.restricted_view` by default: O(1) and
      STRUCTURALLY incapable of mutating the base graph, 20x cheaper than
      `G.copy()` (78 ms each here). Caveat: a view shares the base's edge
      ATTRIBUTE dicts, so a caller wanting to write attributes must pass
      `copy=True`. Nothing here does — algorithms read weights from the snapshot.
      **FRAGILITY RESULT (paper-worthy, 4,200 generations, 0 infeasible).**
      Rejection rate = candidate closures discarded for disconnecting s-t:
        S1 2.6% | S2 1.0% | S3 14.5% | S4 31.7% | S5 7.4% | S6 4.5%  (freeflow)
      1. The network is NOT fragile to single closures — only ~1 path edge in 40
         is a cut edge for its own OD pair, as a dense irregular grid should be.
      2. Fragility is SUPER-ADDITIVE in simultaneous closures: 1->3->5 edges
         gives 2.6% -> 14.5% -> 31.7%, far faster than 3x and 5x.
      3. A whole CORRIDOR is safer than 5 scattered edges (S5 7.4% at a median 4
         and up to 60 edges, vs S4 31.7% at 7). Corridor edges are consecutive,
         so one detour routes around all of them.
      4. Congestion barely moves any of it (within ~1pp), as expected —
         connectivity is topological. S2 is the exception because its selection
         is weight-dependent.
      A test asserts ZERO of 60 real-graph disruptions leave the optimal path
      unchanged — the null-result trap this phase exists to avoid.
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
