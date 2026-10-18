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
- [x] Phase 8 — Runner built, verified, and THE FULL SWEEP IS COMPLETE (see Phase 8e).
      Matrix: **420 cells, 5,520 runs** (aco 3,900 / physarum 780 / dijkstra 420
      / astar 420) over 7 scenarios x {sumo light, sumo heavy} x {warm, cold},
      `sumo_speeds` baseline. 30 OD pairs, draw seed 8080, stratified 10/10/10
      by straight-line distance, **verified zero overlap** with the ACO tuning
      draw (2024) and the Phase 4d criterion draw (4242).
      Projected **34.2 h serial, ~2.4 h at 14 workers** (lower bound, perfect
      scaling assumed — never quote it as a performance figure). ACTUAL: 10.3 h
      of wall-clock at 14 workers for the 404 remaining cells, i.e. the
      perfect-scaling lower bound understated the real run by ~4x. Logged here
      as a standing reminder of what that caveat is worth.
      Seeds per algorithm: aco 5, physarum 1, classical 1. **Physarum's single
      seed is MEASURED, not assumed** — 5 OD pairs x 3 initialisation seeds gave
      costs identical to 4 d.p. (0/5 varied), because conductivities initialise
      from a fixed value. Classical routers get cold-only, since a one-shot
      router has no warm mode and the duplicate rows would be meaningless.
      Verified: `--smoke` 18 cells / 126 runs / 0 failed; `--resume` found all
      126 run_ids on disk and skipped them, 0 cells remaining.
      GOTCHA: `load_baseline_graph(cfg, baseline="sumo_speeds")` computes the
      hash from `netconvert_flags`, so calling it with the default `None` yields
      a DIFFERENT hash and a FileNotFoundError whose message wrongly suggests
      re-running `scripts/02_run_sumo.py`. Use `netconvert_flags_for(cfg)` from
      `experiments/runner.py`, or read the flags from `configs/sumo.yaml`.
