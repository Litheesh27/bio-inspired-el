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
    formats: Sequence[str] | None = DEFAULT_FORMATS,
) -> list[Path]:
    """Write `fig` once per requested format and return the paths."""
    if formats is None:
        formats = style.output_formats()
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
    head, _, tail = style.scenario_label(scenario).partition("\n")
    if style.is_ieee():
        # `IEEE_SCENARIO_LABELS` is already one short word per scenario;
        # re-wrapping it would only add a blank line.
        return "\n".join([head, tail])
    return "\n".join([head, *textwrap.wrap(tail, width=width)])


def _caption(
    fig: plt.Figure,
    text: str,
    y: float,
    *,
    width: int = 150,
    outdir: Path | None = None,
    stem: str | None = None,
    title: str | None = None,
) -> None:
    """Draw a figure caption with EXPLICIT wrapping and line spacing.

    matplotlib's `wrap=True` re-wraps against the figure box but keeps the
    single-line spacing, so a three-line caption overlaps itself. Wrapping
    here and setting `linespacing` fixes that deterministically instead of
    depending on the renderer's idea of the figure width.

    IN IEEE MODE NOTHING IS DRAWN. A paper's caption is set by LaTeX in the
    column, below the float, and a second caption baked into the image
    would duplicate it at 7 pt. The text is written to
    `<stem>.caption.txt` instead, headed by the figure's own headline
    claim (its screen suptitle), so the claim the drawn figure drops is
    still carried -- and is right there to paste into `\\caption{}`.
    """
    if style.is_ieee():
        if outdir is not None and stem is not None:
            _write_caption(Path(outdir), stem, title, text)
        return
    fig.text(
        0.5, y, textwrap.fill(" ".join(text.split()), width=width),
        ha="center", va="top", fontsize=7, color=MUTED, linespacing=1.55,
    )


