"""Shared figure styling: one palette and one set of labels for every figure.

IMPLEMENTED IN: Phase 9.

Every figure in `figures.py` and `maps.py` imports from here so the three
algorithms keep the SAME colour in every panel of the paper and the
dashboard. Changing a colour here changes it everywhere, which is the whole
point -- a reader who learns "orange = ACO" in figure 2 must not have to
relearn it in figure 4.

The palette is Okabe-Ito, which is colourblind-safe and prints legibly in
greyscale (the luminances are deliberately spread). Do not substitute a
default matplotlib cycle: figure 4 is the paper's money figure and gets
read by people with deuteranopia like everyone else.

PHYSARUM HAS TWO ENTRIES, and that is a methodological requirement, not a
style choice. CLAUDE.md Phase 9 forbids pooling Physarum's stretch across
path provenance: ~70% of its reported paths come from a fallback rather
than from its own max-flux extraction (Phase 8e). So the model-only series
(`max_flux_walk` / `walk_best_stretch`) is drawn in a DIFFERENT colour from
the with-fallback series, and both are labelled, everywhere both appear.
"""

from __future__ import annotations

# --- algorithm identity ----------------------------------------------------

# Okabe-Ito. `physarum_walk` is the model's own extraction; `physarum` is the
# pooled/with-fallback series.
ALGO_COLORS: dict[str, str] = {
    "dijkstra": "#0173B2",       # blue      -- exact baseline
    "astar": "#56B4E9",          # sky blue  -- exact baseline, same family
    "aco": "#D55E00",            # vermillion
    "physarum": "#029E73",       # green     -- WITH fallback
    "physarum_walk": "#CC78BC",  # purple    -- model-only (max_flux_walk)
}

ALGO_LABELS: dict[str, str] = {
    "dijkstra": "Dijkstra",
    "astar": "A*",
    "aco": "ACO (MMAS)",
    "physarum": "Physarum (with fallback)",
    "physarum_walk": "Physarum (model only)",
}

#: Shorter labels for the IEEE two-column variant, where a legend entry has
#: ~1.8 in rather than ~2.8 in. `physarum` and `physarum_walk` MUST stay
#: distinguishable: the no-pooling rule is enforced by the label as much as
#: by the colour, so "Physarum" alone is never an acceptable shortening.
IEEE_ALGO_LABELS: dict[str, str] = {
    "dijkstra": "Dijkstra",
    "astar": "A*",
    "aco": "ACO (MMAS)",
    "physarum": "Physarum (w/ fallback)",
    "physarum_walk": "Physarum (model only)",
}

# Dijkstra/A* are exact, so they are drawn as reference lines rather than
# competing distributions in the quality figures.
EXACT_ALGOS: tuple[str, ...] = ("dijkstra", "astar")
BIO_ALGOS: tuple[str, ...] = ("aco", "physarum")

# Plot order: baselines first, then bio-inspired, then the model-only series.
ALGO_ORDER: tuple[str, ...] = (
    "dijkstra", "astar", "aco", "physarum", "physarum_walk",
)

# --- experiment axes -------------------------------------------------------

# Scenario order is SEVERITY order, which is what figure 2 plots against.
# S0 is the no-disruption control. Phase 7's rejection rates (S1 2.6% ->
# S3 14.5% -> S4 31.7%) establish that severity is super-additive in the
# number of simultaneous closures, so S1 < S3 < S4 is a measured ordering,
# not an assumed one.
SCENARIO_ORDER: tuple[str, ...] = ("S0", "S1", "S2", "S3", "S4", "S5", "S6")

SCENARIO_LABELS: dict[str, str] = {
    "S0": "S0\nno disruption",
    "S1": "S1\n1 path edge",
    "S2": "S2\nhighest-betweenness",
    "S3": "S3\n3 path edges",
    "S4": "S4\n5 path edges",
    "S5": "S5\ncorridor",
    "S6": "S6\nintersection",
}

