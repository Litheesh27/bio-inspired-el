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


def apply_style() -> None:
    """Install the project rcParams. Call once per figure script."""
    import matplotlib
    matplotlib.use("Agg")       # headless: these run under a sweep script
    import matplotlib.pyplot as plt
    plt.rcParams.update(RC)


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


def label(algorithm: str) -> str:
    """Display label for `algorithm`, raising on an unknown name."""
    try:
        return ALGO_LABELS[algorithm]
    except KeyError:
        raise KeyError(
            f"no label for algorithm {algorithm!r}; "
            f"known: {sorted(ALGO_LABELS)}"
        ) from None
