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

**NAME THE PRIOR THREE-WAY COMPARISON EXPLICITLY, AND NEVER CLAIM "THREE-WAY"
AS THE NOVELTY.** Zhang, Wang, Adamatzky, Chan, Mahadevan & Deng (Sci. World
J., 2014 — section 1 below) already benchmark Physarum against **both ACO and
Dijkstra**. The combination of the three algorithms is therefore NOT new, and a
reviewer who knows that paper will reach for it first. What is new is the
SUBSTRATE and the MEASUREMENT: a real, directed, organically-grown road network
with one-ways and mean out-degree 2.46, simulator-generated congestion at two
demand levels, and recovery from closures — against their Erdos-Renyi random
graphs with static integer weights and no disruption. Two further axes are
untouched by them and by everyone else found so far: **no Physarum + SUMO work
exists at all**, and **no ACO paper found reports recovery after a closure
against a Dijkstra baseline**.

The safe formulation, which survives every paper read so far:

> the first three-way benchmark of classical, ant-colony and Physarum routing
> on a real *road* network with directed constraints and simulator-generated
> congestion, measuring recovery from closures as well as path quality.

**Caveat on the strength of this check — NOW PARTLY SUPERSEDED: see
"Systematic database pass" below, which adds two complete forward-citation
sweeps and structured database queries.** Four searches is a sanity check, not
a systematic review. It does not cover non-indexed venues, Indian journals and
conference proceedings that index poorly, or theses. Before submission this
needs a proper database pass (IEEE Xplore, Scopus, Google Scholar) with
explicit queries recorded. Treat the novelty claim as *not refuted* rather than
as *confirmed*.

---

## The closest prior art, and what to say about it

### 1. Zhang et al. 2014 — improved Physarum (IPPA), shortest path — **READ**

<https://onlinelibrary.wiley.com/doi/10.1155/2014/487069> (Wiley blocks
fetching; open-access full text at
<https://pmc.ncbi.nlm.nih.gov/articles/PMC3984829/>, PMC3984829)

Zhang, Wang, **Adamatzky**, Chan, Mahadevan, Deng. Sci. World J., 2014-03-25.

> **READ 2026-10-18 via PMC full text. The three questions below are answered
> from the body text, not the abstract. Figures 4, 5 and 9 carry the numeric
> comparisons and are images, so no accuracy figures were readable — those
> still need the PDF.**

**ANSWER 1 — THEY HAVE NO FALLBACK, AND NO SEPARATE EXTRACTION STEP AT ALL.**
The path is read straight off the converged flux field by inspecting which
edges carry flux: "the flux of edges (1,2), (2,3), and (3,4) converge to 1
while that of the other edges converge to 0. Therefore, the shortest path
found by the proposed method is 1 → 2 → 3 → 4", and §4.2 "we can follow the
direction of the flow to construct these paths." No conductivity threshold, no
greedy-walk rule, no shortest-path search on a subgraph, and **no fallback**.
Termination is `sum |dD| <= 0.01`.

**THIS KILLS ONE FRAMING AND HANDS US A BETTER ONE.** We cannot claim that
Physarum papers in general report accuracy pooled across extraction
provenance — this one has nothing to pool. The reason it needs no extraction
machinery is that **on its test graphs the flux separates cleanly**: it goes
to 1 on the path and 0 elsewhere, so reading the path off is trivial.

The honest framing is therefore the opposite of a critique of their
bookkeeping, and stronger for us:

> On randomly generated graphs the Physarum flux field separates cleanly
> enough that path extraction is trivial and reported accuracy is effectively
> the solver's accuracy. On a real, directed, organically-grown road network it
> does not: our model's own max-flux walk yields a directed-legal path on only
> 69.8% of runs and matches the optimum on 23.3%, so extraction stops being a
> formality and becomes the step that decides the reported result. Any
> aggregate Physarum accuracy on such a network is therefore a statement about
> the extraction policy as much as about the model, which is why we report the
> two series separately.

Our fallback is not a flaw we are confessing — it exists because on this
substrate something is needed, and the contribution is measuring exactly how
much of the headline number it accounts for.

