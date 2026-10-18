# IEEE two-column figure variant — working notes

Companion to `docs/results/phase9a_notes.md` / `phase9b_notes.md`. This
file records the SIZE DECISION and the restructuring for each figure, so a
later reader does not have to diff two renderings to find out what changed
and why.

## What the mode is

`src/bioroute/viz/style.py` gains `set_mode("ieee")` / `pick(screen, ieee)`
and an `IEEE_RC` layer on top of the existing `RC`. There is **one** code
path per figure; every place the two variants differ is a single
`style.pick(...)` call. The palette is untouched — `physarum` and
`physarum_walk` keep their separate colours AND separate labels, because
CLAUDE.md's no-pooling rule is enforced by the label as much as by the
colour.

Entry points:

    .venv/Scripts/python.exe scripts/04_make_figures.py --ieee     # 2,3,4,7
    .venv/Scripts/python.exe scripts/06_make_traces.py --ieee --from-artifacts  # 1,5,6

Both write to `figures/ieee/` as PDF (for LaTeX, `pdf.fonttype 42`),
PNG at 600 dpi and SVG. `--ieee` on script 04 **implies `--no-tables`**:
the stats bundle and `summary.md` are evidence, not rendering, and a
re-render must not be able to move them.

### Two deliberate rc choices

* `savefig.bbox = None` (the screen figures use `"tight"`). A cropped PDF
  is no longer 7.16 in wide, so `\includegraphics[width=\textwidth]` would
  rescale it and move every font size off the value chosen here. Every
  IEEE figure therefore lays its legend out INSIDE the figure rectangle
  with explicit `subplots_adjust` margins. Verified: each PNG's pixel size
  is exactly `dpi x figsize`.
* 8 pt floor for axis and tick labels. Supplementary text (per-box n,
  in-panel annotations) sits at 6.5–7 pt — the brief's trade: shorten the
  text, do not shrink past the floor.

### Captions

IEEE mode draws NO in-figure caption and NO suptitle: LaTeX sets the
caption in the column. Each figure instead writes
`figures/ieee/<stem>.caption.txt`, headed by the headline claim that was
the screen variant's suptitle, ready to paste into `\caption{}`. Nothing
the drawn figure drops is lost; it moves into that file.

## Per-figure decisions

### fig02 — recovery by scenario — FULL WIDTH (7.16 x 3.10)
Two panels x six scenario groups x up to four boxes = 24 boxes. At 3.5 in
that is 0.15 in per box and the ticks collide. Restructured: scenario tick
labels abbreviated (`style.IEEE_SCENARIO_LABELS`, "S2/betwn." for
"S2/highest-betweenness"), panel titles cut to "warm start"/"cold start",
and the four-line annotation about the empty band cut to two lines
("empty below 70: cold start's MINIMUM is 73 iterations"). The dropped
half of that annotation is figure 7's subject and is in the caption file.

### fig03 — stretch pre/post — SINGLE COLUMN (3.5 x 2.90)
The one chart here that fits a column: five CATEGORIES, not a continuous
range, and the boxes keep their 0.3-of-a-slot width at any width.
Restructured: x tick labels ROTATED 22 degrees and kept at 8 pt in full.
Stacking them two-line left 2 pt between "+ fallback" and "model only",
and the alternative — abbreviating "Physarum (model only)" — is exactly
the label that must not blur into the pooled series. The per-group counts
lose their "n=" prefix ("60/358"); the caption file says they are
n(pre)/n(post).

### fig04 — the money figure — FULL WIDTH (7.16 x 3.15)
Earns it. The figure's point is the VERTICAL GAP between Physarum's two
markers; at 3.5 in that gap is ~2 mm with the IQR arms drawn over each
other. Restructured, and this is the one place the IEEE variant SAYS MORE
than the screen one: a double-headed arrow is drawn between the two COLD
Physarum markers in panel (a), labelled "the fallback's contribution".
Cold rather than warm because cold's gap is 0.20 of stretch against warm's
0.06 (~14 mm vs ~4 mm on the axis). The arrow's endpoints are the marker
coordinates as drawn, recorded in `marker_pts` as they are plotted, not
recomputed. Panel titles become "(a) algorithmic cost" / "(b) engineering
cost"; the "faster and closer to optimal" annotation becomes "better" on
the same arrow.

