"""Publication-quality figures generated from the results Parquet.

IMPLEMENTED IN: Phase 9 (9a). Driven by `scripts/04_make_figures.py`;
every number comes from `viz/stats.py`, which reads only
`data/results/final_14a535c02b83.parquet`.

This module owns the four figures that come purely from the sweep's
tabular results:

  fig02  recovery-iteration distributions by scenario severity
  fig03  stretch, pre- vs post-disruption, per algorithm
  fig04  Pareto: recovery speed vs post-disruption path quality
         -- the money figure, the direct answer to the research question
  fig07  warm start vs cold start, including the bimodality

Figures 1 (convergence curves), 5 (route maps) and 6 (congestion heatmap)
need per-iteration traces and network geometry rather than the results
table, and live in `viz/traces.py` / `viz/maps.py`.

THREE DRAWING RULES THAT ARE METHODOLOGY, NOT TASTE
===================================================

1. PHYSARUM IS ALWAYS TWO SERIES. `physarum` is the with-fallback series
   (the `stretch` columns as recorded); `physarum_walk` is the model's own
   max-flux extraction. ~70% of Physarum's reported paths come from a
   fallback, and the median stretch is exactly 1.000 inside every
   provenance class (Phase 8e) -- so a single pooled Physarum box would
   draw Dijkstra's own answer and label it Physarum. `style.py` gives the
   two different colours for this reason.

2. NULLS ARE NEVER DRAWN AS ZEROS. S0 has no recovery (it is the
   no-disruption control), and the max-flux walk legitimately dead-ends
   without producing any path. Both show up as absent boxes/markers with
   the count stated, never as a zero that averages into a distribution.

3. NO RATIO-OF-MEDIANS FOR WARM VS COLD. fig07 shows the per-pair delta
   distribution and the 1-iteration spike, because the warm distribution
   is bimodal (modes at 1 and ~50) and a ratio of its median to cold's
   produces a bogus "24x".

Boxes are median + IQR with whiskers at 1.5 IQR and outliers drawn: the
tails are the interesting part of a right-skewed recovery distribution, so
hiding fliers would hide the result. matplotlib only -- seaborn is not
installed and is not needed.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Callable, Iterable, Sequence

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from . import stats, style

DEFAULT_FORMATS: tuple[str, ...] = ("png", "svg")

#: Ink for text and reference lines. Kept out of `style.py` on purpose:
#: that module is finished and owns identity colours, not neutrals.
INK = "#1A1A1A"
MUTED = "#6B6B6B"
REFERENCE = "#9A9A9A"


# --- plumbing --------------------------------------------------------------


def _save(
    fig: plt.Figure,
    outdir: Path,
    stem: str,
    formats: Sequence[str] = DEFAULT_FORMATS,
) -> list[Path]:
    """Write `fig` once per requested format and return the paths."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fmt in formats:
        path = outdir / f"{stem}.{fmt}"
        fig.savefig(path, format=fmt)
        written.append(path)
    plt.close(fig)
    return written


def _n(value: float | int) -> str:
    return f"{int(value):,}"


def _box(
    ax: plt.Axes,
    values: Iterable[float],
    position: float,
    colour: str,
    *,
    width: float = 0.18,
    hatch: str | None = None,
    alpha: float = 0.75,
) -> int:
    """One box at `position`; returns how many points it drew.

    Returns the count so callers can annotate it: an unlabelled box over 21
    points next to one over 150 invites exactly the misreading this project
    keeps catching.
    """
    data = np.asarray(
        [v for v in values if v is not None and np.isfinite(v)], dtype=float
    )
    if data.size == 0:
        return 0
    bp = ax.boxplot(
        [data],
        positions=[position],
        widths=width,
        patch_artist=True,
        showfliers=True,
        whis=1.5,
        medianprops={"color": INK, "linewidth": 1.4},
        boxprops={"facecolor": colour, "edgecolor": colour, "alpha": alpha,
                  "linewidth": 0.8},
        whiskerprops={"color": colour, "linewidth": 1.0},
        capprops={"color": colour, "linewidth": 1.0},
        flierprops={"marker": ".", "markersize": 2.5,
                    "markerfacecolor": colour, "markeredgecolor": "none",
                    "alpha": 0.45},
    )
    if hatch:
        for patch in bp["boxes"]:
            patch.set_hatch(hatch)
    return int(data.size)