**ANSWER 2 — comparison numbers.** 15 networks, 15–2,000 nodes, 40 runs each,
random graphs from igraph's `erdos.renyi.game`, integer edge lengths 1–100.
Both IPPA and Dijkstra find optimal paths "with one hundred percent"; ACO's
accuracy "decreases gradually with the increase of the network" and is "very
low", with no numeric rate in the text. Dijkstra is faster than IPPA; IPPA is
faster than ACO. Iteration counts are compared only against the basic Physarum
algorithm, not against ACO or Dijkstra. **Note their ACO result points the
same way ours does** — ACO degrading as the graph grows is consistent with our
path-length stratification, where ACO's within-10% rate falls to 48% on the
longest tercile.

**ANSWER 3 — directedness is never stated, and one-ways are never mentioned.**
The paper does not say whether its test networks are directed, and contains no
mention of one-way edges, directed arcs or directional feasibility.
`erdos.renyi.game` defaults to undirected. The flux equations are signed and
the text says "follow the direction of the flow", which is a directed
*formulation* over an undirected *topology* — exactly our situation, since we
solve the pressure field on an undirected projection too.

**This is the cleanest possible support for why the benchmark had to be run on
a real road network.** Our Phase 8c mechanism — the flux says "flow this way",
the road says "not in that direction", on a graph with mean out-degree 2.46 and
pervasive one-ways — is a failure mode that cannot occur on an Erdős–Rényi
graph with no directional constraints. The prior three-way comparison could not
have found it.

Minor, worth a footnote only: the paper says "we make the network fully
connected" while Table 1 lists sparse edge counts (23 edges on 15 nodes, where
complete would be 105), and does not reconcile the two.

#### Figures 4, 5 and 9 — **READ 2026-10-18. ITEM CLOSED.**

Not from the PDF: the figures are raster images, so PDF text extraction was
never going to work. I downloaded the figure JPEGs that PMC serves alongside
the article (`cdn.ncbi.nlm.nih.gov/pmc/blobs/2519/3984829/...`) and read them
directly. Values below are read off the plotted points and are approximate to
about +/- 0.02 on the accuracy axis.

**FIGURE 9 — degree of accuracy vs number of nodes. THIS IS THE ONE THAT
MATTERS, AND IT IS A DEFENSIVE RESULT FOR US.**

| nodes | ~15 | 30 | 50 | 80 | 100 | 200 | 400 | 500 | 800 | 1000+ |
|---|---|---|---|---|---|---|---|---|---|---|
| ACO | 0.57 | 0.45 | 0.22 | 0.30 | 0.15 | 0.20 | 0.08 | 0.00 | 0.10 | **0.00** |
| IPPA | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | **1.0** |
| Dijkstra | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | **1.0** |

ACO is flat at **exactly zero from 1,000 nodes through 2,000**, with 1,200,
1,500, 1,800 and 2,000 all on the axis.

**WHY THIS IS THE MOST USEFUL SINGLE NUMBER IN THE LITERATURE FOR US: IT
PRE-EMPTS THE FIRST ATTACK ON OUR ACO RESULTS.** The obvious reviewer response
to "ACO misses its exit criterion" and to any ACO-vs-Physarum gap is *your ACO
was a strawman*. The only published three-way comparison of these same three
algorithms reports its ACO at **0% optimal-path accuracy on 1,000-2,000-node
graphs**. Ours, on a **2,856-node** real road network — larger than their
largest, and far harder, being directed with mean out-degree 2.46 — reaches
**43-44% exact match and 73.3-81.3% within 10% of optimal**. Our ACO is not a
weak baseline; by the standard of the prior art it is a conspicuously strong
one, and that is what licenses us to report its missed criterion as a finding
about ACO rather than about our tuning.

State it as a CONTRAST, never as a controlled comparison: different graph
family (Erdos-Renyi vs road network), different edge-weight distribution
(integers 1-100 vs congested travel times), and their "degree of accuracy" is
an exact-optimum rate whose definition we are inferring. The direction and the
order of magnitude are what carry.

