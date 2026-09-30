# Phase 2d — SUMO demand recalibration and the shared free-flow baseline

Two commissioned fixes to the SUMO congestion layer, landed before Phase 8's
sweep, plus one bug found while measuring them.

* **Job 1** — the old `period_heavy: 0.6` produced *gridlock*, not congestion.
* **Job 2** — "free-flow" meant two different things, so SUMO and BPR ratios
  were not comparable.
* **Found while measuring** — the edgedata→graph-edge travel-time
  aggregation was dimensionally wrong, understating 26 % of edges badly
  enough to break A*'s optimality guarantee.

Everything below is measured, not estimated.

Machine: SUMO 1.27.1 at `C:\Sumo`. Net = `data/sumo/net.net.xml`
(20,587 non-internal edges). Graph = 7,015 edges.

**Read the aggregation-bug section first** — every congestion number in
this document is post-fix, and the pre-fix numbers were wrong by up to 2×.

---

## The aggregation bug (found while measuring Job 2)

`network/ids.py::compute_edge_travel_times` computed a graph edge's
congested travel time as the **length-weighted mean of its matched SUMO
segments' `traveltime`**. That answers "how long does it take to cross *one
of* my matched segments", not "how long does it take to cross *this graph
edge*".

netconvert splits an OSM way at every interior shape node, and OSMnx's
`simplify=True` merges consecutive ways, so one graph edge routinely matches
a great deal of SUMO road. Measured on this network — total matched SUMO
length ÷ graph-edge length:

| p1 | p10 | p25 | **p50** | p75 | p90 | p99 | max |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.04 | 0.53 | 0.78 | **1.44** | 3.27 | 8.93 | 50.1 | 308 |

Wherever that ratio exceeds 1, the old rule understated the edge's travel
time by roughly that factor. 55 % of matched edges were above 1.25×, 17 %
above 5×.

**How it showed up.** Under the old rule, **1,794 of 7,015 graph edges
(26 %)** came out with SUMO travel times implying speeds above the fastest
speed limit anywhere in the network — the worst by a factor of **266**.
That silently broke `AStarRouter`, whose heuristic is great-circle distance
÷ the global maximum edge speed and is admissible only if no edge is
traversed faster than that. `test_astar_matches_dijkstra_cost_under_sumo_weights`
began failing the moment the heavy edgedata changed; it had been passing by
luck of the sampled OD pairs, and SUMO *light* (untested) was already
violating optimality on 4 of 30 pairs.

**The fix.** Work in **pace** (seconds per metre), which is scale-free:

```
tt(graph edge) = graph_length x  sum_i tt_i / sum_i L_i
```

i.e. the graph edge's own length times the length-weighted mean pace of its
matched segments. This is the exact analogue of the free-flow rule added
for Job 2 (`annotate_speeds_from_sumo`: total length over total time), so a
congestion ratio on an edge is now a clean ratio of two speeds derived by
the same rule.

**Effect.** Edges implying a speed above the network's global maximum, and
A* vs Dijkstra cost equality over the same 30 OD pairs:

| graph | level | before: edges over max (worst) | after | A* ≠ Dijkstra before / after |
|---|---|---|---|---|
| config_speeds | light | 1770 (266×) | 321 (1.71×) | 4/30 → **0/30** |
| config_speeds | heavy | 1794 (266×) | 242 (1.72×) | 3/30 → **0/30** |
| sumo_speeds | light | 720 (160×) | **1** (1.02×) | 1/30 → **0/30** |
| sumo_speeds | heavy | 661 (160×) | **1** (1.03×) | 0/30 → **0/30** |