#: Second lines short enough for six ticks inside a 3.5 in panel. The
#: descriptive wording in `SCENARIO_LABELS` is authoritative and unchanged;
#: these are abbreviations of it, and the IEEE caption spells each one out.
IEEE_SCENARIO_LABELS: dict[str, str] = {
    "S0": "S0\ncontrol",
    "S1": "S1\n1 edge",
    "S2": "S2\nbetwn.",
    "S3": "S3\n3 edges",
    "S4": "S4\n5 edges",
    "S5": "S5\ncorridor",
    "S6": "S6\nnode",
}

TRAFFIC_ORDER: tuple[str, ...] = ("light", "heavy")
TRAFFIC_LABELS: dict[str, str] = {"light": "SUMO light", "heavy": "SUMO heavy"}

# Warm vs cold is the project's central comparison; give it a fixed pairing.
START_COLORS: dict[str, str] = {"warm": "#D55E00", "cold": "#56B4E9"}
START_LABELS: dict[str, str] = {
    "warm": "warm start (retained state)",
    "cold": "cold start (rebuilt)",
}

# --- map layers ------------------------------------------------------------

BASEMAP_EDGE = "#D9D9D9"       # the street network, deliberately recessive
REMOVED_EDGE = "#000000"       # closed roads: black, drawn thick, on top
NODE_SOURCE = "#000000"
NODE_TARGET = "#000000"

# --- rc params -------------------------------------------------------------

#: Applied by `apply_style()`. Vector text stays editable in the SVG so the
#: figures can be relabelled for a paper without re-running anything.
RC: dict[str, object] = {
    "figure.dpi": 110,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.03,
    "font.size": 9,
    "axes.titlesize": 10,
    "axes.labelsize": 9,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.25,
    "grid.linewidth": 0.5,
    "legend.frameon": False,
    "legend.fontsize": 8,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "svg.fonttype": "none",
    "pdf.fonttype": 42,
}


# --- IEEE two-column rendering mode ---------------------------------------
#
# The screen figures are sized for a dashboard and a laptop; a paper
# reproduces a figure at 100% of its rendered size inside a fixed column, so
# the IEEE variant has to be rendered AT final size rather than scaled down.
# Scaling a 11 in figure into 3.5 in would put every tick label at ~2.5 pt.
#
# Geometry is the IEEE conference template's:
IEEE_COL_WIDTH_IN = 3.5     # one column
IEEE_PAGE_WIDTH_IN = 7.16   # both columns (a figure* / spanning float)

#: Overrides layered ON TOP of `RC`. Only geometry and type change -- the
#: palette is untouched, deliberately, because the colours are load-bearing
#: (see the module docstring on `physarum` vs `physarum_walk`).
#:
#: `savefig.bbox` is reset to None on purpose. The screen figures crop to
#: their content with bbox="tight", which is right for a dashboard but wrong
#: here: a cropped PDF is no longer 7.16 in wide, so `\includegraphics
#: [width=\textwidth]` would rescale it and move every font size off the
#: value chosen here. Every IEEE figure therefore lays its legend and labels
#: out INSIDE the figure rectangle with explicit margins.
#:
#: 8 pt is the floor for axis and tick labels: that is the smallest size
#: that stays legible at 100% reproduction. Supplementary text (per-box n,
#: in-panel annotations) goes to 6.5-7.5 pt, which is the trade the task
#: brief asks for -- shorten the text rather than shrink past the floor.
IEEE_RC: dict[str, object] = {
    "savefig.dpi": 600,          # PNG drafts; the PDF is vector regardless
    "savefig.bbox": None,        # exact physical size -- see above
    "savefig.pad_inches": 0.0,
    "savefig.transparent": False,
    "font.size": 8,
    "axes.titlesize": 8,
    "axes.labelsize": 8,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 7,
    "legend.handlelength": 1.6,
    "legend.handletextpad": 0.5,
    "legend.columnspacing": 1.1,
    "legend.borderaxespad": 0.2,
    "axes.linewidth": 0.6,
    "xtick.major.width": 0.6,
    "ytick.major.width": 0.6,
    "xtick.major.size": 2.5,
    "ytick.major.size": 2.5,
    "xtick.major.pad": 2.0,
    "ytick.major.pad": 2.0,
    "axes.labelpad": 2.5,
    "axes.titlepad": 3.0,
    "grid.linewidth": 0.4,
    "lines.linewidth": 1.1,
    "pdf.fonttype": 42,          # TrueType, embedded: what LaTeX wants
    "ps.fonttype": 42,
    "svg.fonttype": "none",
}