Their ACO parameters, for the record: alpha=2, beta=5, rho=0.1, colony size
and iteration cap both equal to the node count. That last choice is likely
much of the story — a 2,000-ant colony for 2,000 iterations on a sparse ER
graph, with no backtracking construction and no dead-node handling, is close
to the plain-MMAS configuration that gave us a **0% ant completion rate** in
Phase 4 before backtracking construction was added.

**FIGURE 5 — executing time (log axis).** At 2,000 nodes, roughly: ACO
~1.5e5 s, IPPA ~4e2 s, Dijkstra ~1.5e1 s. **The ordering Dijkstra < Physarum <
ACO is exactly ours**, which is worth one sentence of corroboration for the
honest-reporting note. **The absolute values are not comparable to ours and
must not be quoted as if they were:** their Dijkstra takes ~15 seconds on a
2,000-node graph where ours takes ~31 ms on 2,856 nodes, a ~500x discrepancy
that is about their implementation and timing protocol, not about Dijkstra.
Use the ratios if anything: theirs is ACO/Dijkstra ~1e4 and Physarum/Dijkstra
~27; ours is ACO/Dijkstra ~1,100-1,900 and Physarum/Dijkstra ~7.6 (warm).

**FIGURE 4 — iterations, IPPA vs BPPA (the basic Physarum model).** IPPA is
flat at ~25-28 iterations across the whole 15-2,000 range; BPPA rises from ~65
to ~200, non-monotonically. **BPPA is the canonical model, which is what WE
implement**, and our Physarum's iteration counts sit squarely in that band —
median 98 to converge an undisrupted solve, 98 cold / 42 warm on recovery. So
our implementation's convergence cost is consistent with the published basic
model, which is a useful implementation-sanity citation. Their IPPA's flat ~25
is a claim about their improvement, not about ours, and we do not implement
IPPA.

### 1b. The same group's DIRECTED variant — **CHECKED. IT EXISTS.**

Zhang, Zhang & Deng, "An improved bio-inspired algorithm for the directed
shortest path problem", *Bioinspiration & Biomimetics* **9**(4) 046016, 2014.
DOI <https://doi.org/10.1088/1748-3182/9/4/046016>. Record:
<https://research.polyu.edu.hk/en/publications/an-improved-bio-inspired-algorithm-for-the-directed-shortest-path/>
Precursor: Zhang, Zhang, Zhang & Deng (2012) modified the Physarum model for
directed graphs, "noting that the classical algorithm could not handle
directed graphs".

**TWO CONSEQUENCES, AND THE FIRST ONE IS GOOD FOR US.**

**(a) The literature itself states the limitation we measured.** The abstract
describes the Physarum path-finding model as one "which is designed only for
undirected networks". So our Phase 8c mechanism — the pressure field is solved
on an undirected projection while an extracted path must be directed-legal —
is **not a strawman we invented and not a defect of our implementation**. It is
the canonical model's acknowledged scope, confirmed in print by the group that
went on to extend it. That is a citation we should use, and it removes the
"you implemented it wrong" line of attack entirely.

**(b) WE CANNOT CLAIM DIRECTEDNESS IS AN UNADDRESSED GAP, AND WE MUST DISCLOSE
THAT WE DID NOT IMPLEMENT THE DIRECTED VARIANT.** A directed Physarum
formulation has existed since 2014. Our benchmark uses the canonical
undirected-projection model. A reviewer will ask why, and the paper must answer
before they ask. The honest statement:

> We benchmark the canonical Tero-style conductivity model, whose pressure
> field is defined on an undirected projection. A directed formulation via
> analog circuit analysis exists (Zhang, Zhang & Deng 2014); we did not
> implement it, so our extraction results characterise the canonical model on a
> directed road network and should not be read as a limit on Physarum-style
> methods in general. Benchmarking the directed formulation on this network is
> the obvious next experiment.

**What survives, and it is still worth reporting.** Their paper reports
"illustrative examples"; it is not a quantitative benchmark on a real network
under congestion with disruption. Our contribution is the measurement — 5,520
runs, paired statistics, and the provenance split — not the observation that
directedness is hard. **Reframe the extraction finding from "we discovered the
model cannot handle one-ways" to "we quantified what the canonical model's
undirected projection costs on a real directed road network, which the
literature flags qualitatively and had not measured."**

