# Related work — leads for the paper

**Status: SEARCH-DERIVED LEADS, NOT VERIFIED.** Everything below comes from web
search summaries. **No paper here has been read in full.** Every claim
attributed to a source must be checked against the actual paper before it is
cited, and any number quoted from this file without that check is a
fabrication risk. Treat this as a reading list with annotations, not as a
literature review.

Compiled 2026-10-18, before drafting the IEEE report.

---

## Does the novelty claim still hold?

The project's stated contribution (CLAUDE.md, `docs/PLAN.md`):

> no single existing paper combines all three methods on a real Indian city's
> organically-grown road topology under dynamic simulator-generated congestion,
> measuring both routing quality and recovery from disruption.

**Four searches found nothing that combines all of it.** Each component exists
separately; the combination did not surface. Specifically, no result was found
for:

- a three-way Dijkstra / ACO / Physarum benchmark on a *real* road network
  (the three-way comparisons found are on synthetic graphs),
- any bio-inspired routing work on an *Indian* city road network,
- bio-inspired routing + road closure recovery + SUMO in one study.

**Caveat on the strength of this check.** Four searches is a sanity check, not
a systematic review. It does not cover non-indexed venues, Indian journals and
conference proceedings that index poorly, or theses. Before submission this
needs a proper database pass (IEEE Xplore, Scopus, Google Scholar) with
explicit queries recorded. Treat the novelty claim as *not refuted* rather than
as *confirmed*.

---

## The closest prior art, and what to say about it

### 1. Zhang et al. 2014 — improved Physarum (IPPA), shortest path

<https://onlinelibrary.wiley.com/doi/10.1155/2014/487069>

**The single most important paper for us to engage with.** Reportedly compares
Physarum against *both* Dijkstra and ACO, on synthetic graphs of 15–2,000
nodes, and reports that IPPA reaches the optimal path about as reliably as
Dijkstra while beating ACO on accuracy, with ACO degrading as graphs grow.
Runtime reported as O(n³).

**Why this matters more than it looks.** Their headline — Physarum matches
Dijkstra's accuracy — is precisely the claim our Phase 8b/8c/8e provenance
finding complicates. We measured that a pooled Physarum accuracy number
conflates the model's own max-flux extraction with its fallbacks, and that one
of those fallbacks *is Dijkstra*: pooled 90% against 23.3% for the model's own
walk, with median stretch exactly 1.000 inside every provenance class.

**To check when reading it:** does IPPA use a fallback path-extraction step at
all, and if so, is its reported accuracy pooled across extraction provenance?
If it is, our finding generalises beyond our implementation and becomes a
methodological critique of how Physarum results are reported. **If it is not —
if their extraction is unaided — then our provenance problem is a property of
our implementation and must be reported as such.** Do not assume the former.
This is the difference between a contribution and an own-goal, and it rests on
reading their extraction step carefully.

Also note: synthetic graphs have no one-way streets. Our Phase 8c mechanism for
why the walk fails is that the pressure field is solved on an *undirected*
projection while an extracted path must be directed-legal on a network with
mean out-degree 2.46 and pervasive one-ways. That failure mode cannot appear on
their test graphs, which is a clean argument for why a real-road-network
benchmark was needed.

### 2. Adamatzky's slime-mould transport-network series

- M6/M74 motorways: <https://arxiv.org/html/0912.3967>
- Netherlands motorways: <https://arxiv.org/pdf/1209.2974>
- Are motorways rational from slime mould's point of view: <https://arxiv.org/html/1203.2851>
- Roman roads, 3D: <https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5539195/>
- Trans-Canada: <https://arxiv.org/pdf/1105.5084>

Physarum on *real* transport geography, but doing **network design /
approximation** — does the slime reproduce the motorway network — rather than
**point-to-point routing under congestion with recovery**, which is our task.
This is the cleanest "adjacent but different problem" framing available.

One directly relevant detail: the M6/M74 work includes a contamination
experiment where part of the network is destroyed and later recovers, and the
authors report the response as exploring uncolonised territory plus restoring
abandoned links — but also that their particle model showed *no dramatic*
reconfiguration, which they attribute to omitting Physarum's oscillatory
behaviour. So prior Physarum disruption work exists, is qualitative, and the
authors flag their own model as under-responsive. Our warm-vs-cold recovery
measurement is quantitative and paired, which is a defensible delta.

### 3. ACO on OpenStreetMap road networks

- AntScout — dynamic routing on OSM with ant colonies:
  <https://link.springer.com/chapter/10.1007/978-3-642-41019-2_5>
- ACO with link travel-time predictions, tested on OSM graphs of Leuven,
  Antwerp and Ghent: <https://ieeexplore.ieee.org/document/6008852/>
- Survey, ACO for shortest path on road networks:
  <https://www.academia.edu/80380151/>

Closest ACO prior art: real OSM road graphs, traffic-aware, European cities. No
Physarum, no disruption recovery, no Indian city. These establish that ACO on
OSM is not itself novel — our ACO is a *baseline within a comparison*, and the
paper should say so plainly rather than overclaiming it.

### 4. The ACO variant that contradicts our ablation

<https://www.pmf.ni.ac.rs/filomat-content/2018/32-5/32-5-13-6623.pdf> — a
traffic-network ACO guiding ants with pheromone *plus a heuristic favouring
edges pointing toward the destination*, reporting good-quality solutions.

**That is exactly our goal-directed ablation, and we measured it as not
helping.** CLAUDE.md Phase 4: identical to purely-local under free-flow and
*worse* under BPR heavy, because the heuristic is a free-flow great-circle
bound that steers ants toward geometrically direct routes exactly when those
are the congested ones. Figure 5 shows the same effect without any heuristic —
the bio-inspired routes are shorter in hops but slower.

This is a concrete point of engagement rather than a conflict to hide: their
result is plausibly correct on networks where congestion is not
geometry-correlated. Reconciling the two is a genuine discussion-section
contribution. **Check first whether their evaluation includes congestion at
all** — if it is free-flow only, our results do not contradict theirs, they
extend them, and the paper must say that instead.

### 5. Resilience / disruption baselines (non-bio-inspired)

- Short-term road network resilience restoration, validated on a SUMO-built
  network, bi-objective over 1–5 disrupted links:
  <https://www.researchgate.net/publication/415250272_>
- ResiliFlow, open transport world model, resilience curves from targeted vs
  random link removal: <https://arxiv.org/pdf/2608.20709>
- Quantifying urban road network vulnerability and resilience to attacks:
  <https://arxiv.org/pdf/2010.09887>

Useful for framing the disruption design and for citing the convention of
1–5 simultaneous link removals, which our S1/S3/S4 ladder matches. None are
bio-inspired. The first is a good citation for "SUMO-built network used to
validate a resilience framework" being accepted practice.

---

## What to do with this before writing

1. **Read Zhang 2014 properly first.** Its extraction step determines whether
   our headline provenance finding is a general critique or a local defect.
   Nothing else in the reading list changes the paper's framing as much.
2. Check whether the goal-directed ACO paper evaluates under congestion.
3. Run a systematic database pass and record the queries, so the novelty claim
   in the paper is backed by a reproducible search rather than by four
   ad-hoc web searches.
4. Only then write the related-work section.