def _wrap_scenario(scenario: str, width: int = 12) -> str:
    """Re-wrap a scenario label so six of them fit on one axis.

    `style.SCENARIO_LABELS` is authoritative and unmodified; this only
    breaks its descriptive second line, which otherwise collides with its
    neighbours at six ticks per panel.
    """
    head, _, tail = style.SCENARIO_LABELS[scenario].partition("\n")
    return "\n".join([head, *textwrap.wrap(tail, width=width)])


def _caption(
    fig: plt.Figure, text: str, y: float, *, width: int = 150,
) -> None:
    """Draw a figure caption with EXPLICIT wrapping and line spacing.

    matplotlib's `wrap=True` re-wraps against the figure box but keeps the
    single-line spacing, so a three-line caption overlaps itself. Wrapping
    here and setting `linespacing` fixes that deterministically instead of
    depending on the renderer's idea of the figure width.
    """
    fig.text(
        0.5, y, textwrap.fill(" ".join(text.split()), width=width),
        ha="center", va="top", fontsize=7, color=MUTED, linespacing=1.55,
    )


def _iqr_bars(values: pd.Series) -> tuple[float, np.ndarray] | None:
    """Median and asymmetric IQR error-bar arms, or None if empty."""
    v = pd.to_numeric(values, errors="coerce").dropna()
    if v.empty:
        return None
    med = float(v.median())
    lo = med - float(v.quantile(0.25))
    hi = float(v.quantile(0.75)) - med
    return med, np.array([[lo], [hi]])


# --- figure 2 --------------------------------------------------------------


def fig02_recovery_by_scenario(
    df: pd.DataFrame,
    outdir: Path,
    formats: Sequence[str] = DEFAULT_FORMATS,
) -> list[Path]:
    """Recovery iterations by scenario, warm and cold panels.

    Split into warm and cold panels rather than pooled: pooling would
    average Physarum's 1-iteration warm recoveries together with its
    73-iteration cold floor and produce a box describing neither. The
    classical routers appear in the cold panel only -- a one-shot router
    has no warm mode, so a warm box for them would be a duplicated row
    dressed up as a measurement (Phase 8).

    Log y, because the series span 1 to 150 iterations and a linear axis
    would compress Dijkstra, A* and half of warm Physarum onto the
    baseline.
    """
    ok = df[~df["failed"].astype(bool)]
    have = ok[ok["recovery_iterations"].notna()]
    scenarios = [s for s in style.SCENARIO_ORDER if s != "S0"]

    panels = (
        ("warm start (retained pheromone / conductivity)", True,
         ("aco", "physarum")),
        ("cold start (state rebuilt from scratch)", False,
         ("dijkstra", "astar", "aco", "physarum")),
    )

    fig, axes = plt.subplots(
        1, 2, figsize=(11.0, 4.4), sharey=True,
        gridspec_kw={"wspace": 0.08},
    )
    legend_handles: dict[str, Patch] = {}

    for ax, (title, warm, algos) in zip(axes, panels):
        side = have[have["warm_start"].astype(bool) == warm]
        n_algos = len(algos)
        # 0.22 slot per series keeps a visible gap between adjacent boxes.
        offsets = (np.arange(n_algos) - (n_algos - 1) / 2) * 0.22
        for algo, offset in zip(algos, offsets):
            colour = style.color(algo)
            total = 0
            for x, scenario in enumerate(scenarios):
                grp = side[(side["algorithm"] == algo)
                           & (side["scenario_id"] == scenario)]
                total += _box(
                    ax, grp["recovery_iterations"], x + offset, colour,
                    width=0.19,
                )
            legend_handles.setdefault(
                algo,
                Patch(facecolor=colour, edgecolor=colour, alpha=0.75,
                      label=f"{style.label(algo)}  (n={_n(total)})"),
            )
        ax.set_title(title, color=INK)
        ax.set_xticks(range(len(scenarios)))
        ax.set_xticklabels(
            [_wrap_scenario(s) for s in scenarios], fontsize=7,
        )
        ax.set_xlim(-0.6, len(scenarios) - 0.4)
        ax.set_yscale("log")
        ax.set_xlabel("disruption scenario (severity order)")

    # The empty band in the cold panel is itself the result, so it gets
    # labelled instead of being left for the reader to notice.
    axes[1].annotate(
        "Nothing is plotted between 1 and ~70 here: cold start's\n"
        "MINIMUM is 73 iterations. Warm Physarum boxes (left panel)\n"
        "reach all the way down to 1 because that distribution is\n"
        "bimodal, not because it is uncertain -- see figure 7.",
        xy=(0.03, 0.34), xycoords="axes fraction",
        fontsize=7.5, color=MUTED,
    )
    axes[0].set_ylabel("recovery iterations to adapt (count, log scale)")
    axes[0].set_yticks([1, 2, 5, 10, 20, 50, 100])
    axes[0].get_yaxis().set_major_formatter(mticker.ScalarFormatter())

    fig.suptitle(
        "Bio-inspired routers re-adapt in tens of iterations; warm start "
        "cuts that to one on half of all closures",
        fontsize=11, color=INK, y=1.02,
    )
    fig.legend(
        handles=list(legend_handles.values()),
        loc="lower center", ncol=4, bbox_to_anchor=(0.5, -0.13),
    )
    _caption(
        fig, y=-0.19, width=170, text=(
        "Box = median and IQR, whiskers 1.5 IQR, dots are outliers. S0 "
        "(no disruption) is absent by construction: it has no recovery to "
        "measure, and plotting its nulls as zeros would be a fabricated "
        "measurement. Dijkstra and A* recompute in exactly 1 iteration, so "
        "their boxes collapse to a line at y=1. Warm panel omits the "
        "classical routers, which have no warm mode. 28 failed runs "
        "(all S4, infeasible closure) excluded."
        ),
    )
    return _save(fig, outdir, "fig02_recovery_by_scenario", formats)