Not yet read in full (paywalled at IOP; Scopus record
<https://www.scopus.com/pages/publications/84914125447>, 6 citations). Worth
getting through an institutional login before submission, to check whether
their directed model would even be applicable here and what its cost is.

### 1c. Physarum applied to traffic assignment

"A modified Physarum-inspired model for the user equilibrium traffic assignment
problem" — <https://arxiv.org/pdf/1612.06174>

Physarum applied to *traffic*, which is adjacent to our setting, though it
solves user-equilibrium assignment rather than point-to-point routing. Relevant
to the related-work framing and to the BPR side of Phase 2d, since UE
assignment is exactly what our BPR provider approximates. Unread.

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

### 4. The goal-directed ACO variant — **READ IN FULL. IT DOES NOT CONTRADICT US.**

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
geometry-correlated.

> **READ IN FULL 2026-10-18** (pypdf text extraction of the Filomat PDF).
> Citation: Shuijian Zhang, Xuejun Liu, Meizhen Wang, "A Novel Ant Colony
> Optimization Algorithm For The Shortest-path Problem In Traffic Networks",
> *Filomat* **32**(5), 2018, 1619-1628, doi:10.2298/FIL1805619Z.

**ANSWER: THEIR EVALUATION HAS NO CONGESTION, SO THERE IS NO CONTRADICTION TO
RECONCILE.** The formalism is nominally time-varying — the network is defined as
`G:{V, E, C, W(i,j)(t)}` where `W` is "a set of weights of the road arcs at
time t ... measured using the travel time of the road arcs" — but **nothing in
the evaluation ever varies with t**. The case study is a single 528-node /
906-arc traffic network digitised in ArcGIS 10.2, with static per-arc travel
times, no traffic demand, no simulator and no time-dependence exercised
anywhere. **Our results EXTEND theirs rather than contradict them, and the
paper must say exactly that.**

**AND THE MECHANISM IS NOT THE SAME AS OUR ABLATION — I had this wrong.**
Their orientation guidance is an **angular alignment** term, not a
distance-to-target bound: `u_ij = 1/(1 + theta)` where theta is the included
angle between the road arc and the straight line from the current node to the
destination, entering the transition rule as a third factor with its own
exponent gamma (`tau^alpha * eta^beta * u^gamma`, with gamma=2 for the novel
variant and gamma=0 for the baseline). **Our ablation instead folds A*'s
great-circle TIME bound into the visibility, `eta = 1/(tt + h(v))`.** Both
steer ants toward the destination, but one scores direction and the other
scores remaining cost, so our measured result is not a direct refutation of
theirs even in spirit.

Their visibility also differs from ours: `eta_ij = d_ij / w_ij`, i.e. the
arc's **average speed**, where ours is `1/travel_time`. Worth one sentence,
since it is a defensible alternative we did not try.

**What they actually compare is novel ACO (gamma=2) against traditional ACO
(gamma=0) on one network, "tested successfully several times", with Figure 7
showing a single run. There is NO Dijkstra baseline, no optimality rate, no
disruption and no recovery.** So the honest statement is: the only published
evidence for orientation guidance helping on a traffic network is a
single-network, congestion-free, ACO-vs-ACO comparison against no ground
truth; we measure a related (not identical) goal-directed term under two
congestion models and find it neutral under free-flow and harmful under BPR
heavy, which is new information rather than a dispute.

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

### 6a. The transition-point paper — **READ IN FULL. THIS IS THE ONE.**

"When does the Physarum Solver Distinguish the Shortest Path from other Paths:
the Transition Point and its Applications" — <https://arxiv.org/pdf/2101.02913>
(IEEE Transactions submission format). **Read 2026-10-18 from the PDF.**

**THEIR "D-PATH" IS OUR `max_flux_walk`. Same algorithm, independently
named.** Their procedure, quoted: starting from the start node, "find all the
nodes and their corresponding edges that are connect to the current last
element", then "among the edges found in the second step, find the edge that
contains the maximum flow", move there, and "delete the current last element
of ListNode and its corresponding edges from the graph" — i.e. a greedy
maximum-flux walk with a visited/tabu rule, repeated "until the next element we
found in Step iv is the ending node".

**THE CLAIM WE CONTRADICT, in their words:**

> "after the convergence of the OPPA, the algorithm will direct most (or even
> all) of the inflow to the shortest path; thus, **when the OPPA converges, the
> D-Path generated is exactly the shortest path of the graph**"