The single residual edge on the shared baseline is a vehicle exceeding its
speed limit by 3 % (SUMO's `speedFactor` distribution), which is physical
and harmless. Note the second column of that table is also an argument for
Job 2 independently: pairing SUMO with the *SUMO-derived* baseline takes
residual heuristic inadmissibility from 242 edges to 1.

`_AGGREGATION_VERSION = "v2-length-weighted-pace"` is folded into
`SUMOProvider`'s cache key so no pre-fix snapshot is ever reused.

> **Note for whoever owns `algorithms/`** (out of scope here, not touched):
> `algorithms/classical.py`'s module docstring asserts that "congestion only
> ever *increases* edge travel time above the free-flow value `speed_kph`
> implies, so a heuristic based on free-flow max speed remains a valid lower
> bound under any TrafficSnapshot". That is **false** for a SUMO snapshot
> read against the config-speeds graph — 242 edges still violate it after
> the aggregation fix, because SUMO's own speed limits are faster than the
> Bengaluru table. It is true (to within 3 % on one edge) against the
> `sumo_speeds` baseline. The docstring should be narrowed to say so.

---

## Job 1 — recalibrating `period_heavy`

### Protocol

`randomTrips.py --period P --seed 43 -b 0 -e 3600 --validate
--fringe-factor 5 --vehicle-class passenger`, then headless `sumo` to
`sim_end`, 300 s `edgeData` bins. Net and seed held fixed; only `--period`
varies. `period_light: 3` is included as the reference point, at its own
seed 42.

Congestion ratio = mean over all 7,015 **graph** edges of (snapshot travel
time ÷ the `sumo_speeds` baseline graph's free-flow travel time), after the
Phase 2c edge-ID join — reported length-weighted, and also as the mean cost
over 100 random OD paths (seed 123), which is what a router experiences.

### The sweep (sim_end = 5000 throughout)

| period | inserted | teleports | teleport % | Jam | Yield | WrongLane | running at end | running % | mean speed m/s | mean trip s | **ratio, edge len-wtd** | **ratio, 100 OD paths** | median edge ratio |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.6 (old heavy) | 5980 | 1825 | **30.52 %** | 1234 | 555 | 36 | 2422 | 40.5 % | 6.64 | 816.1 | 2.964 | 2.970 | 1.419 |
| 0.8 | 4499 | 522 | 11.60 % | 373 | 130 | 19 | 1157 | 25.7 % | 7.56 | 707.6 | 2.191 | 2.334 | 1.383 |
| 0.9 | 3998 | 366 | 9.15 % | 257 | 85 | 24 | 766 | 19.2 % | 7.39 | 514.7 | 2.073 | 2.241 | 1.377 |
| 1.0 | 3599 | 280 | 7.78 % | 205 | 61 | 14 | 619 | 17.2 % | 7.77 | 664.0 | 1.929 | 2.152 | 1.363 |
| **1.2 (chosen)** | 3000 | 154 | **5.13 %** | 108 | 41 | 5 | 367 | 12.2 % | 7.90 | 612.6 | **1.846** | **2.108** | 1.361 |
| 1.5 | 2400 | 45 | 1.88 % | 40 | 4 | 1 | 151 | 6.3 % | 8.43 | 546.7 | 1.697 | 1.989 | 1.340 |
| 2.0 | 1800 | 3 | 0.17 % | 1 | 2 | 0 | 39 | 2.2 % | 8.95 | 480.0 | 1.575 | 1.840 | 1.321 |
| 3.0 (= light) | 1200 | 0 | 0.00 % | 0 | 0 | 0 | 0 | 0.0 % | 9.38 | 420.6 | 1.546 | 1.813 | 1.298 |

`loaded` equals `inserted` at every setting except 0.6, where 20 of 6,000
vehicles never made it onto the network at all.

### Chosen: `period_heavy: 1.2` — both targets met

* teleports **5.13 %** of inserted (154 of 3,000; 108 Jam) — meets the
  ~5 % target.
* congestion ratio **2.11×** on OD paths, **1.85×** length-weighted over
  graph edges — meets the ~2× target.
* mean trip duration 612.6 s vs 420.6 s at light (+46 %); mean speed
  7.90 m/s vs 9.38 (−16 %); mean time loss 422 s.

**Honesty note on how this conclusion was reached.** The sweep was run
*before* the aggregation bug was found. On the pre-fix numbers the two
targets looked mutually unreachable (2× only at 30 % teleports) and 1.2 was
being written up as a compromise that traded away congestion intensity. The
fix dissolved most of that trade-off: the old aggregation suppressed the
congestion signal roughly in proportion to how finely a graph edge was split
into SUMO segments, which hit the busiest, most-split arterial corridors
hardest. The pre-fix figures are not reported above because they were
simply wrong, not because they were inconvenient.

What remains of the trade-off is real but small: `period 0.9` would buy
2.07× / 2.24× for 9.15 % teleports. 1.2 is preferred because a teleport
*falsifies* the edge travel times a vehicle skipped, and 2.11× on OD paths
already satisfies the target.

`period 1.0` (7.78 % teleports, 1.93× / 2.15×) is the nearest alternative:
it costs 52 % more teleports for 4 % more congestion.

### Honest caveat for Phase 8 — heavy vs light contrast is modest

Light sits at 1.546× / 1.813× and heavy at 1.846× / 2.108×. In
excess-delay terms that is 54.6 % vs 84.6 % above free-flow (edge,
length-weighted) — a real but not dramatic separation, and the median edge
barely moves (1.298 → 1.361).

Two reasons, both structural rather than fixable by tuning:

1. **The free-flow baseline has no signal delay.** It is
   length ÷ speed limit, while `--tls.guess-signals` puts traffic signals
   across this network. So *even zero-congestion* traffic reads well above
   1.0 — light's 1.55× is mostly signals, not congestion. The light↔heavy
   *difference* is the congestion signal; the absolute level is not.
2. **Most of the network is empty at both levels.** 68 % of SUMO edges see
   no vehicle at light, 61 % at heavy; 1,616 and 812 graph edges
   respectively fall back to free-flow for want of any observation.

Any Phase 8 conclusion that needs heavy to be a qualitatively different
cost surface from light must justify itself against these numbers.
`period_light` was deliberately **not** re-tuned to widen the gap; moving it
would have been tuning the result rather than the instrument.

### `sim_end` stays at 5000 — and heavy does *not* fully drain

Light drains completely at 5000 (0 running). Heavy at period 1.2 does not:
367 vehicles (12.2 %) are still circulating. Extending the clock was
measured, not assumed:

| sim_end | teleports | teleport % | running at end | running % | ratio (edge, len-wtd) |
|---:|---:|---:|---:|---:|---:|
| **5000** | 154 | **5.13 %** | 367 | 12.2 % | 1.846 |
| 6000 | 184 | 6.13 % | 222 | 7.4 % | — |
| 8000 | 212 | 7.07 % | 84 | 2.8 % | — |
| 12000 | 222 | 7.40 % | **0** | **0.0 %** | 1.851 |

So full drainage **is** reachable, at `sim_end: 12000`. It was not taken:

* The congestion ratio is flat across the whole range (1.846 → 1.851), so
  draining buys no accuracy in the quantity the provider actually consumes.
* It costs the teleport target: 5.13 % → 7.40 %. Demand stops at t=3600, so
  every extra interval is a near-empty network carrying only vehicles that
  were *already* stuck; the jam timer keeps firing on them.
* Teleports and non-drainage are not equally bad. A teleport **falsifies**
  edge travel times — the vehicle never traversed the edges it skipped. A
  still-running vehicle does not: every edge it did traverse contributed
  honest data, and only its own trip-level statistics are truncated. Since
  `SUMOProvider` consumes edge travel times and not trip statistics, the
  setting that minimises teleports is the correct one.
* Light and heavy keep the same clock, which keeps the two levels
  comparable.

This is a deliberate deviation from "heavy should ideally drain too", with
the cost stated: 12.2 % of heavy vehicles have truncated trips in
`tripinfo_heavy.xml`, and the `Statistics (avg of N)` block for heavy covers
2,633 of 3,000 vehicles.

### The 20× clipping is still needed — the gridlock hypothesis was wrong

The expectation was that implausible edge samples came from gridlock, so
that once teleports were rare the `implausible_cap_factor` guard would
become a no-op. **It does not.** Fraction of interval samples clipped, and
what removing the cap does to the length-weighted ratio:

| run | teleports | drained? | samples clipped | ratio with cap | ratio, cap removed | worst single sample |
|---|---:|---|---:|---:|---:|---:|
| period 0.6 | 30.5 % | no | 5,024 / 63,200 = 7.95 % | 2.964× | **52.34×** | 347,532× |
| period 1.0 | 7.8 % | no | 1,519 / 53,501 = 2.84 % | 1.929× | 9.10× | 25,466× |
| period 1.2 | 5.1 % | no | 1,185 / 49,908 = 2.37 % | 1.846× | **14.59×** | 86,942× |
| period 1.5 | 1.9 % | no | 799 / 45,678 = 1.75 % | 1.697× | 3.07× | 12,664× |
| period 3.0 (light) | **0** | **yes** | 488 / 31,817 = **1.53 %** | 1.546× | **2.08×** | **2,141×** |

The bottom row is the point. A run with **zero teleports, zero vehicles
left running, and every trip completed** still produces samples up to
2,141× free-flow, still needs a 1.53 % clip, and still has its ratio
inflated by 34 % if the cap is removed.

Why: SUMO's per-interval `traveltime` is the edge length divided by the mean
speed of whatever vehicles occupied it during that 300 s bin. One car that
entered just before the bin closed, or that sat through a red light, gives
an enormous traveltime from a one-vehicle sample. That is a **sampling
artefact**, present at every demand level and independent of congestion. The
clipped samples are also not marginal: their median is 86–97× free-flow,
far above the 20× cap, so this is a cleanly separated tail rather than a
borderline judgement call.

Conclusion: the guard stays, is not a no-op, and its accounting is now
surfaced on every run — `edgedata_report()` carries `n_interval_samples`,
`n_samples_clipped`, `frac_samples_clipped`, `max_sample_ratio_to_freeflow`
and `cap_factor`, and `scripts/02_run_sumo.py` prints them.

---

## Job 2 — one shared free-flow baseline

### The problem, quantified

SUMO derives free-flow speeds from OSM tags and netconvert's per-highway
defaults; the graph's `travel_time` came from the deliberately conservative
Bengaluru `speeds_kph` table in `configs/area_central_blr.yaml`. SUMO's are
much faster — across the whole graph, **1.75×** faster:

| | config-speeds graph | sumo-speeds graph |
|---|---:|---:|
| mean edge `speed_kph` | 22.74 | 51.49 |
| total free-flow time over all edges | 66,983 s | 38,250 s (×0.5710) |

The consequence was not subtle. Against the old config-speeds baseline —
even *after* the aggregation fix — the SUMO provider's *congested*
snapshots come out at or below free-flow:

| provider / level | ratio vs config-speeds free-flow (100 OD paths) | median edge ratio |
|---|---:|---:|
| SUMO light | **0.894×** | 0.692 |
| SUMO heavy | **1.029×** | 0.643 |

A "congestion" ratio below 1.0 is not a congestion ratio. Any side-by-side
reading of "SUMO N×" against "BPR M×" was comparing two different
denominators.

### The fix

`scripts/02_run_sumo.py` now re-annotates `speed_kph` / `travel_time` on the
cached Phase 1 graph from the speeds in the generated `net.net.xml`, using
the existing Phase 2c edge-ID join, and caches the result as a **separate**
artifact:

```
data/network/central_blr_<areahash>_<baselinehash>_sumospeeds.graphml
```

Phase 1 is untouched. `scripts/01_build_network.py` and
`network/graph.py::build_graph` still need no SUMO, so the graph can be
built without first running netconvert — the backwards dependency the brief
ruled out. The re-annotation is a Phase 2 step consuming the Phase 1
artifact.

**Which mean.** A graph edge can match several SUMO segments. The effective
speed is the length-weighted **harmonic** mean, `sum(L_i) / sum(L_i / v_i)`
— total length over total free-flow time. An arithmetic mean would not
preserve travel time: 100 m at 10 m/s then 100 m at 50 m/s takes 12 s, not
the 6.7 s an arithmetic mean implies. `travel_time` is then the **graph**
edge's own length over that speed, so graph geometry still sets distance.
(The congested side now uses the matching pace rule — see the aggregation
section — so numerator and denominator are derived consistently.)

### Re-annotation coverage

| | edges | % |
|---|---:|---:|
| graph edges | 7,015 | 100 % |
| re-annotated from SUMO speeds | 6,842 | **97.53 %** |
| kept config-derived speed (no SUMO match) | **173** | 2.47 % |

Every edge is stamped `speed_source` = `"sumo"` or `"config"`, so the 173
are explicit, counted and queryable rather than invisible. This is the same
97.53 % as the Phase 2c ID-based join; no geometric fallback was needed.

### Selecting the baseline

`configs/congestion.yaml` gains `baseline_graph:` (`config_speeds` |
`sumo_speeds`), and `network/graph.py::load_baseline_graph(cfg, baseline=…)`
is the explicit loader. **The project default is now `sumo_speeds`.**

The choice is stamped onto the graph object itself (`G.graph["baseline"]`
and `["baseline_hash"]`, which survive the GraphML round-trip), so:

* every `TrafficSnapshot.meta` carries `baseline` and `baseline_hash` — a
  result row records its own denominator;
* `BPRProvider` and `SUMOProvider` fold it into their snapshot **cache
  key**. This is load-bearing: the two graphs describe the same study area
  and therefore share an *area-config* hash, while carrying different
  `travel_time` on every edge. Without it, switching baselines would read
  the other baseline's numbers straight back off disk.

Cache invalidation verified by measurement — the key hash changes:

| provider | config_speeds | sumo_speeds |
|---|---|---|
| BPR | `1175b8695aaf` | `4f912ae4f997` |
| SUMO | `7479bdb732cf` | `bdaa0bbee65e` |

(sumo_speeds `baseline_hash` = `285f18f9a225`; artifact
`data/network/central_blr_9caede2d05c4_285f18f9a225_sumospeeds.graphml`.)

**Baseline artifact hashing.** netconvert opens every net file with an XML
comment holding a generation timestamp and the output path, so two runs
producing a byte-identical network differ as *files* (verified: regenerating
from the same OSM extract and flags differs ONLY inside that comment).
`sumo_net_content_sha256` therefore hashes the net from after that comment,
so a pipeline re-run does not spuriously invalidate the baseline graph and
every snapshot keyed off it — while a genuine change to the network body
still does.

### BPR against the new shared baseline

Protocol identical to `test_bpr_congestion_ratio_bands`: mean cost over 100
random OD pairs (seed 123), `time_bin=1`, divided by the free-flow mean on
the same graph and pairs.

| level | old (config_speeds) | new (sumo_speeds) | target band | in band? |
|---|---:|---:|---|---|
| light | 1.120× | **1.181×** | 1.1–1.3 | yes |
| heavy | 2.187× | **2.534×** | 1.8–2.5 | **no — 0.034 over** |

Heavy drifts marginally above the band ceiling. **Demand was not retuned to
force it back.** The drift has a cause: the BPR ratio is invariant to a
*uniform* rescaling of free-flow times, but this re-annotation is not
uniform — it compresses the speed spread between highway classes (config:
residential 20 vs primary 40 km/h, a 2× gap; SUMO's defaults put both near
50), so assignment routes differently and loads concentrate differently.

Per-edge, length-weighted: light 1.021× → 1.034×, heavy 1.338× → 1.460×.

`tests/test_congestion.py::test_bpr_congestion_ratio_bands` still asserts
the 1.8–2.5 band on the **config_speeds** graph, where it still passes; it
is a property of the Phase 2a calibration against the Phase 1 artifact. A
band assertion on `sumo_speeds` was deliberately *not* added, because it
would have to be written to pass at 2.534 and that is fitting the test to
the number.

### SUMO against the new shared baseline

| level | vs config_speeds (old) | vs sumo_speeds (shared) |
|---|---:|---:|
| light — OD path ratio | 0.894× | **1.813×** |
| light — edge, len-weighted | 0.859× | **1.546×** |
| heavy — OD path ratio | 1.029× | **2.108×** |
| heavy — edge, len-weighted | 0.963× | **1.846×** |

Fraction of edges reading *below* free-flow collapses from 35.3 % / 35.2 %
(old baseline, old aggregation) to **1.2 % / 0.5 %**.

### All four numbers, one denominator

| | light | heavy |
|---|---:|---:|
| BPR — OD path | 1.181× | 2.534× |
| SUMO — OD path | 1.813× | 2.108× |
| BPR — edge, len-weighted | 1.034× | 1.460× |
| SUMO — edge, len-weighted | 1.546× | 1.846× |

The two sources now bracket each other rather than being incomparable, but
they are *not* interchangeable and Phase 9 should not present them as one
"congestion level":

* **SUMO's floor is higher.** Its light level already reads 1.81× because
  the free-flow denominator has no signal delay while the simulation does.
* **BPR's dynamic range is wider.** Its synthetic OD demand loads shortest
  paths, so delay concentrates exactly on the corridors an OD-path
  measurement walks (1.18× → 2.53×); SUMO's `randomTrips` demand spreads
  more evenly and leaves 61 % of edges untouched at heavy
  (1.81× → 2.11×).

Reporting both sources' ratios separately is the honest presentation.

### Caveat on absolute times

The shared baseline is SUMO's, which is 1.75× faster than the Bengaluru
speeds Phase 1 chose. Ratios are now comparable; **absolute** travel times
on the `sumo_speeds` graph are correspondingly optimistic for Bengaluru
(mean edge speed 51 km/h). `configs/area_central_blr.yaml` was not changed,
so the config-speeds graph remains reproducible and remains available via
`baseline_graph: config_speeds` for any result that needs realistic
absolute times rather than cross-provider comparability.
