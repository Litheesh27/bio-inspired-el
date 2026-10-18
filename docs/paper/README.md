# The paper

Two versions, sharing one bibliography and one set of figures:

| File | Class | Target | Sections |
|---|---|---|---|
| `main.tex` | `IEEEtran[journal]` | 10–14 pp | `sections/01…09` |
| `main_conf.tex` | `IEEEtran[conference]` | 6–8 pp | `conf/01…05` |

The conference version is a **genuine cut, not the journal paper with
sections deleted** — its text is written tighter. It keeps one lead
contribution (extraction provenance) plus the recovery trade, and drops the
fragility result, the congestion-model result, most of the mechanism
refutations, and most of the methods detail. `main_conf.tex` carries a
comment listing what to cut next if it still runs long.

## Building

**There is no LaTeX toolchain in this repository**, so neither file has been
compiled. Treat the first build as a debugging pass, not a formality.

### Overleaf (recommended)

Run the staging script, then upload the resulting zip as a new project:

```
.venv/Scripts/python.exe docs/paper/make_overleaf_zip.py
```

It copies the two `.tex` roots, `refs.bib`, `sections/`, `conf/`, and the
seven IEEE figure PDFs into `docs/paper/figures/`, so `\graphicspath` resolves
without reaching outside the project. On Overleaf set the main document to
`main.tex` or `main_conf.tex` and the compiler to pdfLaTeX.

### Locally

Needs TeX Live or MiKTeX with `IEEEtran`:

```
cd docs/paper
latexmk -pdf main.tex        # or main_conf.tex
```

`\graphicspath` already includes `../../figures/ieee/`, so a local build finds
the figures without staging.

## Figures

Both versions use `fig01_convergence.pdf` and `fig07_warm_vs_cold.pdf`.
Regenerate all seven IEEE variants from the committed results file with:

```
.venv/Scripts/python.exe scripts/04_make_figures.py --ieee   # figs 2,3,4,7
.venv/Scripts/python.exe scripts/06_make_traces.py --ieee    # figs 1,5,6
```

Figures 1, 5 and 6 are *regenerated* rather than read from the Parquet (it
stores path costs but not paths, and no per-iteration traces); that
regeneration is cross-checked against the results file and reproduces 64/64
comparable values. Every caption states which kind it is.

## References

`refs.bib` — **every entry was verified against Crossref or OpenAlex on
2026-10-18, or read from the publisher's full text.** Entries read end to end
are marked `READ IN FULL` in a `note` field; two entries are explicitly
marked as judged from the abstract because their full texts are paywalled.

Do not add an entry that has not been checked the same way. A fabricated or
mis-attributed reference is the one defect in this paper that cannot be
defended, and this project has deliberately kept its claims checkable.

## Before submitting

- [ ] Compile both; fix whatever the first pass reports.
- [x] ~~Add author affiliation and email.~~ All five authors are in both
      versions, ordered as in the team's previous submission. Add a funding
      note to the `\author` block if one is required.
- [ ] Check the venue's page limit and cut per the list in `main_conf.tex`.
- [ ] Re-read `docs/related_work.md` §"Systematic database pass" — the novelty
      claim is stated there as *systematically supported for the indexed
      literature*, with the residual gaps named. Keep the paper's phrasing
      inside that, and never claim the three-algorithm combination is new:
      Zhang et al. 2014 already did it on synthetic graphs.
- [ ] If institutional access becomes available, read the two paywalled full
      texts (`zhang2014directed`, `tuan2026improved`) and confirm the two
      sentences that cite them.