**We measured that on a real directed road network and it is false there.** At
the convergence iteration the max-flux walk completes a path on **43.0%** of
solves and matches the optimum on **23.3%** (Phase 8e / 9b-mech). Their entire
proposed termination criterion (OPPA-D) rests on the D-Path having converged to
the shortest path and *staying* there — they report `L_D-Path` "converges to the
length of the shortest path" well before the conductivity criterion fires.
**Phase 9b-mech found the opposite tail behaviour: the walk is best early and
DEGRADES as the field converges** (completion 69.8% at some iteration → 43.0%
at the final one; cold's walk gets significantly worse as its own field
converges, p=1.2e-06).

**WHY BOTH CAN BE TRUE — and this is the paper's cleanest contribution.**
Look at what they test on:

| Their networks | Size | Degree |
|---|---|---|
| Motivating demo | **5 nodes, COMPLETE** | 4 |
| `Da-Com-1..8` | 50–5,000 nodes, **complete** | up to 5,000 |
| `Da-SW-1..8` | 50–5,000, Watts–Strogatz | **mean degree 6 to 600** |
| `Da-R-1` Sioux Falls | 24 nodes, 76 edges | ~3.2 |
| `Da-R-2` Anaheim | 416 nodes, 914 edges | ~2.2 |

**Ours: 2,856 nodes, 7,015 edges, mean out-degree 2.46, pervasive one-ways.**

And the governing assumption, quoted: *"Note that G is assumed to be an
**undirected** connected graph without cycles and negative-weight edges"*.

**On a complete graph the max-flux walk CANNOT dead-end** — every remaining
node is adjacent, so a next step always exists. On a mean-degree-600
small-world it essentially cannot either. The failure mode we spend three
phases on is structurally unreachable on most of their test set. Their two real
transport networks are the interesting exception (Anaheim's ~2.2 is comparable
to ours), but the model is still applied as undirected, which is precisely the
constraint that breaks our extraction.

**POSITIONING, ready to write:**

> The D-Path construction is the standard max-flux extraction, and prior work
> treats its convergence to the shortest path as reliable enough to serve as a
> termination criterion. That holds on complete and high-degree synthetic
> graphs, where the walk cannot dead-end. On a real road network with mean
> out-degree 2.46 and one-way constraints, it does not: at the conductivity
> convergence point the walk completes a path on 43.0% of solves and matches
> the optimum on 23.3%, and its reliability *decreases* as the field converges.

**This also reframes our stopping-rule experiment.** Testing whether an earlier
stopping rule helps is not a loose idea of mine — it is testing **their OPPA-D
criterion on a substrate they did not test**. Cite OPPA-D as the prior art and
report whether it transfers.

**Cite approvingly:** they make the methodological point that some accelerated
Physarum variants' apparent speedups come from an "inappropriate termination
criterion". That is the same species of argument as our provenance finding —
a reported number that is really a statement about bookkeeping — and it shows
the field already takes this class of error seriously.

### 6b. Other Physarum theory

- **"When does the Physarum Solver Distinguish the Shortest Path from other
  Paths: the Transition Point and its Applications"** —
  <https://arxiv.org/pdf/2101.02913>
- **"Physarum Can Compute Shortest Paths"** (Bonifaci et al., the convergence
  proof; our `D <- |Q|` config switch is this variant) —
  <https://www.researchgate.net/publication/228065551_Physarum_Can_Compute_Shortest_Paths>

**Read the transition-point paper before interpreting our 23.3% walk-only
exact-match rate.** If there is existing theory on the conditions under which
the Physarum solver separates the shortest path from competitors, it may
predict our result rather than be surprised by it — in which case our
contribution is the *measurement on a real directed network*, not the discovery
that the walk is weak. It may also identify a parameter regime (our `theta`,
`mu`, `dt`, or the iteration budget) where separation does occur, which would
be a legitimate "we did not tune into that regime" limitation to disclose
rather than a finding.

