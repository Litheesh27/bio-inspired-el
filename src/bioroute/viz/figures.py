"""Publication-quality figures generated from the results Parquet.

IMPLEMENTED IN: Phase 9. Load the `dataviz` skill before writing any chart
code here.
Will contain: (1) convergence curves (cost vs. iteration) per algorithm,
(2) recovery-iteration distributions by scenario severity, (3) stretch-
factor boxplots pre- vs. post-disruption, (4) the Pareto scatter of
recovery speed vs. post-disruption path quality (the "money figure" that
directly answers the research question), (6) congestion heatmap light vs.
heavy, (7) warm-start vs. cold-start delta. Uses median/IQR (not means),
paired Wilcoxon signed-rank tests, and bootstrap CIs on medians. Exit
criterion: all figures regenerate from Parquet with one command
(scripts/04_make_figures.py).
"""