#: Formats written per mode. PDF first in IEEE mode because that is the one
#: LaTeX includes; the PNG is a 600 dpi draft and the SVG is free.
FORMATS: dict[str, tuple[str, ...]] = {
    "screen": ("png", "svg"),
    "ieee": ("pdf", "png", "svg"),
}

_MODE = "screen"


def set_mode(mode: str) -> None:
    """Select "screen" (default) or "ieee" rendering.

    Module-level rather than threaded through every signature because
    `maps.py` and `traces.py` call `apply_style()` from inside their own
    plot functions; a mode that lived only in an argument would be reset to
    screen by the next such call.
    """
    global _MODE
    if mode not in FORMATS:
        raise ValueError(f"unknown mode {mode!r}; known: {sorted(FORMATS)}")
    _MODE = mode


def mode() -> str:
    return _MODE


def is_ieee() -> bool:
    return _MODE == "ieee"


def pick(screen, ieee):
    """`screen` in screen mode, `ieee` in IEEE mode.

    Exists so the two variants share ONE code path per figure: a fork of
    the plotting code would drift, and the figures are the paper's
    evidence. Where a geometry constant differs between the two, it differs
    at exactly one `pick(...)` call.
    """
    return ieee if _MODE == "ieee" else screen


def output_formats() -> tuple[str, ...]:
    return FORMATS[_MODE]


def apply_style(mode: str | None = None) -> None:
    """Install the project rcParams. Call once per figure script.

    `mode=None` keeps whatever `set_mode` last selected, so a nested
    `apply_style()` inside a plotting function cannot silently drop the
    caller back to screen geometry.
    """
    if mode is not None:
        set_mode(mode)
    import matplotlib
    matplotlib.use("Agg")       # headless: these run under a sweep script
    import matplotlib.pyplot as plt
    plt.rcParams.update(RC)
    if is_ieee():
        plt.rcParams.update(IEEE_RC)


def color(algorithm: str) -> str:
    """Colour for `algorithm`, raising on an unknown name.

    Raises rather than falling back to a default so that a renamed or
    misspelled series fails loudly instead of silently drawing two
    algorithms in the same colour.
    """
    try:
        return ALGO_COLORS[algorithm]
    except KeyError:
        raise KeyError(
            f"no colour for algorithm {algorithm!r}; "
            f"known: {sorted(ALGO_COLORS)}"
        ) from None


def label(algorithm: str, compact: bool | None = None) -> str:
    """Display label for `algorithm`, raising on an unknown name.

    `compact=None` means "compact iff IEEE mode", so screen callers keep
    the long labels with no change at the call site.
    """
    table = IEEE_ALGO_LABELS if (
        is_ieee() if compact is None else compact
    ) else ALGO_LABELS
    try:
        return table[algorithm]
    except KeyError:
        raise KeyError(
            f"no label for algorithm {algorithm!r}; "
            f"known: {sorted(ALGO_LABELS)}"
        ) from None


def scenario_label(scenario: str) -> str:
    """Tick label for a scenario, abbreviated in IEEE mode."""
    table = IEEE_SCENARIO_LABELS if is_ieee() else SCENARIO_LABELS
    return table[scenario]