Bonifaci proves convergence for the continuous dynamics on *undirected* graphs.
Our Phase 8c mechanism is that the pressure field is solved on an undirected
projection while the extracted path must be directed-legal. Worth checking
whether the proof's assumptions are violated by our directed extraction step —
if so, that is a clean theoretical framing for the whole provenance finding.

### 7. Physarum on a real transport network, benchmarked against Dijkstra

**Traffic optimization in railroad networks using an algorithm mimicking
Physarum plasmodium** (Biosystems) —
<https://www.sciencedirect.com/science/article/abs/pii/S0303264711000803>

Applies a modified Physarum model to the **Tokyo rail network** with Dijkstra
as the comparison, evaluating cost, travelling efficiency and **network
weakness**. This is the closest existing work to ours on two axes at once: a
real transport network, and a disruption-adjacent measure. It is rail rather
than road (no one-ways, far smaller, no turn restrictions, and the topology is
engineered rather than organically grown), has no ACO arm, and no
simulator-generated congestion.

**This is the paper our "real network" novelty has to be stated against.**
Claiming "first on a real network" would be wrong; the accurate claim is first
on a real *road* network with directed constraints and simulator congestion,
with all three methods, measuring recovery.

---

## Queries run (2026-10-18)

Recorded so the novelty check is reproducible rather than anecdotal.

1. `Physarum polycephalum ant colony optimization Dijkstra comparison road network routing benchmark`
2. `bio-inspired routing resilience road network disruption recovery SUMO simulation 2025 2026`
3. `Physarum slime mould algorithm road network resilience edge removal recovery adaptive reroute`
4. `ant colony optimization OpenStreetMap Indian city road network shortest path Bengaluru Bangalore routing study`
5. `Physarum model SUMO traffic simulator time-varying congestion travel time routing`
6. `"slime mould" OR "Physarum" "ant colony" Dijkstra three-way comparison urban road network disruption`

**Result: no source combining all three methods + real road network + simulated
congestion + disruption recovery.** Query 5 specifically found **no work
combining Physarum with SUMO at all**, which is the strongest single support
for the novelty claim.

### Second round (2026-10-18, after the three close reads)

7. `forward citations Zhang Physarum shortest path algorithm road network
   directed congestion benchmark 2020..2026`
8. `ant colony optimization dynamic vehicle routing road network congestion
   SUMO benchmark comparison Dijkstra recovery road closure`
9. `Physarum shortest path "traffic network" redundant nodes ICSI 2021 ...
   directed`

**Result of query 8, and it is the one that matters for the recovery axis:
several ACO-for-traffic papers do use SUMO and do compare against Dijkstra,
but NONE reports recovery after a closure.** The nearest miss is an improved
ACO for dynamic shortest-path problems (Springer LNCS, 2026,
doi:10.1007/978-3-032-26319-3_12) whose abstract motivates the work with
exactly our setting — "traditional shortest path algorithms, such as Dijkstra's
and Bellman-Ford, struggle to provide effective solutions in environments
where congestion, accidents, and road closures continuously alter network
conditions" — but is paywalled and **UNREAD**. It is the single highest-priority
item left: if it measures recovery after closures against Dijkstra, it is our
closest competitor on the recovery axis and must be cited and distinguished.
Two others seen only in snippet: a density-guided ACO for dynamic VRP that
uses SUMO for traffic-density matrices, and an ACO routing paper for connected
vehicles evaluated in SUMO. Neither shows a closure-recovery measurement in
the visible text; **both unread**.

---

## Systematic database pass — **RUN 2026-10-18**

Done through the open citation APIs (Semantic Scholar, OpenAlex, Crossref),
which need no subscription. **Read the scope note at the end before quoting
this as a systematic review.**

### Forward-citation sweep — COMPLETE, NOT SAMPLED