# --- figure 3 --------------------------------------------------------------


def fig03_stretch_pre_post(
    df: pd.DataFrame,
    outdir: Path,
    formats: Sequence[str] = DEFAULT_FORMATS,
) -> list[Path]:
    """Stretch before and after disruption, per series.

    The model-only Physarum series gets its OWN pair of boxes, which is
    the whole point of the figure: side by side it is visible that the
    with-fallback box sits on 1.000 while the model's own extraction does
    not.

    Pre-disruption boxes use one row per initial solve (the initial solve
    is identical across all 7 scenarios, so using every row would count it
    up to 13 times); post-disruption boxes use every successful S1-S6
    recovery row.
    """
    ok = df[~df["failed"].astype(bool)]
    initial = stats.initial_solves(ok)

    fig, ax = plt.subplots(figsize=(9.2, 4.6))
    ax.axhline(
        1.0, color=REFERENCE, linestyle="--", linewidth=1.0, zorder=0,
    )
    ax.text(
        -0.60, 1.016, "stretch = 1.000: Dijkstra's optimum, same graph",
        fontsize=7, color=MUTED, va="bottom",
    )

    labels: list[str] = []
    for x, series in enumerate(stats.QUALITY_SERIES):
        colour = style.color(series.key)
        pre = initial[initial["algorithm"] == series.algorithm][series.pre_col]
        post_rows = ok[ok["algorithm"] == series.algorithm]
        post = post_rows[series.post_col]
        n_pre = _box(ax, pre, x - 0.19, colour, width=0.3, alpha=0.45)
        n_post = _box(
            ax, post, x + 0.19, colour, width=0.3, alpha=0.85, hatch="///",
        )
        labels.append(style.label(series.key))
        ax.text(
            x, 0.962, f"n={_n(n_pre)} / {_n(n_post)}",
            ha="center", fontsize=7, color=MUTED,
        )

    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(
        [lab.replace(" (", "\n(") for lab in labels], fontsize=8,
    )
    ax.set_ylim(0.95, 2.45)
    ax.set_ylabel("stretch  (algorithm cost / Dijkstra cost, dimensionless)")
    ax.set_xlabel("")
    ax.set_title(
        "Path quality holds up after a closure -- but Physarum's does so "
        "only with its fallback",
        fontsize=11, color=INK,
    )
    ax.legend(
        handles=[
            Patch(facecolor=MUTED, edgecolor=MUTED, alpha=0.45,
                  label="pre-disruption (initial solve)"),
            Patch(facecolor=MUTED, edgecolor=MUTED, alpha=0.85, hatch="///",
                  label="post-disruption (S1-S6 recovery)"),
        ],
        loc="upper left",
    )
    _caption(
        fig, y=-0.06, width=150, text=(
        "Counts under each pair are n(pre) / n(post). The two Physarum "
        "series are never pooled: 'with fallback' is the reported `stretch` "
        "column, 'model only' is the max-flux walk's own result "
        "(`walk_best_stretch`), which is null -- not zero -- on the runs "
        "where the walk never completed a path, so its n is smaller by "
        "construction. SUMO congestion, sumo_speeds baseline; 28 failed "
        "runs excluded."
        ),
    )
    return _save(fig, outdir, "fig03_stretch_pre_post", formats)