### fig07 — warm vs cold — FULL WIDTH (7.16 x 2.45)
Three panels; at 3.5 in each would be 1.1 in and cannot carry four decades
of log x. Restructured: the per-panel legends move from "lower right" to
"upper left" — at IEEE width the lower-right legend lands on the iteration
budget rule and its rotated label — and their entries shorten to
"warm (n=358)". Panel (c)'s in-box annotations drop the p-value
("2.97x (warm faster 350/358)"); the p-values are 1e-59 to 1e-198 and are
in Table 3 of summary.md.

### fig01 — convergence — FULL WIDTH (7.16 x 2.75), ALL THREE STRATA KEPT
At 7.16 in each panel is ~2.2 in, which a chart of five thin lines plus a
median survives. Dropping to the two most informative strata was
considered and REJECTED: the finding this figure carries is "Physarum's
with-fallback curve sits at stretch 1.0 from iteration 1 in TWO OF THREE
panels", which is a count — a two-panel version cannot state it, and a
two-panel version chosen for informativeness would be choosing the panels
after seeing the result. Restructured: the panel titles drop the OD node
ids (10–11 digits each, ~1.6 in of title on a 2.2 in panel) and keep
stratum, hops, optimum and Physarum provenance; the ids move into the
caption file, which is what a reader re-running the cell needs. ylabel
shortens to "stretch".

### fig05 — routes before/after — FULL WIDTH (7.16 x 2.95)
Maps lose their point when small, so this is never a single-column
candidate. Restructured as the brief suggested: the legend drops the
per-route stretch and hop counts (duplicated by Table 5, and the
post-disruption stretches are in the right panel's title) and keeps each
route's IDENTITY and — non-negotiably — Physarum's extraction provenance,
since an unlabelled green line is the one thing this figure must never
show. The legend is now 4 columns of short entries inside the figure
rectangle. The right panel's stretch line is WRAPPED rather than
truncated: unwrapped it ran off the figure and lost the last algorithm's
number. The dropped per-route numbers are appended to the caption file.

### fig06 — congestion heatmap — FULL WIDTH (7.16 x 3.5)
**It survives, and it is worth stating why rather than asserting it.**
Each panel ends up ~3.1 in for a 4x4 km extent, i.e. ~1.3 km per inch.
Edge line width drops 0.75 -> 0.45 pt (a 600 dpi raster resolves 0.45 pt
as 3-4 px; a vector PDF reproduces it exactly). At that size individual
streets stay separable, the arterial structure is legible, and the
light-vs-heavy difference the figure exists to show — more black corridors
under heavy — reads at a glance. Two layout points: the panels are square
(equal-distance aspect on a square extent), so the width the colourbar
leaves sets the panel size and the FIGURE HEIGHT follows from it;
and `fig.subplots_adjust` has to be called BEFORE `fig.colorbar(ax=axes)`,
which steals its space from the panels' current boxes and silently
overrides a later adjustment.

## Verification

* **Screen figures unchanged.** All 7 screen PNGs re-rendered with the
  modified code are BYTE-IDENTICAL to the committed `figures/*.png`
  (`cmp`, 7/7). The SVGs differ only in matplotlib's randomly generated
  clip-path / glyph element ids; every path, style and coordinate matches.
  `git status` shows no modification under `figures/` — only the new
  `figures/ieee/`.
* **Stats JSON untouched.** `data/results/stats_14a535c02b83.json` still
  hashes to `5dcd936a...` (`--ieee` implies `--no-tables`).
* **Exact physical size.** Each PNG's pixel size is exactly 600 dpi x the
  intended inches, and each PDF's `/MediaBox` is exactly 515.52 pt
  (7.16 in) or 252 pt (3.5 in) wide. Nothing is cropped, so LaTeX includes
  them at 100%.
* **Fonts embedded.** Every PDF carries a `/FontFile2` (Type 42) — no
  Type 3 text, which is what `pdf.fonttype: 42` is for.
* **No recomputation.** Figures 1, 5 and 6 render from
  `data/results/traces/*.json` via `--from-artifacts` (3.8 s, zero
  solves). That path already existed for all three figures, not only
  figure 6.
* `pytest tests/test_stats.py` — 23 passed (also `test_traces.py`,
  `test_metrics.py`, `test_disruption.py`, `test_classical.py`).
* **One latent coupling closed.** `viz/stats.py` builds the bundle's
  `label` fields with `style.label(...)`, which is now mode-aware. Those
  labels feed `summary.md` and `stats_<hash>.json`, so they are pinned
  with `compact=False`: evidence must read the same whatever mode a
  figure script is in. Re-running `compute_all` with the change gives a
  bundle byte-identical to the committed JSON (verified against the file
  on disk, allowing for its CRLF line endings).