**Zhang et al. 2014** (`10.1155/2014/487069`) — 28 citing papers in Semantic
Scholar, 26 in OpenAlex. **All 28 titles screened. No competitor.** The set is
dominated by slime-mould algorithm design, surveys, and applications far from
road routing (knapsack, water-distribution networks, asteroid tours, ZigBee
deployment, robot path planning). The only road/traffic-adjacent entries are
"A Node Selecting Approach for Traffic Network Based on Artificial Slime Mold"
(IEEE Access, 2020) and "A Physarum-inspired algorithm for logistics
optimization" (Swarm Evol. Comput., 2021) — neither is a benchmark against ACO
and Dijkstra, and neither models congestion or disruption.

**Tokyo rail paper** (`10.1016/j.biosystems.2011.05.001`) — 69 citing papers,
all screened for road / traffic / congestion / disruption / recovery / ACO
terms; 10 matched and were read by title. **No competitor.** The two closest
are *network design* rather than routing: "Fault tolerant network design
inspired by Physarum polycephalum" (Natural Computing, 2013) and "PANDA: a
physarum-inspired algorithm to solve the multi-objective discrete network
design problem" (Expert Syst. Appl., 2024).

**A category worth one sentence in the paper: Physarum+ACO HYBRIDS exist and
are NOT benchmarks.** "An Ant Colony System Based on the Physarum Network"
(ICSI 2013) and "A Logistic Distribution Routes Solving Strategy Based on the
Physarum Network and Ant Colony Optimization" (IEEE HPCC 2015) COMBINE the two
methods into one algorithm. That is a different contribution from comparing
them, and we should name the distinction rather than let a reviewer think we
missed them.

### Structured OpenAlex queries

| query | matches |
|---|---|
| Physarum + ant colony + Dijkstra + road network + congestion + recovery | **0** |
| Physarum + slime mould + road closure + disruption + recovery + rerouting | **0** |
| bio-inspired routing + SUMO + microscopic simulation + algorithm comparison | 59, none relevant |
| ACO + road network + disruption + recovery + warm start + pheromone retention | 4, all noise |

The 59 bio-inspired+SUMO hits are traffic-SIGNAL control, V2X route advisory,
and GA intersection tuning. **None is a Physarum/ACO/Dijkstra routing
benchmark**, which corroborates the earlier finding that no Physarum + SUMO
work exists.

### The 2026 Springer ACO chapter — **RESOLVED. NOT A COMPETITOR.**

Nguyen Minh Tuan, Phayung Meesad, Nguyen Hong Son, Huynh Trong Thua, "An
Improved Ant Colony Optimization for Dynamic Shortest Path Problems in Smart
Transportation Systems", *Lecture Notes in Networks and Systems* (AUTSYS 2025
proceedings), 2026, pp. 147-158, doi:10.1007/978-3-032-26319-3_12.
**Abstract read in full from the publisher page; full text is paywalled.**

It was flagged as the likeliest competitor on the recovery axis because its
motivation names road closures. **That is motivation only.** A keyword audit
of the entire chapter landing page gives: `closure` 2 hits (both in the one
motivating sentence), **`disrupt` 0, `recovery` 0, `Physarum` 0, `SUMO` 0**.
The evaluation is the **METR-LA** dataset — loop-detector speeds from Los
Angeles highways — against classical ACO, Dijkstra and a GA router, measuring
travel time and convergence under *fluctuating traffic*, never under a closure.

**Cite it, for two reasons, one of them against us.** It is the most recent
ACO-vs-Dijkstra comparison under genuinely MEASURED time-varying traffic, and
that is precisely where our design is weaker: our congestion is
simulator-generated from synthetic demand, theirs is real sensor data. The
honest limitation sentence is that we buy a controlled, repeatable disruption
protocol and a full directed street network at the cost of measured demand,
and METR-LA is the obvious complement. It is not a competitor because it has
no Physarum arm, no closure experiment and no recovery metric.

### SCOPE NOTE — what this pass does and does not cover

Covered: everything indexed by Crossref/OpenAlex/Semantic Scholar, which
includes IEEE, Springer, Elsevier, Wiley and most Scopus-indexed venues, plus
complete forward-citation sets for the two anchor papers.

**NOT covered, and these remain genuine gaps:** the IEEE Xplore and Scopus
native interfaces with their own controlled vocabularies; Google Scholar's
full-text index (which reaches theses and non-indexed proceedings); Indian
journals and conference proceedings that index poorly; and anything behind a
paywall that we judged from the abstract alone (the 2026 chapter, and the
directed-Physarum variant, whose full text still needs an institutional
login).