# --- figure 4 (the money figure) ------------------------------------------


#: warm = filled circle, cold = hollow square. Shape carries the start
#: mode so colour is free to carry algorithm identity -- identity is never
#: encoded by colour alone.
_START_MARKER = {True: ("o", True), False: ("s", False)}


def fig04_pareto_recovery_vs_quality(
    df: pd.DataFrame,
    outdir: Path,
    formats: Sequence[str] = DEFAULT_FORMATS,
) -> list[Path]:
    """Recovery speed vs post-disruption path quality. The money figure.

    Two panels, because recovery speed has two honest units and they tell
    opposite stories: iterations-to-adapt (left) is the algorithmic
    measure, wall-clock seconds (right) is the engineering one, and
    Dijkstra wins the second by three orders of magnitude. CLAUDE.md's
    honest-reporting note requires showing that rather than quietly
    choosing the flattering axis. Two panels, NOT two y-axes on one plot.

    Bottom-left is better in both panels: fast and close to optimal.
    """
    ok = df[~df["failed"].astype(bool)]
    rows = ok[ok["recovery_iterations"].notna()]

    # Laid out with explicit margins rather than negative-offset legends:
    # the legend and caption live INSIDE the figure box, so no dead band
    # opens up between the axes and the legend, and the plot area keeps
    # the rest of the height.
    fig, axes = plt.subplots(
        1, 2, figsize=(11.4, 5.9), sharey=True,
    )
    # bottom leaves room for, in order: the x labels, the legend, and the
    # four-line caption -- each given its own band so none can collide.
    fig.subplots_adjust(
        left=0.075, right=0.985, top=0.875, bottom=0.335, wspace=0.07,
    )
    x_specs = (
        ("recovery_iterations", "recovery iterations to adapt "
                                "(count, log scale)"),
        ("recovery_wallclock_s", "recovery wall-clock (seconds, log scale)"),
    )

    for ax, (x_col, x_label) in zip(axes, x_specs):
        for series in stats.QUALITY_SERIES:
            colour = style.color(series.key)
            sel = rows[rows["algorithm"] == series.algorithm]
            for warm, (marker, filled) in _START_MARKER.items():
                grp = sel[sel["warm_start"].astype(bool) == warm]
                pts = grp.dropna(subset=[x_col, series.post_col])
                if pts.empty:
                    continue
                # Light scatter of the per-cell points behind the summary,
                # so the reader sees the spread the IQR bars summarise.
                ax.scatter(
                    pts[x_col], pts[series.post_col],
                    s=5, color=colour, alpha=0.07, linewidths=0, zorder=1,
                )
                mx = _iqr_bars(pts[x_col])
                my = _iqr_bars(pts[series.post_col])
                if mx is None or my is None:
                    continue
                # Dijkstra and A* land on the SAME point (1 iteration,
                # stretch 1.000) and would hide each other, so A* is drawn
                # smaller on top with a surface ring rather than nudged --
                # moving a marker would misreport its coordinates.
                size = 11 if series.key == "dijkstra" else 9
                ring = {"markeredgecolor": "white", "markeredgewidth": 1.2} \
                    if series.key == "astar" and filled else {}
                ax.errorbar(
                    [mx[0]], [my[0]], xerr=mx[1], yerr=my[1],
                    fmt=marker, markersize=size,
                    color=colour,
                    markerfacecolor=colour if filled else "none",
                    markeredgecolor=colour, markeredgewidth=1.6,
                    ecolor=colour, elinewidth=1.4, capsize=3,
                    zorder=3 if series.key != "astar" else 4,
                    **ring,
                )
        ax.set_xscale("log")
        ax.set_xlabel(x_label)
        # Placed in empty space rather than over the bottom-left corner,
        # which is exactly where the classical markers sit.
        ax.annotate(
            "faster and closer\nto optimal",
            xy=(0.10, 0.52), xytext=(0.33, 0.80),
            xycoords="axes fraction", textcoords="axes fraction",
            fontsize=8, color=MUTED, ha="left",
            arrowprops={"arrowstyle": "->", "color": MUTED, "lw": 0.9},
        )

    axes[0].set_ylabel(
        "post-disruption stretch  (cost / Dijkstra cost on the closed graph)"
    )
    axes[0].set_title("algorithmic cost: iterations", color=INK)
    axes[1].set_title("engineering cost: wall-clock", color=INK)
    axes[0].set_ylim(0.97, 1.75)
    # Dijkstra and A* coincide exactly; say so rather than let a reader
    # conclude one of them is missing.
    axes[0].annotate(
        "Dijkstra and A* coincide here:\n1 iteration, stretch 1.000",
        xy=(1.0, 1.0), xytext=(1.9, 1.17),
        fontsize=7.5, color=INK,
        arrowprops={"arrowstyle": "->", "color": INK, "lw": 0.9},
    )

    algo_handles = [
        Line2D([], [], color=style.color(s.key), marker="o", linestyle="none",
               markersize=8, label=style.label(s.key))
        for s in stats.QUALITY_SERIES
    ]
    start_handles = [
        Line2D([], [], color=INK, marker="o", linestyle="none",
               markersize=8, label="warm start (retained state)"),
        Line2D([], [], color=INK, marker="s", linestyle="none",
               markersize=8, markerfacecolor="none", markeredgewidth=1.6,
               label="cold start (rebuilt) -- the only mode Dijkstra/A* have"),
    ]
    fig.legend(
        handles=algo_handles + start_handles,
        loc="lower center", ncol=4, bbox_to_anchor=(0.5, 0.145),
    )
    fig.suptitle(
        "The speed-vs-quality trade: warm start buys an order of magnitude "
        "in iterations, and costs stretch only through Physarum's own "
        "extraction",
        fontsize=11, color=INK, y=0.98,
    )
    _caption(
        fig, y=0.105, text=(
        "One marker per series and start mode at the median, with IQR arms "
        "on both axes, over a light scatter of the individual S1-S6 "
        "recovery runs. Dijkstra and A* anchor the bottom-left: exact paths "
        "in one iteration and ~30 ms, which is the honest baseline the "
        "bio-inspired methods are measured against. The model-only Physarum "
        "series uses `post_disruption_walk_stretch` on the same runs, so its "
        "x is identical and only its y differs -- the vertical gap between "
        "the two green/purple markers IS the fallback's contribution. SUMO "
        "congestion on the sumo_speeds baseline; 28 failed runs excluded."
        ),
    )
    return _save(fig, outdir, "fig04_pareto_recovery_vs_quality", formats)