- [x] Phase 8e — **THE FULL SWEEP IS RUN AND COMMITTED.**
      `data/results/final_14a535c02b83.parquet` (416 KB, 5,520 rows x 38 cols,
      baseline_hash 285f18f9a225). This file IS the experimental evidence; every
      Phase 9 number must derive from it and nothing else.
      Integrity: 5,520 unique `run_id`, **0 duplicates**; per-algorithm counts
      exactly as designed (aco 3,900 / physarum 780 / dijkstra 420 / astar 420);
      30 OD pairs x 7 scenarios x 2 traffic levels x {warm,cold} = 780 cells.
      **28 failed rows (0.51%), ALL ONE CAUSE:** "scenario S4 infeasible: no
      connectivity-preserving choice of 5 path edge(s) in 50 attempts" — 2 cells
      where Phase 7 could not cut 5 path edges without disconnecting s-t. That
      is the fragility result, not a bug. Exit criterion PASS (<5%).
      Invariants re-verified on the real data, 0 violations of any:
      `stretch` (n=5,492), `post_disruption_stretch` (n=5,012),
      `walk_best_stretch` (n=542), `post_disruption_walk_stretch` (n=343) all
      >= 1.0, and `walk_best_cost >= path_cost` on all 542 rows where both exist.
      **RESULT 1 — warm start's real shape is BETTER than "2.3x", and the 24x
      figure is an ARTIFACT. DO NOT REPORT 24x.** Physarum recovery is BIMODAL
      under warm start: **48.3% of warm recoveries converge in EXACTLY 1
      iteration**, while cold start never does — cold's MINIMUM is 73 iterations
      (n=358 each, cold 25-50-75th = 90/98/111). The retained conductivity field
      already encodes a viable detour, so one adaptation step suffices. Because
      the warm distribution has modes at 1 and ~50, the ratio of medians (98 vs
      42 iters; 5.66 vs 0.235 s = "24x") is not a valid statistic.
      **Correct PAIRED numbers on identical (od, scenario, traffic, seed):
      median ratio cold/warm = 2.97x iterations (warm faster on 350/358,
      Wilcoxon p=3.2e-59) and 6.87x wall-clock (336/358, p=9.0e-52).**
      Per-iteration cost is IDENTICAL warm vs cold (median 0.0511 vs 0.0538
      s/iter), which is the proof that the entire gain is fewer iterations and
      not cheaper ones — exactly the claim the project set out to test.
      **RESULT 2 — THE "CONGESTION HELPS ACO" FINDING DOES NOT SURVIVE SUMO.
      It REVERSES.** On S0, ACO's within-10% rate is light 81.3% vs heavy 58.0%,
      and PAIRED on the same (od, seed) heavy is significantly WORSE (Wilcoxon
      p=3.6e-02; 37 pairs pass only under light vs 2 only under heavy). Phase 4/4d
      measured the opposite direction on **BPR / config_speeds**; the sweep uses
      **real SUMO / sumo_speeds**. So the sign of the congestion effect on ACO is
      a property of the CONGESTION MODEL, not of ACO. This is direct evidence for
      Phase 2d's warning that BPR and SUMO "bracket rather than agree and must
      not be merged into a single congestion level".
      **CONSEQUENCE: Phase 8c's "the two bio-inspired methods fail in OPPOSITE
      directions under congestion" is NO LONGER SUPPORTED on the sweep's own
      data — under SUMO, BOTH degrade.** Phase 9 must either drop that framing
      or state it as BPR-specific. Do not carry it forward unqualified.
      Also: ACO's exact-match (stretch == 1.0) share is only 43-44%, and its
      median stretch is 1.005 (light) / 1.021 (heavy); per-seed within-10% spread
      is tight (0.567-0.633 heavy, 0.767-0.833 light), so this is not a seed fluke.
      **RESULT 3 — Physarum's pooled competence is CONFIRMED to be mostly the
      fallback's, on the sweep's own graph.** S0 pooled exact-match 90.0% (heavy)
      / 86.7% (light) — reproducing Phase 5's 87% — against **walk-only exact
      match of just 23.3% / 16.7%**, with the walk producing ANY path on 69.8%
      of all Physarum rows. Reported-path provenance over all 776 successful
      Physarum rows: `full_graph_fallback` 315 (40.6%), `max_flux_walk` 232
      (29.9%), `theta_subgraph_fallback` 229 (29.5%) — i.e. **~70% of Physarum's
      reported paths did NOT come from its own extraction**, and the median
      stretch is exactly 1.000 within every one of the three provenance classes,
      so pooling hides the difference completely. This retires the Phase 8d
      speculation that sumo_speeds would be kinder to the walk: it is not
      (16.7-23.3% here vs Phase 8c's 16.7-46.7% on config_speeds).
      **RESULT 4 — CORRECTED. The original claim here was WRONG, and the sign
      REVERSES once the comparison is paired.** It first read "warm start
      improves the model's own extraction quality: `post_disruption_walk_stretch`
      median 1.062 warm vs 1.199 cold (n=130/213)". That is an UNPAIRED
      comparison across two differently-composed samples, and the unequal n is
      the tell — it should have stopped me. The warm max-flux walk COMPLETES a
      path on only 36.3% of recoveries vs cold's 59.5%, so the two medians are
      taken over different cells. Paired on the 126 cells where BOTH complete:
      warm 1.0587 vs **cold 1.0127**, cold better on 44 and warm on 12,
      Wilcoxon p=2.7e-04. The 87 cells only COLD completes have median 1.371 and
      are exactly what inflated cold's unpaired figure.
      Pairing verified sound: pre-disruption `walk_best_stretch` is byte-identical
      warm vs cold (max |diff| 0.0e+00 over 250 cells), as it must be — before
      the disruption the two modes are the same run.
      **HONEST STATEMENT: warm start makes the walk LESS LIKELY TO COMPLETE AT
      ALL, and on cells where both complete, cold's walk is marginally BETTER.
      Warm start's robust win is SPEED (Result 1), not extraction quality.**
      **MECHANISM — TESTED, AND MY FIRST GUESS WAS WRONG. See Phase 9b-mech.**
      I originally wrote: "the retained conductivity field is tuned to the OLD
      optimum, so after a closure it keeps driving flux down now-severed
      corridors and the walk dead-ends; a cold field starts uniform and explores
      freely." The second clause is right. **The first clause is refuted: the
      closure's LOCATION is almost irrelevant.** Do not repeat it.
      **STANDING RULE THIS COST US: in this project, never compare two medians
      whose n differ without first checking whether the samples are the same
      cells. The sweep is fully paired BY CONSTRUCTION, so an unpaired
      comparison is always a choice, and usually the wrong one.**
      Classical sanity: Dijkstra and A* are at stretch 1.000 and
      post-disruption stretch 1.000 on every scenario, recovering in 1 iteration
      / 0.031 s and 0.027 s — the honest-reporting baseline stands.
- [x] Phase 8a — Physarum singular-Laplacian degeneracy: diagnosed, fixed.
      **THE SWEEP IS UNBLOCKED.** Mechanism was STRANDED COMPONENTS, not
      conductivity decay: `p_sink = 0` grounds exactly ONE component, Phase 7
      guarantees only source-target connectivity, and closures remove edges
      while never removing nodes — so an unreachable node gives an exactly-zero
      Laplacian row (isolated) or an ungrounded floating block (rows sum to
      zero). `spsolve` then returns ALL-NaN pressures which propagate into Q
      and D for the rest of the run. Confirmed structural, not decay:
      `first_singular_iter == 1` on all 12 affected solves.
      Census over all 360 disrupted cells — **82 (22.8%) strand nodes**:
      S1 0/60, S2 0/60, S3 2/60, S4 2/60, S5 18/60, **S6 60/60**. S6
      ("intersection closure") removes every edge incident to a path node, so
      it isolates that node BY CONSTRUCTION and degenerated on 100% of cells.
      **WHY THIS MATTERED MORE THAN THE WARNING SUGGESTED:** NaN pressures make
      every `flux > best_flux` comparison False (walk dead-ends) and every
      `D > theta` False (theta subgraph empty), so extraction fell through to
      `full_graph_fallback` = plain Dijkstra. Physarum was reporting DIJKSTRA'S
      OWN PATH as its own, with `failed=False`. S6 `post_disruption_stretch` was
      mean 1.000 / max 1.000 on all 12 rows — **fabricated perfection on exactly
      the scenario where Physarum is worst.** Post-fix: mean 1.181 / max 2.044.
      Fix: `_refresh_active_component()` restricts the Laplacian to the source's
      connected component in the undirected projection of the SURVIVING graph.
      Membership derives from surviving edges, never from D magnitudes, so
      sparsity cannot drift as tubes atrophy. Called from `_build_model` AND
      explicitly from the warm branch of `apply_disruption` — warm deliberately
      does not rebuild the model, which is exactly where the bug lived.
      This is the model, not a patch: Physarum cannot flow where it cannot
      reach, so the pressure field there is genuinely undefined (any constant
      satisfies the equations) — which is what the singularity was reporting.
      **An epsilon floor on D was REJECTED**: it would give SEVERED roads a
      nonzero conductance, inventing transport capacity across a closed
      intersection. No `d_floor` knob exists, deliberately; reasoning is in
      `configs/physarum.yaml`.
      Backstop retained: `PhysarumDegenerateSolve` promotes scipy's warning to a
      raised error AND rejects any non-finite pressure vector (needed — one
      measured case degenerated WITHOUT warning, so warning-detection alone was
      never sufficient). `PhysarumUnreachableTarget` covers a stranded target.
      Phase 6 records these as `failed=True`. Nothing is warned-and-continued.
      Phase 5's exit criterion is UNCHANGED at 26/30 (87%) — the undisrupted
      solve is bit-identical, so no correctness-for-stability trade.
      Singular solves 12/54 -> 0/54. `full_graph_fallback` 21/30 -> 13/30:
      12 were bug-caused (all S6), 9 are independent (3x S0, 6x S1) and
      bit-identical after the fix — that residual is Phase 5's genuine bimodal
      behaviour, confirmed identical at budget 300 as at 60.
- [x] Phase 8b — `best_path_extraction_method` added (tracked when `best_cost`
      improves, NOT at the final iteration) plus a schema column, so every sweep
      row records where its reported path came from. Instrumentation only,
      verified bit-identical to HEAD across 30 pairs.
      Of Physarum's reported paths (30 pairs, freeflow): 53% from
      `theta_subgraph_fallback`, 33% from `max_flux_walk`, 13% from
      `full_graph_fallback` (= plain Dijkstra). The two method columns disagree
      on 17/30 pairs. Mechanism: `theta: 0.05` while D starts near 1.0, so for
      ~18 iterations the theta subgraph is essentially the WHOLE graph and the
      "fallback" is Dijkstra; `best_cost` is a STRICT min over all iterations so
      it locks in there and the walk can only tie, never win (observed: walk
      succeeded on 115/118 iterations and still scored a loss).
- [x] Phase 8c — Measured the walk's UNAIDED accuracy, since 8b's 33% was
      confounded by that strict-`<` head start. Measurement only; Physarum
      bit-identical (16 interleaved cases, 0 differences, exact float equality
      on per-iteration traces, final D and p vectors).
      **WALK-ONLY EXACT-MATCH vs POOLED 83-87%:**
        draw 2024 freeflow  36.7%   | draw 8080 freeflow  46.7%
        draw 2024 BPR heavy  3.3%   | draw 8080 BPR heavy 16.7%
      Walk produced ANY path on 76.7%/83.3% (freeflow), 53.3%/70.0% (heavy).
      **Tie rate (walk reached the pooled optimum but lost on strict `<`):
      25-29% of fallback-attributed pairs under freeflow, and EXACTLY 0% under
      congestion.** So the bookkeeping artifact explains about a THIRD of the
      freeflow deficit and NONE of the congestion deficit.
      **VERDICT (b): Physarum's apparent competence substantially DEPENDS on
      `theta_subgraph_fallback`. Phase 5's 87% is not a measure of the model's
      own extraction.** This is a reportable finding, not something to tune away.
      Mechanism: the pressure field is solved on an UNDIRECTED projection while
      an extracted path must be DIRECTED-legal on a network with mean out-degree
      2.46, pervasive one-ways and cul-de-sacs — the flux says "flow this way",
      the road says "not in that direction". Failure split (8080 freeflow)
      exact/wrong-path/no-path = 14/11/5, so dead-ending and wrong-path are both
      first-order. Median Dijkstra hops 30 (exact) / 49 (wrong) / 44 (no-path):
      it degrades with path length.
      **PAIRS WITH THE ACO FINDING INTO A TWO-SIDED RESULT: the two bio-inspired
      methods fail in OPPOSITE directions under congestion.** ACO improves
      (congestion flattens the cost surface, more routes land within 10% of
      optimal); Physarum degrades (46.7% -> 16.7%). See
      `docs/tuning/physarum_extraction_provenance.md`.
      Measured but NOT applied: a prefer-walk-on-tie policy would move the
      freeflow provenance share 33% -> ~50% WITHOUT changing a single reported
      cost (the 10 denied ties land at iterations 25-65), and would recover
      nothing under congestion. It changes `best_cost` semantics, so it is a
      deliberate open option rather than a silent fix.
      CAVEAT: 8c measured freeflow and BPR on the `config_speeds` graph. The
      sweep's SUMO / `sumo_speeds` cells are UNMEASURED — hence Phase 8d.
- [x] Phase 8d — Walk-only columns plumbed into the schema. `physarum.py`
      UNTOUCHED (verified: `git diff --stat` on it is empty).
      Four nullable fields, grouped as `WALK_FIELDS` inside `DIAGNOSTIC_FIELDS`
      so Phase 9 has a named group it must never pool with `stretch`:
      `walk_best_cost`, `walk_best_stretch`, `walk_success_count`,
      `post_disruption_walk_stretch`. Null on ACO/classical rows, where null
      means "this algorithm has no separable extraction step".
      `walk_best_stretch` uses the SAME per-cell Dijkstra `reference_cost` as
      the pooled `stretch` (and the disrupted reference for the recovery twin),
      so the two are directly comparable within one row.
      **SUBTLETY THAT WOULD HAVE SILENTLY MISFILED DATA:** Physarum's walk
      tracker is reset inside `apply_disruption` (`_reset_run_state`), so reading
      it once at the end of `run_and_collect` would have filed RECOVERY numbers
      under the INITIAL-solve columns. `_run_into` therefore captures twice —
      right after the initial `run_to_convergence` (before the no-path early
      return, so a walk that found nothing still records
      `walk_success_count`), and again after the recovery passes its
      removed-edge check.
      Divergence invariant asserted: `walk_best_cost >= path_cost` when both
      present. The pooled best is a min that already INCLUDES the walk, so a
      lower walk cost means the two trackers are no longer measuring the same
      run. Also `walk_best_stretch >= 1.0`, and the three initial-solve columns
      must move together (`count == 0` with a cost raises; `count > 0` with a
      null cost raises unless failed).
      `post_disruption_walk_stretch` is enforced null for S0 but deliberately
      kept OPTIONAL on a successful disruption row rather than added to
      `RECOVERY_FIELDS` — a Physarum walk can legitimately dead-end through an
      entire recovery, which `RECOVERY_FIELDS`' must-not-be-null half would have
      forbidden.
      **THE COLUMN EARNS ITS PLACE ON THE SWEEP'S OWN GRAPH.** Sample row,
      `sumo_speeds` + real SUMO heavy + S4 warm: pooled `stretch = 1.0000`
      attributed to `full_graph_fallback` (plain Dijkstra), while
      `walk_best_stretch` is NULL with `walk_success_count = 0` — the model's own
      extraction never completed a path at all. Without these columns that row
      reads as a perfect Physarum result.
      Also noted: SUMO-heavy-on-`sumo_speeds` looks KINDER to the walk than
      Phase 8c's config_speeds/BPR-heavy (4 of 5 sample rows were genuine
      `max_flux_walk` wins at stretch 1.0, vs 8c's 3.3-16.7%), which is itself
      why the sweep had to carry its own measurement rather than inherit 8c's.
      GOTCHA: `generate_disruption(G, source, target, snapshot, scenario_id,
      seed, ...)` has `scenario_id` FIFTH, and callers must check `dis.ok`
      before use — passing a `ok=False` Disruption through yields
      `SchemaViolation: scenario 'S4' recorded 0 removed edges`.
- [x] Phase 9 — Analysis, figures, and results dashboard. COMPLETE.
      All 7 figures regenerate from the Parquet; `docs/results/summary.md` holds
      7 tables with CIs on every number; the dashboard is published at
      https://claude.ai/artifact/2ZPysszxaGhKW2a5712vbH (private to the owner).
      **9a** (`viz/stats.py`, `viz/figures.py`, `scripts/04_make_figures.py`):
      figures 2/3/4/7 + stats bundle. Paired Wilcoxon on identical
      (od, scenario, traffic, seed) tuples; two-stage clustered bootstrap
      (resample OD pairs, then seeds within them, seed 20260101, 10k draws).
      Pre-disruption quality is DEDUPLICATED on the initial solve — it is
      identical across all 7 scenarios and both start modes, so pooling raw rows
      would count one solve up to 13x and shrink every CI by bookkeeping alone.
      **9b** (`viz/traces.py`, `viz/maps.py`, `scripts/06_make_traces.py`):
      figures 1/5/6. The Parquet has NO per-iteration traces and NO path
      geometry, so these three are regenerated under the sweep's own config and
      seeds and are labelled as such in every caption. Cross-check vs the
      Parquet: **64/64 values match to 1e-6**, ACO bit-for-bit per seed.
      **9c** (`viz/export.py`, `scripts/05_export_web.py`, `web/`): dashboard.
      All NUMBERS come from the Parquet (complete coverage); only route GEOMETRY
      is regenerated (30 pairs x S1/S4/S6 at heavy, 89/90 cells — the 1 skip is
      the same infeasible S4 cell the sweep itself failed on). 1.90 MB bundle.
      FIG 1 IS THE BEST SINGLE PIECE OF EVIDENCE FOR THE PROVENANCE FINDING: in
      2 of 3 panels Physarum's with-fallback curve sits at stretch 1.0 from
      iteration 1 — it was Dijkstra's answer before the model did any work.
      Figure 6 caption GOTCHA: "61% of SUMO edges see no vehicle" is a
      per-SUMO-EDGE statistic. Per GRAPH edge it is 24% (light) / 12% (heavy) —
      stating the 61% per graph edge would be wrong by ~5x. netconvert splits
      what OSMnx merges, so a graph edge reads as congested if ANY of its
      segments carried traffic. The two counts match the snapshots' own
      `n_fallback_edges` exactly.
      **HARD REQUIREMENT: never pool Physarum's stretch across provenance.**
      Report a model-only series (`max_flux_walk`) and a with-fallback series,
      separately and labelled, using `best_path_extraction_method` and
      `walk_best_stretch`. A pooled Physarum stretch of 1.0 can mean "Dijkstra's
      answer via the fallback" (see Phase 8b/8c), and pooling it would make the
      Physarum-vs-Dijkstra comparison partly circular — the single most
      attackable claim in the writeup. Phase 8e measured exactly this on the
      sweep: 90% pooled exact-match vs 23.3% walk-only, with median stretch
      1.000 in ALL THREE provenance classes, so pooling erases the whole effect.
      **THREE MORE HARD REQUIREMENTS, all from Phase 8e:**
      1. **Never report Physarum's warm-vs-cold recovery as a ratio of medians**
         — that yields a bogus "24x". The distribution is BIMODAL: 48.3% of warm
         recoveries take exactly 1 iteration. Report the PAIRED median ratio
         (2.97x iterations / 6.87x wall-clock) and SHOW the bimodality; "warm
         start recovers in a single iteration on half of all disruptions, which
         cold start never does" is both truer and stronger than any ratio.
      2. **Do not repeat "the two methods fail in opposite directions under
         congestion".** It held on BPR and REVERSES on SUMO (ACO within-10%:
         81.3% light -> 58.0% heavy, paired p=0.036). Every congestion-direction
         claim must name its congestion model.
      3. Use MEDIAN + IQR and PAIRED tests keyed on identical
         (od, scenario, traffic, seed) tuples — the sweep is fully paired by
         construction, so an unpaired test discards the design.
      **PHASE 9 AUDIT FINDING — `time_to_first_valid_path_iters` IS DEGENERATE
      AND MUST NOT BE REPORTED AS A METRIC.** It is **exactly 1.0 on all 5,012
      disrupted rows, every algorithm, warm and cold, zero variance** (min = p50
      = p99 = max = 1.0). Phase 6 built it to separate "finds a valid route
      fast" from "finds a good one slowly", and on this data it separates
      nothing. The cause is structural, not a null result worth celebrating:
      ACO's ants construct COMPLETE paths from iteration 1, and Physarum's
      extraction falls through to `full_graph_fallback` = Dijkstra, which always
      returns a path — so no router here can score anything but 1.
      Consequence: the project's "defensible metrics" list is
      **iterations-to-adapt and post-disruption path quality**, NOT the three
      originally planned. The honest-reporting note at the bottom of this file
      and `docs/PLAN.md` both still name time-to-first-valid-path; treat those
      as superseded. The column stays in the schema (it is cheap, and a future
      router with a genuine search phase could move it) but it is excluded from
      every reported metric. If the paper wants this measurement, the metric has
      to be REDEFINED — e.g. iterations to first path within X% of optimal —
      which is a new measurement, not a re-reading of this column.