**Revised verdict: the novelty claim is now SYSTEMATICALLY SUPPORTED for the
indexed literature, not merely unrefuted.** Two complete forward-citation
sweeps of the two most likely anchor papers, plus structured database queries
on the decisive combinations, return no work combining the three algorithms on
a real road network with simulator congestion and closure recovery. The
residual risk is concentrated in non-indexed venues.

---

## What to do with this before writing

1. ~~Read Zhang 2014 first.~~ **DONE 2026-10-18 — see above. It has no
   fallback and no extraction step, so the "everyone pools across provenance"
   framing is dead. The replacement framing is stronger: on random graphs the
   flux separates cleanly and extraction is trivial; on a real directed road
   network it does not, which is what makes extraction decisive and the
   two-series split necessary.** ~~Still outstanding: read Figures 4/5/9.~~
   **ALSO DONE 2026-10-18** — read from the PMC figure images, not the PDF
   (they are rasters). Figure 9 is the defensive result: **their ACO sits at
   0% optimal-path accuracy from 1,000 to 2,000 nodes**, where ours reaches
   43-44% exact and 73-81% within 10% on a larger, directed, real network.
   That is what licenses reporting ACO's missed exit criterion as a finding
   about ACO rather than about our tuning.
2. ~~Read the directed variant before claiming directedness is unaddressed.~~
   **DONE 2026-10-18 — it exists (Zhang, Zhang & Deng 2014). Two edits follow
   from it: (i) we must NOT claim directedness is an unaddressed gap, and must
   disclose that we did not implement the directed formulation; (ii) we SHOULD
   cite the literature's own statement that the model is "designed only for
   undirected networks", which turns our extraction finding from a possible
   implementation defect into a measurement of an acknowledged limitation.**
3. ~~Read the transition-point paper.~~ **DONE 2026-10-18, read in full from
   the PDF. It is the most important paper in this list. Their D-Path IS our
   max-flux walk, and their termination criterion rests on a claim we measured
   as false on a directed road network. Their test graphs are complete or
   mean-degree 6-600 and explicitly undirected, where the walk cannot
   dead-end. Cite OPPA-D as the prior art our stopping-rule result tests.** If theory already predicts when
   the Physarum solver fails to separate the shortest path, our 23.3% is a
   confirmation on a new substrate rather than a discovery, and the paper must
   be framed accordingly.
4. **Fix the novelty sentence.** The Tokyo rail paper means "first on a real
   network" is false. The defensible claim is: first three-way benchmark on a
   real *road* network with directed constraints and simulator-generated
   congestion, measuring recovery from closures as well as path quality.
5. ~~Check whether the goal-directed ACO paper evaluates under congestion.~~
   **DONE 2026-10-18, read in full. IT DOES NOT: static arc travel times on
   one 528-node ArcGIS network, no simulator, no Dijkstra baseline, no
   recovery, and its orientation term is an ANGULAR alignment factor rather
   than our great-circle time bound. We extend it; we do not contradict it,
   and the section heading has been corrected to say so.**
6. ~~Run the database pass.~~ **DONE 2026-10-18 via the open citation APIs —
   see the section above.** Complete forward-citation sweeps of Zhang 2014
   (28/28 screened) and the Tokyo rail paper (69, 10 relevant read), plus
   structured OpenAlex queries returning ZERO matches on the two decisive
   combinations. The 2026 Springer chapter is **resolved and is not a
   competitor**: its closure language is motivation only, and its evaluation
   is METR-LA sensor data with no disruption, no recovery metric and no
   Physarum arm. Residual gaps are named in the scope note: Google Scholar
   full text, poorly-indexed Indian venues, and two paywalled full texts
   judged from abstracts.
7. Only then write the related-work section.

**One standing risk.** Three of the six leads above could each weaken a claim
we currently make. That is the expected shape of an honest literature check,
and none of them touches the measurements themselves — the sweep, the paired
statistics and the provenance split stand regardless. What they can change is
how the contribution is *framed*, which is cheaper to fix now than after a
reviewer finds it.