# --- figure 7 --------------------------------------------------------------


def _ecdf(values: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    v = np.sort(pd.to_numeric(values, errors="coerce").dropna().to_numpy())
    if v.size == 0:
        return v, v
    return v, np.arange(1, v.size + 1) / v.size


def fig07_warm_vs_cold(
    df: pd.DataFrame,
    outdir: Path,
    formats: Sequence[str] = DEFAULT_FORMATS,
) -> list[Path]:
    """Warm vs cold start: the bimodality, and the per-pair delta.

    Panels (a) and (b) are ECDFs of recovery iterations, which is the one
    form that makes the bimodality unmissable: Physarum's warm curve jumps
    to 0.48 at x=1 and then flattens, while the cold curve does not leave
    zero until x=73. A pair of medians cannot show that, and their ratio
    (98/42) is the "24x" artifact Phase 8e retired.

    Panel (c) is the admissible summary: the distribution of the per-pair
    cold/warm ratio, paired on identical (od, scenario, traffic, seed), for
    both algorithms and both cost units.
    """
    ok = df[~df["failed"].astype(bool)]
    have = ok[ok["recovery_iterations"].notna()]

    fig, axes = plt.subplots(
        1, 3, figsize=(13.0, 4.2),
        gridspec_kw={"width_ratios": [1.0, 1.0, 1.15], "wspace": 0.28},
    )

    for ax, algo in zip(axes[:2], ("physarum", "aco")):
        sel = have[have["algorithm"] == algo]
        for warm in (True, False):
            key = "warm" if warm else "cold"
            grp = sel[sel["warm_start"].astype(bool) == warm]
            x, y = _ecdf(grp["recovery_iterations"])
            if x.size == 0:
                continue
            ax.step(
                np.concatenate([[x[0]], x]), np.concatenate([[0.0], y]),
                where="post", color=style.START_COLORS[key], linewidth=2.0,
                label=f"{style.START_LABELS[key]}  (n={_n(x.size)})",
            )
        # The iteration budget is drawn because the ECDF's vertical jump at
        # the right edge is runs that BURNED it rather than converged --
        # 24.4% of ACO recoveries. Without the line that jump reads as a
        # cluster of fast finishers.
        budget = float(sel["iteration_budget"].max())
        ax.axvline(budget, color=REFERENCE, linestyle=":", linewidth=1.0)
        ax.text(
            budget * 0.95, 0.02, f"budget {budget:.0f}",
            rotation=90, ha="right", va="bottom", fontsize=7, color=MUTED,
        )
        ax.set_xscale("log")
        ax.set_xlim(0.85, 380)
        ax.set_ylim(0, 1.02)
        ax.set_xlabel("recovery iterations (count, log scale)")
        ax.set_title(f"{style.label(algo)}", color=INK)
        ax.legend(loc="lower right")

    # The spike is the result, so it is annotated rather than left to be
    # inferred from a curve.
    phys = have[have["algorithm"] == "physarum"]
    warm_iters = pd.to_numeric(
        phys.loc[phys["warm_start"].astype(bool), "recovery_iterations"],
        errors="coerce",
    ).dropna()
    cold_iters = pd.to_numeric(
        phys.loc[~phys["warm_start"].astype(bool), "recovery_iterations"],
        errors="coerce",
    ).dropna()
    share_1 = float((warm_iters == 1).mean())
    axes[0].annotate(
        f"{share_1:.1%} of warm recoveries\nconverge in EXACTLY 1 iteration",
        xy=(1.0, share_1), xytext=(1.6, 0.74),
        fontsize=8, color=INK,
        arrowprops={"arrowstyle": "->", "color": INK, "lw": 0.9},
    )
    axes[0].annotate(
        f"cold start never does:\nits minimum is "
        f"{int(cold_iters.min())} iterations",
        xy=(float(cold_iters.min()), 0.0), xytext=(8.0, 0.26),
        fontsize=8, color=INK,
        arrowprops={"arrowstyle": "->", "color": INK, "lw": 0.9},
    )
    axes[0].set_ylabel("cumulative share of recoveries")

    # Panel (c): per-pair ratios.
    ax = axes[2]
    specs = [
        ("physarum", "recovery_iterations", "iterations"),
        ("physarum", "recovery_wallclock_s", "wall-clock"),
        ("aco", "recovery_iterations", "iterations"),
        ("aco", "recovery_wallclock_s", "wall-clock"),
    ]
    tick_labels: list[str] = []
    ratio_max = 1.0
    for y, (algo, metric, unit) in enumerate(specs):
        pairs = stats.paired_frame(
            ok[ok["algorithm"] == algo], metric, "warm_start", True, False,
        )
        ratios = pairs["ratio"].dropna()
        ratio_max = max(ratio_max, float(ratios.max()))
        colour = style.color(algo)
        bp = ax.boxplot(
            [ratios.to_numpy()], positions=[y], widths=0.5, vert=False,
            patch_artist=True, whis=1.5,
            medianprops={"color": INK, "linewidth": 1.4},
            boxprops={"facecolor": colour, "edgecolor": colour,
                      "alpha": 0.7, "linewidth": 0.8},
            whiskerprops={"color": colour, "linewidth": 1.0},
            capprops={"color": colour, "linewidth": 1.0},
            flierprops={"marker": ".", "markersize": 2.5,
                        "markerfacecolor": colour, "markeredgecolor": "none",
                        "alpha": 0.4},
        )
        res = stats.paired_wilcoxon(
            ok[ok["algorithm"] == algo], metric, "warm_start", True, False,
        )
        # Annotation goes in the empty gap ABOVE each box, at the left
        # margin. Putting it to the right of the boxes does not work:
        # Physarum's wall-clock ratios reach 1,230x, so any fixed x inside
        # the data range lands on a whisker or a flier.
        ax.text(
            0.33, y - 0.34,
            f"{res['median_paired_ratio']:.2f}x   "
            f"warm faster {res['n_a_better']}/{res['n_pairs']}, "
            f"p={res['pvalue']:.1e}",
            fontsize=7, color=INK, va="center", ha="left",
        )
        tick_labels.append(f"{style.label(algo).split(' (')[0]}\n{unit}")
    ax.axvline(1.0, color=REFERENCE, linestyle="--", linewidth=1.0)
    ax.set_xscale("log")
    ax.set_xlim(0.28, ratio_max * 2.2)
    ax.set_xticks([t for t in (1, 10, 100, 1000) if t <= ratio_max * 2.2])
    ax.get_xaxis().set_major_formatter(mticker.ScalarFormatter())
    ax.set_yticks(range(len(specs)))
    ax.set_yticklabels(tick_labels, fontsize=8)
    # Half a slot of headroom above the first box so its annotation, which
    # sits at y - 0.34, is not clipped by the axes.
    ax.set_ylim(len(specs) - 0.5, -0.75)
    ax.set_xlabel(
        "per-pair cold / warm ratio (log scale; >1 means warm start won)"
    )
    ax.set_title("paired delta on identical cells", color=INK)

    fig.suptitle(
        "Warm start's gain is fewer iterations, not cheaper ones -- and its "
        "shape is bimodal, not a ratio",
        fontsize=11, color=INK, y=1.03,
    )
    _caption(
        fig, y=-0.08, width=185, text=(
        "(a, b) ECDFs of recovery iterations. (c) Median of the per-pair "
        "ratio, paired on identical (OD pair, scenario, traffic level, "
        "seed) -- never a ratio of the two medians, which on Physarum's "
        "bimodal warm distribution would read as 24x. Physarum's median "
        "seconds per iteration is 0.051 warm vs 0.054 cold, so the entire "
        "saving is iteration count. Wilcoxon signed-rank, exact zero "
        "differences dropped. SUMO congestion, sumo_speeds baseline; 28 "
        "failed runs excluded."
        ),
    )
    return _save(fig, outdir, "fig07_warm_vs_cold", formats)


# --- registry --------------------------------------------------------------

FigureFn = Callable[[pd.DataFrame, Path, Sequence[str]], list[Path]]

#: Figure ids match the PLAN's numbering, so `--only fig04` means the same
#: thing to a reader of docs/PLAN.md as it does to the script.
FIGURES: dict[str, FigureFn] = {
    "fig02": fig02_recovery_by_scenario,
    "fig03": fig03_stretch_pre_post,
    "fig04": fig04_pareto_recovery_vs_quality,
    "fig07": fig07_warm_vs_cold,
}


def make_figures(
    df: pd.DataFrame,
    outdir: Path | str,
    *,
    formats: Sequence[str] = DEFAULT_FORMATS,
    only: Iterable[str] | None = None,
) -> dict[str, list[Path]]:
    """Render the requested figures, returning {figure id: written paths}."""
    style.apply_style()
    wanted = list(FIGURES) if not only else [k for k in FIGURES if k in set(only)]
    unknown = set(only or ()) - set(FIGURES)
    if unknown:
        raise KeyError(
            f"unknown figure id(s) {sorted(unknown)}; known: {sorted(FIGURES)}"
        )
    out: dict[str, list[Path]] = {}
    for key in wanted:
        out[key] = FIGURES[key](df, Path(outdir), formats)
    return out