def _write_caption(
    outdir: Path, stem: str, title: str | None, text: str,
) -> Path:
    """Write the LaTeX-ready caption for one IEEE figure."""
    outdir.mkdir(parents=True, exist_ok=True)
    body = " ".join(text.split())
    head = " ".join(title.split()) if title else ""
    path = outdir / f"{stem}.caption.txt"
    parts = [p for p in (head, body) if p]
    path.write_text(
        textwrap.fill("  ".join(parts), width=78) + "\n", encoding="utf-8",
    )
    return path


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

    # IEEE: FULL WIDTH. Two panels x six scenario groups x up to four boxes
    # is 24 boxes; at 3.5 in that is 0.15 in per box and the scenario ticks
    # would overlap. At 7.16 in each panel gets 3.4 in, which holds six
    # two-line ticks at the 8 pt floor once the descriptive second line is
    # abbreviated (`style.IEEE_SCENARIO_LABELS`).
    fig, axes = plt.subplots(
        1, 2, sharey=True,
        figsize=style.pick((11.0, 4.4), (style.IEEE_PAGE_WIDTH_IN, 3.1)),
        gridspec_kw={"wspace": style.pick(0.08, 0.05)},
    )
    if style.is_ieee():
        # Bottom band holds the x labels and then the legend, in that
        # order; nothing is placed outside the figure rectangle.
        fig.subplots_adjust(
            left=0.082, right=0.995, top=0.935, bottom=0.255,
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
        ax.set_title(
            style.pick(title, title.split(" (")[0]), color=INK,
            fontsize=style.pick(10, 8),
        )
        ax.set_xticks(range(len(scenarios)))
        ax.set_xticklabels(
            [_wrap_scenario(s) for s in scenarios],
            fontsize=style.pick(7, 8),
        )
        ax.set_xlim(-0.6, len(scenarios) - 0.4)
        ax.set_yscale("log")
        ax.set_xlabel("disruption scenario (severity order)")

    # The empty band in the cold panel is itself the result, so it gets
    # labelled instead of being left for the reader to notice.
    axes[1].annotate(
        style.pick(
            "Nothing is plotted between 1 and ~70 here: cold start's\n"
            "MINIMUM is 73 iterations. Warm Physarum boxes (left panel)\n"
            "reach all the way down to 1 because that distribution is\n"
            "bimodal, not because it is uncertain -- see figure 7.",
            # Shortened, not shrunk: the empty band is ~0.9 in tall here
            # and the four-line version needs 0.55 in of text width it
            # does not have. The claim it drops (why warm reaches 1) is
            # the subject of figure 7 and of this figure's caption.
            "empty below 70: cold start's\nMINIMUM is 73 iterations",
        ),
        xy=(0.03, style.pick(0.34, 0.30)), xycoords="axes fraction",
        fontsize=style.pick(7.5, 7.0), color=MUTED,
    )
    axes[0].set_ylabel(
        style.pick(
            "recovery iterations to adapt (count, log scale)",
            "recovery iterations (log)",
        )
    )
    axes[0].set_yticks([1, 2, 5, 10, 20, 50, 100])
    axes[0].get_yaxis().set_major_formatter(mticker.ScalarFormatter())

    headline = (
        "Bio-inspired routers re-adapt in tens of iterations; warm start "
        "cuts that to one on half of all closures"
    )
    if not style.is_ieee():
        fig.suptitle(headline, fontsize=11, color=INK, y=1.02)
    fig.legend(
        handles=list(legend_handles.values()),
        loc="lower center", ncol=4,
        bbox_to_anchor=style.pick((0.5, -0.13), (0.5, -0.005)),
    )
    _caption(
        fig, y=-0.19, width=170,
        outdir=outdir, stem="fig02_recovery_by_scenario", title=headline,
        text=(
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

    # IEEE: SINGLE COLUMN. Five groups of two boxes is the one chart here
    # that fits 3.5 in -- the x axis carries five categories rather than a
    # continuous range, and the boxes stay 0.3 of a 1.0 slot wide either
    # way. Only the tick labels and the reference annotation need
    # shortening.
    fig, ax = plt.subplots(
        figsize=style.pick((9.2, 4.6), (style.IEEE_COL_WIDTH_IN, 2.9)),
    )
    if style.is_ieee():
        fig.subplots_adjust(left=0.155, right=0.985, top=0.98, bottom=0.235)
    ax.axhline(
        1.0, color=REFERENCE, linestyle="--", linewidth=1.0, zorder=0,
    )
    ax.text(
        style.pick(-0.60, -0.56), 1.016,
        style.pick(
            "stretch = 1.000: Dijkstra's optimum, same graph",
            "1.000 = Dijkstra's optimum",
        ),
        fontsize=style.pick(7, 6.5), color=MUTED, va="bottom",
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
            x, 0.962,
            # The counts stay (the model-only series' smaller n IS the
            # point), but the "n=" prefix goes: at 3.5 in the five labels
            # would otherwise collide.
            style.pick(
                f"n={_n(n_pre)} / {_n(n_post)}", f"{_n(n_pre)}/{_n(n_post)}",
            ),
            ha="center", fontsize=style.pick(7, 6.5), color=MUTED,
        )

    ax.set_xticks(range(len(labels)))
    # Five category names inside 3.5 in. Stacked two-line labels collide
    # ("+ fallback" and "model only" leave 2 pt between them) and the fix
    # is NOT to shrink them below the 8 pt floor or to abbreviate
    # "Physarum (model only)" into something that could be mistaken for
    # the pooled series -- that label is load-bearing (no-pooling rule).
    # Rotating keeps both names in full at 8 pt.
    ax.set_xticklabels(
        [lab if style.is_ieee() else lab.replace(" (", "\n(")
         for lab in labels],
        fontsize=8,
        rotation=style.pick(0, 22),
        ha=style.pick("center", "right"),
        rotation_mode=style.pick(None, "anchor"),
    )
    ax.set_ylim(0.95, 2.45)
    ax.set_ylabel(
        style.pick(
            "stretch  (algorithm cost / Dijkstra cost, dimensionless)",
            "stretch (cost / Dijkstra cost)",
        )
    )
    ax.set_xlabel("")
    headline = (
        "Path quality holds up after a closure -- but Physarum's does so "
        "only with its fallback"
    )
    if not style.is_ieee():
        ax.set_title(headline, fontsize=11, color=INK)
    ax.legend(
        handles=[
            Patch(facecolor=MUTED, edgecolor=MUTED, alpha=0.45,
                  label=style.pick(
                      "pre-disruption (initial solve)", "pre-disruption")),
            Patch(facecolor=MUTED, edgecolor=MUTED, alpha=0.85, hatch="///",
                  label=style.pick(
                      "post-disruption (S1-S6 recovery)",
                      "post-disruption (S1-S6)")),
        ],
        loc="upper left",
    )
    _caption(
        fig, y=-0.06, width=150,
        outdir=outdir, stem="fig03_stretch_pre_post", title=headline,
        text=(
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
    # IEEE: FULL WIDTH, and it is the figure that most clearly earns it.
    # Two log-x panels sharing a y axis, each carrying ten markers with
    # IQR arms over a scatter; at 3.5 in the two Physarum markers -- whose
    # VERTICAL GAP is the whole point -- would be ~2 mm apart with their
    # error bars on top of each other.
    fig, axes = plt.subplots(
        1, 2, sharey=True,
        figsize=style.pick((11.4, 5.9), (style.IEEE_PAGE_WIDTH_IN, 3.15)),
    )
    # bottom leaves room for, in order: the x labels, the legend, and the
    # four-line caption -- each given its own band so none can collide.
    # In IEEE mode there is no in-figure caption, so the bottom band holds
    # the x labels and a two-row legend only.
    fig.subplots_adjust(
        **style.pick(
            dict(left=0.075, right=0.985, top=0.875, bottom=0.335,
                 wspace=0.07),
            dict(left=0.082, right=0.983, top=0.92, bottom=0.235,
                 wspace=0.05),
        )
    )
    x_specs = style.pick(
        (
            ("recovery_iterations", "recovery iterations to adapt "
                                    "(count, log scale)"),
            ("recovery_wallclock_s",
             "recovery wall-clock (seconds, log scale)"),
        ),
        (
            ("recovery_iterations", "recovery iterations (log)"),
            ("recovery_wallclock_s", "recovery wall-clock, s (log)"),
        ),
    )

    # Median marker coordinates, recorded as they are drawn so the IEEE
    # gap annotation below can point at the ACTUAL markers rather than
    # recompute them from the frame and risk pointing somewhere else.
    marker_pts: dict[tuple[int, str, bool], tuple[float, float]] = {}

    for panel, (ax, (x_col, x_label)) in enumerate(zip(axes, x_specs)):
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
                size = style.pick(
                    11 if series.key == "dijkstra" else 9,
                    7.5 if series.key == "dijkstra" else 6.0,
                )
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
                marker_pts[(panel, series.key, warm)] = (mx[0], my[0])
        ax.set_xscale("log")
        ax.set_xlabel(x_label)
        # Placed in empty space rather than over the bottom-left corner,
        # which is exactly where the classical markers sit.
        ax.annotate(
            style.pick("faster and closer\nto optimal", "better"),
            xy=(0.10, 0.52), xytext=style.pick((0.33, 0.80), (0.30, 0.72)),
            xycoords="axes fraction", textcoords="axes fraction",
            fontsize=style.pick(8, 7), color=MUTED, ha="left",
            arrowprops={"arrowstyle": "->", "color": MUTED, "lw": 0.9},
        )

    axes[0].set_ylabel(
        style.pick(
            "post-disruption stretch  (cost / Dijkstra cost on the closed "
            "graph)",
            "post-disruption stretch",
        )
    )
    axes[0].set_title(
        style.pick("algorithmic cost: iterations", "(a) algorithmic cost"),
        color=INK,
    )
    axes[1].set_title(
        style.pick("engineering cost: wall-clock", "(b) engineering cost"),
        color=INK,
    )
    axes[0].set_ylim(0.97, 1.75)
    # Dijkstra and A* coincide exactly; say so rather than let a reader
    # conclude one of them is missing.
    axes[0].annotate(
        style.pick(
            "Dijkstra and A* coincide here:\n1 iteration, stretch 1.000",
            "Dijkstra = A*:\n1 iter, 1.000",
        ),
        xy=(1.0, 1.0), xytext=style.pick((1.9, 1.17), (1.5, 1.14)),
        fontsize=style.pick(7.5, 7), color=INK,
        arrowprops={"arrowstyle": "->", "color": INK, "lw": 0.9},
    )
    if style.is_ieee():
        # THE FIGURE'S POINT, drawn rather than left to the caption: the
        # vertical distance between Physarum's two markers IS what the
        # fallback contributes. The screen variant says this in its
        # in-figure caption, which IEEE mode drops, so the claim is made
        # as a double-headed arrow between the two warm markers instead.
        # The COLD pair, not the warm one: the same gap exists on both, but
        # cold's is 0.20 of stretch against warm's 0.06, so on a 2.1 in
        # axis the cold arrow is ~14 mm and the warm one ~4 mm.
        full = marker_pts.get((0, "physarum", False))
        walk = marker_pts.get((0, "physarum_walk", False))
        if full and walk:
            axes[0].annotate(
                "", xy=walk, xytext=full,
                arrowprops={"arrowstyle": "<->", "color": INK, "lw": 0.9,
                            "shrinkA": 3.0, "shrinkB": 3.0},
            )
            axes[0].annotate(
                "the fallback's contribution",
                xy=(walk[0], walk[1]), xytext=(0, 9),
                textcoords="offset points",
                fontsize=6.5, color=INK, ha="center", va="bottom",
                # The ACO scatter runs through this band; a tight white
                # patch keeps the label readable without hiding a marker.
                bbox={"facecolor": "white", "edgecolor": "none",
                      "alpha": 0.85, "pad": 1.0},
            )

    algo_handles = [
        Line2D([], [], color=style.color(s.key), marker="o", linestyle="none",
               markersize=style.pick(8, 6), label=style.label(s.key))
        for s in stats.QUALITY_SERIES
    ]
    start_handles = [
        Line2D([], [], color=INK, marker="o", linestyle="none",
               markersize=style.pick(8, 6),
               label="warm start (retained state)"),
        Line2D([], [], color=INK, marker="s", linestyle="none",
               markersize=style.pick(8, 6), markerfacecolor="none",
               markeredgewidth=1.6,
               label=style.pick(
                   "cold start (rebuilt) -- the only mode Dijkstra/A* have",
                   "cold start (rebuilt; the only mode Dijkstra/A* have)")),
    ]
    fig.legend(
        handles=algo_handles + start_handles,
        loc="lower center", ncol=4,
        bbox_to_anchor=style.pick((0.5, 0.145), (0.5, 0.0)),
    )
    headline = (
        "The speed-vs-quality trade: warm start buys an order of magnitude "
        "in iterations, and costs stretch only through Physarum's own "
        "extraction"
    )
    if not style.is_ieee():
        fig.suptitle(headline, fontsize=11, color=INK, y=0.98)
    _caption(
        fig, y=0.105,
        outdir=outdir, stem="fig04_pareto_recovery_vs_quality",
        title=headline,
        text=(
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

    # IEEE: FULL WIDTH. Three panels at 3.5 in would be 1.1 in each, which
    # cannot carry a log x axis with four decades. At 7.16 in each panel is
    # ~2.2 in; the ECDFs are two curves apiece and survive that, and panel
    # (c)'s four horizontal boxes only need their in-box annotations
    # shortened to the ratio and the n.
    fig, axes = plt.subplots(
        1, 3,
        figsize=style.pick((13.0, 4.2), (style.IEEE_PAGE_WIDTH_IN, 2.45)),
        gridspec_kw={
            "width_ratios": [1.0, 1.0, 1.15],
            "wspace": style.pick(0.28, 0.33),
        },
    )
    if style.is_ieee():
        fig.subplots_adjust(left=0.072, right=0.982, top=0.905, bottom=0.185)

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
                where="post", color=style.START_COLORS[key],
                linewidth=style.pick(2.0, 1.3),
                label=style.pick(
                    f"{style.START_LABELS[key]}  (n={_n(x.size)})",
                    f"{key} (n={_n(x.size)})",
                ),
            )
        # The iteration budget is drawn because the ECDF's vertical jump at
        # the right edge is runs that BURNED it rather than converged --
        # 24.4% of ACO recoveries. Without the line that jump reads as a
        # cluster of fast finishers.
        budget = float(sel["iteration_budget"].max())
        ax.axvline(budget, color=REFERENCE, linestyle=":", linewidth=1.0)
        ax.text(
            budget * 0.95, 0.02, f"budget {budget:.0f}",
            rotation=90, ha="right", va="bottom",
            fontsize=style.pick(7, 6.5), color=MUTED,
        )
        ax.set_xscale("log")
        ax.set_xlim(0.85, 380)
        ax.set_ylim(0, 1.02)
        ax.set_xlabel(
            style.pick(
                "recovery iterations (count, log scale)",
                "recovery iterations (log)",
            )
        )
        panel = "(a) " if algo == "physarum" else "(b) "
        ax.set_title(
            style.pick(style.label(algo), panel + style.label(algo)),
            color=INK,
        )
        # Lower right is where the budget rule and its rotated label sit;
        # at IEEE width the legend and that label overlap, so the legend
        # moves to the upper left, which both ECDFs leave empty (neither
        # curve reaches 0.8 before x=40).
        ax.legend(
            loc=style.pick("lower right", "upper left"),
            fontsize=style.pick(8, 6.5),
        )

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
        style.pick(
            f"{share_1:.1%} of warm recoveries\n"
            f"converge in EXACTLY 1 iteration",
            f"{share_1:.0%} converge in\nEXACTLY 1 iteration",
        ),
        xy=(1.0, share_1), xytext=style.pick((1.6, 0.74), (2.1, 0.54)),
        fontsize=style.pick(8, 7), color=INK,
        arrowprops={"arrowstyle": "->", "color": INK, "lw": 0.9},
    )
    axes[0].annotate(
        style.pick(
            f"cold start never does:\nits minimum is "
            f"{int(cold_iters.min())} iterations",
            f"cold never does:\nits min is {int(cold_iters.min())}",
        ),
        xy=(float(cold_iters.min()), 0.0), xytext=style.pick(
            (8.0, 0.26), (3.4, 0.22),
        ),
        fontsize=style.pick(8, 7), color=INK,
        arrowprops={"arrowstyle": "->", "color": INK, "lw": 0.9},
    )
    axes[0].set_ylabel(
        style.pick("cumulative share of recoveries", "cumulative share")
    )

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
            style.pick(
                f"{res['median_paired_ratio']:.2f}x   "
                f"warm faster {res['n_a_better']}/{res['n_pairs']}, "
                f"p={res['pvalue']:.1e}",
                # The p-values are 1e-59 to 1e-198 and are reported in
                # Table 3; at 2.2 in the ratio and the win count are what
                # the panel can carry.
                f"{res['median_paired_ratio']:.2f}x  "
                f"(warm faster {res['n_a_better']}/{res['n_pairs']})",
            ),
            fontsize=style.pick(7, 6.5), color=INK, va="center", ha="left",
        )
        tick_labels.append(f"{style.label(algo).split(' (')[0]}\n{unit}")
    ax.axvline(1.0, color=REFERENCE, linestyle="--", linewidth=1.0)
    ax.set_xscale("log")
    ax.set_xlim(0.28, ratio_max * 2.2)
    ax.set_xticks([t for t in (1, 10, 100, 1000) if t <= ratio_max * 2.2])
    ax.get_xaxis().set_major_formatter(mticker.ScalarFormatter())
    ax.set_yticks(range(len(specs)))
    ax.set_yticklabels(tick_labels, fontsize=style.pick(8, 7))
    # Half a slot of headroom above the first box so its annotation, which
    # sits at y - 0.34, is not clipped by the axes.
    ax.set_ylim(len(specs) - 0.5, -0.75)
    ax.set_xlabel(
        style.pick(
            "per-pair cold / warm ratio (log scale; >1 means warm start won)",
            "cold/warm ratio (log; >1: warm won)",
        )
    )
    ax.set_title(
        style.pick(
            "paired delta on identical cells",
            "(c) paired delta, identical cells",
        ),
        color=INK,
    )

    headline = (
        "Warm start's gain is fewer iterations, not cheaper ones -- and its "
        "shape is bimodal, not a ratio"
    )
    if not style.is_ieee():
        fig.suptitle(headline, fontsize=11, color=INK, y=1.03)
    _caption(
        fig, y=-0.08, width=185,
        outdir=outdir, stem="fig07_warm_vs_cold", title=headline,
        text=(
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
    formats: Sequence[str] | None = DEFAULT_FORMATS,
    only: Iterable[str] | None = None,
    mode: str | None = None,
) -> dict[str, list[Path]]:
    """Render the requested figures, returning {figure id: written paths}.

    `mode="ieee"` switches geometry, type sizes, legend placement and
    output formats to the two-column paper variant (see `style.IEEE_RC`);
    `formats=None` then follows the mode.
    """
    style.apply_style(mode)
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