- [x] Phase 9b-mech — **WHY warm start hurts Physarum's walk. My mechanism was
      REFUTED; the real one is more interesting and more damaging.**
      `scripts/measure_physarum_warm_mechanism.py`,
      `docs/tuning/physarum_warm_start_mechanism.md` (pre-registered predictions
      written before any datum), `data/results/physarum_warm_mechanism.jsonl`
      (838 records, tracked). CENSUS, not a sample: all 30 OD pairs x 6 scenarios
      x 2 SUMO levels x {warm,cold} = 358 paired cells + 60 placebo.
      Parquet cross-check 2148/2148. `physarum.py` untouched (`git diff --stat`
      empty); the walk was re-implemented OUTSIDE the router and asserted against
      `last_extraction_method()` on ~40k iterations with zero disagreements.
      **REFUTATION 1 — the closure's location is nearly irrelevant.** A PLACEBO
      closure >=8 hops from every node of the retained corridor reproduces almost
      the whole deficit: warm 43.3% vs cold 70.0% (n=60, discordant 0/16,
      p=3.1e-05), against warm 36.3% vs cold 59.5% for real closures. **Placebo
      vs S1 — same closure size, one far away, one directly on the corridor — is
      INDISTINGUISHABLE: 43.3% vs 41.7%, n=60 paired, discordant 3/2, p=1.00.**
      Spatial locality is flat: warm terminal node to nearest closed edge is a
      median 12 hops, cold also 12 (paired p=0.356); I had predicted <=3 and
      warm << cold. Conductivity near the closure REVERSES: where the warm walk
      fails the severed share of retained D is exactly 0.000, where it succeeds
      0.067 (p=1.8e-12). Residual closure effect pooled over scenarios is ~5-8pp
      of a 23-27pp gap (<= a quarter) and nothing at all against S1.
      **REFUTATION 2 — the "less conductivity overall" rival is ANALYTICALLY
      IMPOSSIBLE, not merely unsupported. THE WALK IS SCALE-INVARIANT IN D.**
      Scaling D by c scales w = D/L by c and therefore p by 1/c, leaving
      Q = w*dp exactly unchanged. I verified this independently: rescaling by
      1e-3 and 1e3 changes the flux by <1e-15 (machine precision), and the agent
      measured bit-identical first post-disruption walks on 6/6 OD pairs. So a
      uniform shortage of conductivity CANNOT cause a dead-end. The empirical
      sign is wrong too: `D_total` is HIGHER where the warm walk fails (24.99 vs
      11.50, p=2.7e-18). **Any future argument that invokes the LEVEL of D to
      explain walk behaviour is wrong by construction — only the SHAPE can
      matter.**
      **THE MECHANISM THE DATA SUPPORTS: the max-flux walk works on a NEAR-
      UNIFORM field and STOPS WORKING AS THE FIELD CONVERGES onto a thin
      corridor.** Cold start's advantage is not that it explores freely relative
      to the closure — it is that it REWINDS the field to the uniform state the
      walk can actually walk. Evidence: whether the walk completes on the FINAL
      PRE-DISRUPTION iteration, graph fully intact, predicts warm recovery walk
      completion on 314/358 = 87.7% (Fisher p=1.4e-50). Convergence alone, with
      no closure anywhere, drops the walk from 69.8% (some iteration) to 43.0%
      (final iteration). Of the 204 cells whose converged field was unwalkable,
      cold rescues 79 and warm rescues 10. Cold's walk gets significantly WORSE
      as its own field converges (23 up / 69 down, p=1.2e-06).
      **CONSEQUENCE FOR PHASE 9 — THE NO-POOLING RULE EXTENDS TO `max_flux_walk`
      ITSELF.** 82.6% of cold's walk successes and **94.6% of warm's** first
      occur on ITERATION 1 (I recomputed both from the raw records; the 94.6%
      is mine, the agent reported only cold's). So a `max_flux_walk` attribution
      is overwhelmingly a FIRST-ITERATION result, not the product of Physarum
      adaptation — on a cold recovery that first iteration is a near-uniform
      field that has done no adaptation at all. Phase 8e RESULT 4's "cold's walk
      is marginally better" therefore has a deflating explanation: it is largely
      a win by a field that has not yet run the model.
      **OPEN, NOT A FINDING:** extraction quality and the convergence criterion
      are in direct tension — `max|dD| < eps` runs the field to exactly the state
      the walk handles worst. `walk_best_iteration()` already records where the
      walk peaked, so testing an earlier stopping rule is cheap. UNTESTED.
      **UNDERDETERMINED, stated as such:** the data cannot separate "concentration
      of D" from "OD path length". `reference_hops` is the strongest cross-cell
      predictor (28 vs 51, p=8.9e-21) and every field descriptor correlates with
      it mechanically; `D_gini` is HIGHER where the walk succeeds, which is
      against the concentration story too. Separating them needs a different
      experiment: fix the OD pair and vary `dt`/`mu` to make more and less
      concentrated fields on the SAME pair.
      RESULT 1 (warm start's 2.97x/6.87x paired speed advantage) is UNTOUCHED.

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

Every `src/bioroute/**` package has an `__init__.py`. **All modules are now
implemented** (that sentence used to say they were docstring-only stubs; that
was true at Phase 0 and is long stale). `viz/` additionally contains
`stats.py`, `traces.py` and `style.py`, which the tree above predates. Read a
module's docstring before writing to it; it states which phase owns it.

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

**STALE-DOC WARNING REMOVED:** this used to say scripts 01-05 did not exist
and that SUMO was not installed. Both are long false. All of
`00_setup_check.py` … `06_make_traces.py` exist, SUMO 1.27.1 is installed at
`C:\Sumo`, and the full pipeline has been run end to end. Additional scripts
not listed above: `scripts/06_make_traces.py` (figures 1/5/6),
`scripts/04_make_figures.py --ieee` and `scripts/06_make_traces.py --ieee`
(IEEE two-column variants into `figures/ieee/`, PDFs tracked), and
`scripts/measure_*.py` (one-off measurement experiments).

## Honest-reporting note

Dijkstra will beat both bio-inspired methods on raw wall-clock recovery — a
recompute on a 5k-node graph is milliseconds, ACO and Physarum take seconds.
This is expected and must be reported, not hidden. The interesting, defensible
metrics are **iterations-to-adapt and post-disruption path quality** — that is
the actual speed-vs-quality trade this project quantifies.
**CORRECTED:** this sentence used to list time-to-first-valid-path as a third
defensible metric. The Phase 9 audit found that column is exactly 1.0 on all
5,012 disrupted rows with zero variance, so it discriminates nothing and is
excluded from every reported metric. `docs/PLAN.md` still names it and is
superseded on this point.
