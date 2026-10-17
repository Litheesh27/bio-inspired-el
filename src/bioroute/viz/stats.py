"""Paired statistics over the Phase 8 sweep's Parquet evidence.

IMPLEMENTED IN: Phase 9 (9a). Pure computation -- no plotting, no file
writes. `scripts/04_make_figures.py` renders what this module returns.

Everything here derives from ONE file,
`data/results/final_14a535c02b83.parquet` (5,520 rows x 38 columns,
committed as the project's experimental evidence). That file is read-only:
no function in this module writes to it or regenerates it.

WHY THE STATISTICS LOOK THE WAY THEY DO
=======================================

Four methodological constraints come from the project's own measurement
history, and each exists because the naive alternative produced a number
that was wrong in a way nobody would have caught downstream.

1. MEDIAN + IQR, NEVER MEANS. Recovery iterations and wall-clock are
   heavily right-skewed (Physarum cold recovery runs 73-150+ iterations;
   ACO wall-clock spans two orders of magnitude). A mean over that is a
   number about the tail, reported as if it were about the typical case.

2. PAIRED TESTS, keyed on `(od_source, od_target, scenario_id,
   traffic_level, seed)`. The sweep is fully paired BY CONSTRUCTION: every
   algorithm, warm and cold, saw the identical OD pair, the identical
   closure set and the identical traffic snapshot, and shared ONE Dijkstra
   denominator per cell (Phase 6). An unpaired test (Mann-Whitney) throws
   that design away and spends its power on between-OD-pair variance --
   the largest source of variance in the sweep and the one the design
   already controls for.

3. TWO-STAGE CLUSTERED BOOTSTRAP for CIs on medians. Rows from one OD pair
   are NOT independent: same source and target, same corridor, same
   congestion. So the bootstrap resamples the 30 OD PAIRS with replacement
   first, then resamples seeds within each drawn pair. This is the
   discipline Phase 4d established -- and Phase 4d is also why single-seed
   in-sample numbers are not reportable in this project: its seed 1 gave a
   pass and its seed 4 a clear miss on the same question.

4. PHYSARUM'S STRETCH IS NEVER POOLED ACROSS PATH PROVENANCE. This is
   CLAUDE.md's hardest Phase 9 requirement. ~70% of Physarum's reported
   paths come from a fallback rather than from its own max-flux walk, and
   Phase 8e measured median stretch of EXACTLY 1.000 inside all three
   provenance classes -- so a pooled median hides the entire effect and
   makes the Physarum-vs-Dijkstra comparison partly circular (a pooled
   1.000 can literally be Dijkstra's own path, via `full_graph_fallback`).
   Every table that reports Physarum quality therefore emits TWO labelled
   series:

     `physarum`       with-fallback: the `stretch` /
                      `post_disruption_stretch` columns as recorded
     `physarum_walk`  model-only: `walk_best_stretch` /
                      `post_disruption_walk_stretch`, i.e. what the
                      tube-reinforcement model's own extraction achieved

   The two share one row's Dijkstra denominator (Phase 8d), so they are
   directly comparable within a row.

A FIFTH CONSTRAINT, on wording rather than arithmetic: every
congestion-direction claim must name its congestion model. The sweep is
real SUMO on the `sumo_speeds` baseline, where congestion HURTS ACO.
Phase 4/4d measured the opposite sign on BPR / `config_speeds`. The sign
is a property of the congestion model, so `congestion_effect_table`
stamps `congestion_source` and `baseline_graph` into every row it returns.

FAILED ROWS. 28 rows (0.51%) carry `failed=True`, all from one cause:
scenario S4 could not cut 5 path edges on 2 cells without disconnecting
s-t. They are excluded from every quality statistic and the excluded count
is carried in an `n_failed_excluded` column on every table that drops them
-- CLAUDE.md forbids silently dropping a recorded failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd
from scipy.stats import wilcoxon

from . import style

# --- where the evidence lives ----------------------------------------------

#: src/bioroute/viz/stats.py -> repo root
ROOT = Path(__file__).resolve().parents[3]

#: The ONE sweep output. Phase 8e committed it; Phase 9 derives every number
#: from it and nothing else.
DEFAULT_PARQUET = ROOT / "data" / "results" / "final_14a535c02b83.parquet"

# --- pairing and clustering keys -------------------------------------------

#: The sweep's pairing key. Two rows sharing these five values solved the
#: IDENTICAL problem instance, so their difference is attributable to the
#: thing being compared (algorithm, or warm vs cold) and nothing else.
PAIR_KEYS: tuple[str, ...] = (
    "od_source", "od_target", "scenario_id", "traffic_level", "seed",
)

#: Bootstrap stage 1 resamples these clusters. Rows sharing an OD pair are
#: correlated; treating them as 5,492 independent draws would understate
#: every CI by roughly sqrt(rows per pair).
CLUSTER_KEYS: tuple[str, ...] = ("od_source", "od_target")

#: Bootstrap stage 2 resamples these within a drawn cluster. Physarum and
#: the classical routers ran at ONE seed (Phase 8 MEASURED Physarum's seed
#: invariance rather than assuming it), so for them stage 2 degenerates to
#: "take the cluster's rows" -- correct, and the reason the function reports
#: `n_within_per_cluster`.
WITHIN_KEYS: tuple[str, ...] = ("seed",)

#: Fixed so the CIs are reproducible byte-for-byte. A fresh generator is
#: built per call DELIBERATELY: a CI must not depend on how many other CIs
#: were computed before it in the same process.
BOOTSTRAP_SEED = 20260101
N_BOOT = 10_000

#: `stretch == 1.0` means "matched Dijkstra exactly". The tolerance guards
#: against a last-bit float artefact in the cost sum; it is far tighter than
#: any real routing difference on this graph (edge times are ~1e0 seconds).
EXACT_TOL = 1e-9
WITHIN_10PCT = 1.1

# --- the series every quality table must emit ------------------------------


@dataclass(frozen=True)
class QualitySeries:
    """One reported/plotted quality series.

    `key` is the `viz.style` key (so colour and label are decided in one
    place), `algorithm` is the Parquet value to filter on, and the two
    columns say which measurement this series reads. The split between
    `physarum` and `physarum_walk` is the provenance requirement above
    expressed as data, so no caller has to remember it.
    """

    key: str
    algorithm: str
    pre_col: str
    post_col: str

    @property
    def label(self) -> str:
        return style.label(self.key)


QUALITY_SERIES: tuple[QualitySeries, ...] = (
    QualitySeries("dijkstra", "dijkstra", "stretch", "post_disruption_stretch"),
    QualitySeries("astar", "astar", "stretch", "post_disruption_stretch"),
    QualitySeries("aco", "aco", "stretch", "post_disruption_stretch"),
    QualitySeries("physarum", "physarum", "stretch", "post_disruption_stretch"),
    QualitySeries(
        "physarum_walk", "physarum",
        "walk_best_stretch", "post_disruption_walk_stretch",
    ),
)

#: Algorithms that have a recovery to measure. The model-only series is NOT
#: here: the max-flux walk is an extraction step, not a separate solver, so
#: it shares `recovery_iterations` with `physarum` exactly. Reporting it as a
#: fifth recovery series would double-count one measurement.
RECOVERY_ALGOS: tuple[str, ...] = ("dijkstra", "astar", "aco", "physarum")

# --- schema expectations ---------------------------------------------------

REQUIRED_COLUMNS: tuple[str, ...] = (
    "run_id", "algorithm", "od_source", "od_target", "scenario_id",
    "traffic_level", "congestion_source", "baseline_graph", "baseline_hash",
    "warm_start", "seed", "config_hash", "timestamp_utc",
    "iterations", "iteration_budget", "converged", "wallclock_s",
    "peak_memory_kb", "failed", "failure_reason", "n_removed_edges",
    "path_cost", "stretch", "path_hops",
    "initial_time_to_first_valid_path_iters",
    "recovery_iterations", "recovery_wallclock_s", "recovery_converged",
    "time_to_first_valid_path_iters", "post_disruption_cost",
    "post_disruption_stretch", "extraction_method",
    "best_path_extraction_method", "ant_completion_rate",
    "walk_best_cost", "walk_best_stretch", "walk_success_count",
    "post_disruption_walk_stretch",
)

#: Every column on which `stretch >= 1.0` is an invariant, not a hope.
#: CLAUDE.md: "`stretch < 1.0` is a bug -- assert it."
STRETCH_COLUMNS: tuple[str, ...] = (
    "stretch", "post_disruption_stretch",
    "walk_best_stretch", "post_disruption_walk_stretch",
)

#: Facts Phase 8e established about the canonical sweep file. Checked only
#: when that file is what was loaded, so unit tests can hand `load_results`
#: a small synthetic frame and still get the INVARIANT checks.
CANONICAL_FACTS: dict[str, Any] = {
    "n_rows": 5520,
    "n_columns": 38,
    "algorithm_counts": {
        "aco": 3900, "physarum": 780, "dijkstra": 420, "astar": 420,
    },
    "n_od_pairs": 30,
    "n_cells": 780,
    "n_failed": 28,
    "n_failure_reasons": 1,
}

#: Cell identity: one matrix cell, in which all algorithms shared a single
#: Dijkstra reference cost.
CELL_KEYS: tuple[str, ...] = (
    "od_source", "od_target", "scenario_id", "traffic_level", "warm_start",
)


class ResultsValidationError(ValueError):
    """The results file contradicts an invariant or a Phase 8e fact.

    Raised rather than warned: a stretch below 1.0 or a duplicated run_id
    means the evidence is not what every figure assumes it is, and a figure
    built on it would be quietly wrong.
    """


# --- loading ---------------------------------------------------------------


def load_results(
    path: str | Path | None = None,
    *,
    expect_canonical: bool | None = None,
) -> pd.DataFrame:
    """Load the sweep Parquet and validate it before returning it.

    `expect_canonical` defaults to "True iff this is the committed sweep
    file", so the row-count / per-algorithm-count checks fire on the real
    evidence and are skipped for the synthetic frames in the tests. The
    INVARIANT checks (stretch >= 1.0, unique run_id, S0 has no recovery,
    walk cost >= reported cost) always fire.
    """
    target = Path(path) if path is not None else DEFAULT_PARQUET
    if expect_canonical is None:
        expect_canonical = target.resolve() == DEFAULT_PARQUET.resolve()
    if not target.exists():
        raise FileNotFoundError(
            f"results Parquet not found at {target}. This file is committed "
            f"to git as the Phase 8 evidence; Phase 9 never regenerates it. "
            f"Check out the repo's data/results/ directory."
        )
    df = pd.read_parquet(target)
    validate_results(df, canonical=bool(expect_canonical), source=str(target))
    return df


def validate_results(
    df: pd.DataFrame, *, canonical: bool = False, source: str = "<frame>",
) -> None:
    """Raise `ResultsValidationError` on anything that breaks an invariant."""
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ResultsValidationError(
            f"{source}: missing {len(missing)} required column(s): {missing}"
        )

    dup = int(df["run_id"].duplicated().sum())
    if dup:
        raise ResultsValidationError(
            f"{source}: {dup} duplicated run_id -- run_id is the sweep's "
            f"resume key, so a duplicate means a cell was counted twice"
        )

    for col in STRETCH_COLUMNS:
        bad = df.loc[df[col].notna() & (df[col] < 1.0), col]
        if len(bad):
            raise ResultsValidationError(
                f"{source}: {len(bad)} row(s) with {col} < 1.0 "
                f"(min {bad.min():.6f}). stretch = cost_alg / cost_dijkstra "
                f"against the SAME weighted graph, so a value below 1.0 "
                f"beats Dijkstra and is a bug by definition (CLAUDE.md)."
            )

    both = df["walk_best_cost"].notna() & df["path_cost"].notna()
    bad_walk = df.loc[both & (df["walk_best_cost"] < df["path_cost"] - 1e-9)]
    if len(bad_walk):
        raise ResultsValidationError(
            f"{source}: {len(bad_walk)} row(s) with walk_best_cost < "
            f"path_cost. The pooled best is a min that already INCLUDES the "
            f"walk, so a cheaper walk means the two trackers stopped "
            f"measuring the same run (Phase 8d)."
        )

    s0 = df[df["scenario_id"] == "S0"]
    for col in ("post_disruption_stretch", "recovery_iterations",
                "post_disruption_walk_stretch"):
        n_bad = int(s0[col].notna().sum())
        if n_bad:
            raise ResultsValidationError(
                f"{source}: {n_bad} S0 row(s) have a non-null {col}. S0 is "
                f"the no-disruption control; a value there would average a "
                f"non-existent recovery into the results."
            )

    if not canonical:
        return

    facts = CANONICAL_FACTS
    if len(df) != facts["n_rows"] or df.shape[1] != facts["n_columns"]:
        raise ResultsValidationError(
            f"{source}: expected {facts['n_rows']} rows x "
            f"{facts['n_columns']} columns, got {df.shape}"
        )
    counts = df["algorithm"].value_counts().to_dict()
    if counts != facts["algorithm_counts"]:
        raise ResultsValidationError(
            f"{source}: per-algorithm counts {counts} != designed "
            f"{facts['algorithm_counts']}"
        )
    n_pairs = df.groupby(["od_source", "od_target"]).ngroups
    if n_pairs != facts["n_od_pairs"]:
        raise ResultsValidationError(
            f"{source}: {n_pairs} OD pairs, expected {facts['n_od_pairs']}"
        )
    n_cells = df.groupby(list(CELL_KEYS)).ngroups
    if n_cells != facts["n_cells"]:
        raise ResultsValidationError(
            f"{source}: {n_cells} matrix cells, expected {facts['n_cells']}"
        )
    n_failed = int(df["failed"].sum())
    reasons = sorted(df.loc[df["failed"], "failure_reason"].unique())
    if (n_failed != facts["n_failed"]
            or len(reasons) != facts["n_failure_reasons"]):
        raise ResultsValidationError(
            f"{source}: {n_failed} failed row(s) with {len(reasons)} distinct "
            f"reason(s), expected {facts['n_failed']} with "
            f"{facts['n_failure_reasons']}: {reasons}"
        )


# --- small descriptive helpers ---------------------------------------------


def median_iqr(series: Iterable[float] | pd.Series) -> dict[str, Any]:
    """Median and interquartile range, NaNs dropped.

    The IQR -- not a standard deviation -- because these distributions are
    skewed and, for Physarum warm recovery, outright bimodal (48.3% of warm
    recoveries take exactly 1 iteration). A spread statistic that assumes
    one mode would describe a distribution the data does not have.
    """
    s = series if isinstance(series, pd.Series) else pd.Series(list(series))
    s = pd.to_numeric(s, errors="coerce").dropna()
    if s.empty:
        return {"n": 0, "median": None, "q1": None, "q3": None, "iqr": None}
    q1 = float(s.quantile(0.25))
    q3 = float(s.quantile(0.75))
    return {
        "n": int(s.size),
        "median": float(s.median()),
        "q1": q1,
        "q3": q3,
        "iqr": q3 - q1,
    }


def _share(values: pd.Series, predicate: np.ndarray, n: int) -> float | None:
    """Share over an EXPLICIT denominator `n`, so nulls cannot hide.

    Used for the walk-only series, where a null means "the max-flux walk
    never completed a path" -- which is not an exact match, so it has to
    count against the denominator rather than vanish from it.
    """
    if n == 0:
        return None
    return float(predicate.sum()) / float(n)


# --- the two-stage clustered bootstrap -------------------------------------


def bootstrap_median_ci(
    df: pd.DataFrame,
    value_col: str,
    *,
    n_boot: int = N_BOOT,
    rng: np.random.Generator | int | None = None,
    cluster_keys: Sequence[str] = CLUSTER_KEYS,
    within_keys: Sequence[str] = WITHIN_KEYS,
    ci: float = 0.95,
) -> dict[str, Any]:
    """Percentile CI for the median of `value_col`, clustered on OD pair.

    Stage 1 resamples the OD-pair clusters with replacement; stage 2
    resamples, within each drawn cluster, the distinct `within_keys` groups
    (the seeds) with replacement and takes all of each drawn group's rows.
    Both stages matter, for different reasons: stage 1 because an OD pair is
    the unit that generalises (30 pairs is the real sample size, not 5,492
    rows), stage 2 because Phase 4d showed per-seed swings large enough to
    flip a pass/fail verdict.

    `rng` defaults to a FRESH `default_rng(BOOTSTRAP_SEED)`, so the CI for a
    given frame and column is byte-for-byte reproducible regardless of what
    else was computed first in the same process.
    """
    generator = (
        np.random.default_rng(BOOTSTRAP_SEED) if rng is None
        else rng if isinstance(rng, np.random.Generator)
        else np.random.default_rng(rng)
    )
    cols = [*cluster_keys, *within_keys, value_col]
    sub = df.loc[:, cols].dropna(subset=[value_col])
    n = len(sub)
    out: dict[str, Any] = {
        "value_col": value_col,
        "n": n,
        "n_clusters": 0,
        "n_boot": int(n_boot),
        "ci_level": float(ci),
        "bootstrap_seed": BOOTSTRAP_SEED,
        "median": None,
        "ci_low": None,
        "ci_high": None,
        "n_within_per_cluster": None,
    }
    if n == 0:
        return out

    values = sub[value_col].to_numpy(dtype=float)
    out["median"] = float(np.median(values))

    # Group rows by (cluster, within) and the groups by cluster, as flat
    # CSR-style arrays: a replicate then costs a handful of vectorised numpy
    # calls instead of a Python loop over 30 clusters x 10,000 replicates.
    group_id = pd.MultiIndex.from_frame(
        sub.loc[:, [*cluster_keys, *within_keys]]
    ).factorize()[0]
    cluster_id = pd.MultiIndex.from_frame(
        sub.loc[:, list(cluster_keys)]
    ).factorize()[0]

    row_order = np.argsort(group_id, kind="stable")
    n_groups = int(group_id.max()) + 1
    g_len = np.bincount(group_id[row_order], minlength=n_groups)
    g_start = np.concatenate([[0], np.cumsum(g_len)[:-1]])

    # One row per group gives that group's cluster.
    group_cluster = cluster_id[row_order[g_start]]
    n_clusters = int(cluster_id.max()) + 1
    cg_order = np.argsort(group_cluster, kind="stable")
    c_len = np.bincount(group_cluster[cg_order], minlength=n_clusters)
    c_start = np.concatenate([[0], np.cumsum(c_len)[:-1]])

    out["n_clusters"] = n_clusters
    out["n_within_per_cluster"] = float(np.mean(c_len))

    medians = np.empty(n_boot, dtype=float)
    for b in range(n_boot):
        drawn_clusters = generator.integers(0, n_clusters, n_clusters)
        reps = c_len[drawn_clusters]          # groups to draw from each
        total_groups = int(reps.sum())
        if total_groups == 0:                 # pathological; guard, not fix
            medians[b] = np.nan
            continue
        base = np.repeat(c_start[drawn_clusters], reps)
        span = np.repeat(c_len[drawn_clusters], reps)
        picks = base + (generator.random(total_groups) * span).astype(np.intp)
        drawn_groups = cg_order[picks]

        lens = g_len[drawn_groups]
        total_rows = int(lens.sum())
        offsets = np.repeat(np.cumsum(lens) - lens, lens)
        idx = np.repeat(g_start[drawn_groups], lens) + (
            np.arange(total_rows) - offsets
        )
        medians[b] = np.median(values[row_order[idx]])

    alpha = (1.0 - ci) / 2.0
    lo, hi = np.nanpercentile(medians, [100 * alpha, 100 * (1 - alpha)])
    out["ci_low"] = float(lo)
    out["ci_high"] = float(hi)
    return out


# --- paired tests ----------------------------------------------------------


def paired_frame(
    df: pd.DataFrame,
    value_col: str,
    group_col: str,
    a: Any,
    b: Any,
    *,
    keys: Sequence[str] = PAIR_KEYS,
) -> pd.DataFrame:
    """The matched pairs themselves: one row per pairing key.

    Separated from `paired_wilcoxon` so the figures can draw the per-pair
    deltas that the test summarises -- a figure showing only the summary
    statistic hides a bimodal difference distribution, which is exactly the
    failure mode Phase 8e's "24x" came from.

    Columns: `value_a`, `value_b`, `diff` (b - a), `ratio` (b / a).
    `n_a_only` / `n_b_only` are carried on `.attrs`, because a pair that
    exists on one side only is a COVERAGE fact, not a missing value: on
    `post_disruption_walk_stretch` the warm side has a path on 130 cells
    and the cold side on 213, and ignoring that reverses the apparent sign
    of the effect.
    """
    key_list = list(keys)
    sub = df.loc[:, [*key_list, group_col, value_col]].dropna(
        subset=[value_col]
    )
    sides: dict[str, pd.Series] = {}
    for name, want in (("a", a), ("b", b)):
        side = sub[sub[group_col] == want].set_index(key_list)[value_col]
        if side.index.duplicated().any():
            raise ValueError(
                f"paired_frame: {group_col}={want!r} has multiple rows per "
                f"{tuple(key_list)}; the pairing key must identify exactly "
                f"one row per side or the pairing is not a pairing"
            )
        sides[name] = side

    common = sides["a"].index.intersection(sides["b"].index)
    out = pd.DataFrame({
        "value_a": sides["a"].loc[common].astype(float),
        "value_b": sides["b"].loc[common].astype(float),
    })
    out["diff"] = out["value_b"] - out["value_a"]
    out["ratio"] = np.where(
        out["value_a"] != 0.0, out["value_b"] / out["value_a"], np.nan
    )
    out.attrs["n_a_only"] = int(
        len(sides["a"].index.difference(sides["b"].index))
    )
    out.attrs["n_b_only"] = int(
        len(sides["b"].index.difference(sides["a"].index))
    )
    out.attrs["group_a"] = a
    out.attrs["group_b"] = b
    out.attrs["value_col"] = value_col
    return out


def paired_wilcoxon(
    df: pd.DataFrame,
    value_col: str,
    group_col: str,
    a: Any,
    b: Any,
    *,
    keys: Sequence[str] = PAIR_KEYS,
) -> dict[str, Any]:
    """Wilcoxon signed-rank on `b - a`, paired on `keys`.

    LOWER IS BETTER for every metric this is used on (stretch, recovery
    iterations, wall-clock), so `n_a_better` counts pairs where a < b.

    Exact zero differences are DROPPED before the test -- the signed-rank
    statistic is defined on nonzero differences -- and reported as
    `n_tied`, because on this data a tie is signal rather than noise: it
    usually means both configurations found the same optimal path.

    `median_paired_ratio` is the median of the PER-PAIR ratio b/a, never a
    ratio of the two medians. The distinction is load-bearing: Physarum's
    warm recovery is bimodal, and a ratio of medians there yields a bogus
    "24x" (Phase 8e) where the paired median ratio is 2.97x.
    """
    pairs = paired_frame(df, value_col, group_col, a, b, keys=keys)
    va = pairs["value_a"].to_numpy(dtype=float)
    vb = pairs["value_b"].to_numpy(dtype=float)
    diff = pairs["diff"].to_numpy(dtype=float)
    ratio = pairs["ratio"].to_numpy(dtype=float)
    nonzero = diff[diff != 0.0]

    out: dict[str, Any] = {
        "value_col": value_col,
        "group_col": group_col,
        "group_a": _plain(a),
        "group_b": _plain(b),
        "keys": list(keys),
        "n_pairs": int(len(pairs)),
        "n_a_only": int(pairs.attrs["n_a_only"]),
        "n_b_only": int(pairs.attrs["n_b_only"]),
        "median_a": float(np.median(va)) if len(va) else None,
        "median_b": float(np.median(vb)) if len(vb) else None,
        "median_paired_ratio": (
            float(np.nanmedian(ratio))
            if len(ratio) and np.isfinite(ratio).any() else None
        ),
        "n_a_better": int((va < vb).sum()),
        "n_b_better": int((vb < va).sum()),
        "n_tied": int((diff == 0.0).sum()),
        "n_nonzero": int(len(nonzero)),
        "statistic": None,
        "pvalue": None,
        "note": "lower is better; ratio is per-pair b/a, then medianed",
    }
    if len(nonzero) >= 1:
        res = wilcoxon(nonzero, alternative="two-sided")
        out["statistic"] = float(res.statistic)
        out["pvalue"] = float(res.pvalue)
    return out


# --- tables ----------------------------------------------------------------


def _successful(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop `failed=True` rows and return how many were dropped."""
    ok = df[~df["failed"].astype(bool)]
    return ok, int(len(df) - len(ok))


def _n_failed(df: pd.DataFrame, **equals: Any) -> int:
    """Failed rows matching `equals`, for a PER-GROUP exclusion count.

    A table-wide "28 excluded" would be true but useless: the reader wants
    to know what was dropped from the row in front of them. The sweep's 28
    failures are all S4, split aco 20 / physarum 4 / dijkstra 2 / astar 2.
    """
    mask = df["failed"].astype(bool)
    for col, want in equals.items():
        if want is None:
            continue
        mask &= df[col] == want
    return int(mask.sum())


def _dedup_initial(df: pd.DataFrame) -> pd.DataFrame:
    """One row per initial (pre-disruption) solve.

    MEASURED, not assumed: the pre-disruption `stretch` is IDENTICAL across
    all 7 scenarios and both warm/cold for a given (algorithm, od, traffic,
    seed) -- 300/300 ACO groups and 60/60 Physarum groups hold exactly one
    distinct value, because the initial solve happens BEFORE any closure.
    Pooling every row would count one solve up to 13 times and shrink the
    CIs by a factor that is pure bookkeeping.
    """
    return df.drop_duplicates(
        subset=["algorithm", "od_source", "od_target", "traffic_level", "seed"]
    )


def initial_solves(df: pd.DataFrame) -> pd.DataFrame:
    """Public wrapper over `_dedup_initial`, for the figures.

    Exposed because `figures.py` needs exactly the same deduplicated view
    the quality table uses -- two different views of "the initial solve"
    in one report would be a bug waiting to be published.
    """
    return _dedup_initial(df)


def quality_table(
    df: pd.DataFrame, *, n_boot: int = N_BOOT, with_ci: bool = True,
) -> pd.DataFrame:
    """Pre-disruption path quality per series x traffic level.

    Physarum appears as TWO rows per traffic level -- with-fallback
    (`physarum`) and model-only (`physarum_walk`) -- never one. See the
    module docstring for why that is a requirement rather than a courtesy.

    Two denominators are reported on purpose. `n` is every successful row
    of that series; `n_with_value` is the subset where the measurement
    exists. They differ only for `physarum_walk`, where a null means the
    max-flux walk never completed a path at all -- which is NOT an exact
    match, so the shares are over `n` while median/IQR/CI are necessarily
    over `n_with_value`.
    """
    ok, _ = _successful(df)
    initial = _dedup_initial(ok)
    rows: list[dict[str, Any]] = []
    for series in QUALITY_SERIES:
        sel = initial[initial["algorithm"] == series.algorithm]
        if sel.empty:
            continue
        # "all" is traffic-pooled: both SUMO levels sit on the same
        # sumo_speeds baseline graph and each stretch is already normalised
        # against its own level's Dijkstra, so pooling the RATIOS is
        # legitimate even though pooling the COSTS would not be.
        for traffic in (*style.TRAFFIC_ORDER, "all"):
            grp = (
                sel if traffic == "all"
                else sel[sel["traffic_level"] == traffic]
            )
            if grp.empty:
                continue
            vals = pd.to_numeric(grp[series.pre_col], errors="coerce")
            present = vals.dropna()
            n = int(len(grp))
            desc = median_iqr(present)
            row: dict[str, Any] = {
                "series": series.key,
                "label": series.label,
                "algorithm": series.algorithm,
                "traffic_level": traffic,
                "value_col": series.pre_col,
                "n": n,
                "n_with_value": desc["n"],
                "median": desc["median"],
                "q1": desc["q1"],
                "q3": desc["q3"],
                "iqr": desc["iqr"],
                "exact_match_share": _share(
                    present, (present <= 1.0 + EXACT_TOL).to_numpy(), n
                ),
                "within_10pct_share": _share(
                    present, (present <= WITHIN_10PCT).to_numpy(), n
                ),
                # Failed rows for THIS series, excluded from the stats
                # above. On the sweep these are all S4 rows, and because
                # the initial solve is identical across scenarios the
                # dedup still recovers every initial solve from a
                # surviving scenario -- so no measurement is lost here,
                # only the rows are.
                "n_failed_excluded": _n_failed(
                    df,
                    algorithm=series.algorithm,
                    traffic_level=None if traffic == "all" else traffic,
                ),
            }
            if with_ci:
                boot = bootstrap_median_ci(grp, series.pre_col, n_boot=n_boot)
                row["ci_low"] = boot["ci_low"]
                row["ci_high"] = boot["ci_high"]
                row["n_clusters"] = boot["n_clusters"]
            rows.append(row)
    return pd.DataFrame(rows)


def recovery_table(
    df: pd.DataFrame, *, n_boot: int = N_BOOT, with_ci: bool = True,
) -> pd.DataFrame:
    """Recovery cost per algorithm x warm/cold.

    Only S1-S6 contribute: S0 has no disruption, so its recovery columns
    are null by schema and a zero there would be a fabricated measurement.

    `share_1_iteration` is the headline SHAPE of the warm-start result, not
    a footnote: 48.3% of Physarum's warm recoveries converge in exactly one
    iteration while cold start's MINIMUM is 73. The column exists because
    the distribution is bimodal and a median hides the spike.

    Classical routers have cold rows only (a one-shot router has no warm
    mode), so their warm rows are legitimately absent rather than empty.
    """
    ok, _ = _successful(df)
    have = ok[ok["recovery_iterations"].notna()]
    rows: list[dict[str, Any]] = []
    for algo in RECOVERY_ALGOS:
        for warm in (True, False):
            grp = have[(have["algorithm"] == algo)
                       & (have["warm_start"].astype(bool) == warm)]
            if grp.empty:
                continue
            iters = pd.to_numeric(grp["recovery_iterations"], errors="coerce")
            wall = pd.to_numeric(grp["recovery_wallclock_s"], errors="coerce")
            d_it, d_wa = median_iqr(iters), median_iqr(wall)
            row: dict[str, Any] = {
                "algorithm": algo,
                "label": style.label(algo),
                "start": "warm" if warm else "cold",
                "n": int(len(grp)),
                "iters_median": d_it["median"],
                "iters_q1": d_it["q1"],
                "iters_q3": d_it["q3"],
                "wall_median_s": d_wa["median"],
                "wall_q1_s": d_wa["q1"],
                "wall_q3_s": d_wa["q3"],
                "share_1_iteration": float((iters == 1).mean()),
                "iters_min": float(iters.min()),
                "iters_max": float(iters.max()),
                # Proof that warm start buys FEWER iterations rather than
                # cheaper ones -- the claim the project set out to test.
                "median_s_per_iteration": float((wall / iters).median()),
                "n_failed_excluded": _n_failed(
                    df, algorithm=algo, warm_start=warm
                ),
            }
            if with_ci:
                b_it = bootstrap_median_ci(
                    grp, "recovery_iterations", n_boot=n_boot
                )
                b_wa = bootstrap_median_ci(
                    grp, "recovery_wallclock_s", n_boot=n_boot
                )
                row["iters_ci_low"] = b_it["ci_low"]
                row["iters_ci_high"] = b_it["ci_high"]
                row["wall_ci_low_s"] = b_wa["ci_low"]
                row["wall_ci_high_s"] = b_wa["ci_high"]
                row["n_clusters"] = b_it["n_clusters"]
            rows.append(row)
    return pd.DataFrame(rows)


def warm_cold_table(df: pd.DataFrame) -> pd.DataFrame:
    """Paired warm-vs-cold comparison, one row per algorithm x metric.

    The ONLY admissible form of this comparison (CLAUDE.md Phase 9
    requirement 1): paired on identical (od, scenario, traffic, seed) and
    reported as the median of per-pair ratios.
    `median_paired_ratio_cold_over_warm` > 1 means warm start won.
    """
    ok, _ = _successful(df)
    rows: list[dict[str, Any]] = []
    for algo in RECOVERY_ALGOS:
        grp = ok[ok["algorithm"] == algo]
        if grp["warm_start"].nunique() < 2:
            continue      # classical: cold only, nothing to pair
        for metric, is_quality in (
            ("recovery_iterations", False),
            ("recovery_wallclock_s", False),
            ("post_disruption_stretch", True),
            ("post_disruption_walk_stretch", True),
        ):
            if grp[metric].notna().sum() == 0:
                continue
            res = paired_wilcoxon(grp, metric, "warm_start", True, False)
            # A walk column is the MODEL-ONLY series even on a Physarum
            # row, so it must not be labelled "with fallback" -- that is
            # the pooling confusion this project exists to avoid.
            series_key = (
                "physarum_walk"
                if algo == "physarum" and "walk" in metric else algo
            )
            rows.append({
                "algorithm": algo,
                "series": series_key,
                "label": style.label(series_key),
                "metric": metric,
                "is_quality_metric": is_quality,
                "n_pairs": res["n_pairs"],
                # Coverage, NOT a footnote: on
                # `post_disruption_walk_stretch` the warm side has a value
                # on far fewer cells than the cold side (the warm walk
                # dead-ends more often), so an UNPAIRED comparison of the
                # two medians compares two differently-composed samples and
                # reverses the sign of the effect. These two columns are how
                # a reader sees that before quoting the medians.
                "n_warm_only": res["n_a_only"],
                "n_cold_only": res["n_b_only"],
                "median_warm": res["median_a"],
                "median_cold": res["median_b"],
                "median_paired_ratio_cold_over_warm": res["median_paired_ratio"],
                "n_warm_better": res["n_a_better"],
                "n_cold_better": res["n_b_better"],
                "n_tied": res["n_tied"],
                "statistic": res["statistic"],
                "pvalue": res["pvalue"],
            })
    return pd.DataFrame(rows)


def provenance_table(df: pd.DataFrame) -> pd.DataFrame:
    """Where Physarum's REPORTED path actually came from.

    This table is the evidence for the no-pooling rule. Phase 8e: the
    median stretch is exactly 1.000 inside EVERY provenance class, so a
    pooled median cannot distinguish "the model found the optimum" from
    "the fallback handed back Dijkstra's own path".

    `best_path_extraction_method` is used (not `extraction_method`): it
    records the method that produced the path actually REPORTED, tracked at
    the iteration where `best_cost` improved, whereas `extraction_method`
    only says how the final iteration happened to end (Phase 8b).
    """
    ok, _ = _successful(df)
    n_failed = _n_failed(df, algorithm="physarum")
    phys = ok[(ok["algorithm"] == "physarum")
              & ok["best_path_extraction_method"].notna()]
    total = int(len(phys))
    rows: list[dict[str, Any]] = []
    for method, grp in phys.groupby("best_path_extraction_method"):
        vals = pd.to_numeric(grp["stretch"], errors="coerce").dropna()
        desc = median_iqr(vals)
        rows.append({
            "best_path_extraction_method": str(method),
            "is_model_own_extraction": method == "max_flux_walk",
            "n": int(len(grp)),
            "share_of_physarum_rows": (
                int(len(grp)) / total if total else None
            ),
            "median_stretch": desc["median"],
            "q1": desc["q1"],
            "q3": desc["q3"],
            "exact_match_share": _share(
                vals, (vals <= 1.0 + EXACT_TOL).to_numpy(), int(len(grp))
            ),
            "median_post_disruption_stretch": median_iqr(
                pd.to_numeric(grp["post_disruption_stretch"], errors="coerce")
            )["median"],
            "n_failed_excluded": n_failed,
        })
    if not rows:
        return pd.DataFrame(columns=[
            "best_path_extraction_method", "is_model_own_extraction", "n",
            "share_of_physarum_rows", "median_stretch", "q1", "q3",
            "exact_match_share", "median_post_disruption_stretch",
            "n_failed_excluded",
        ])
    return pd.DataFrame(rows).sort_values(
        "n", ascending=False, ignore_index=True
    )


def scenario_table(df: pd.DataFrame) -> pd.DataFrame:
    """Post-disruption quality and recovery cost per scenario x series.

    S0 is absent BY CONSTRUCTION, not by filtering preference: it is the
    no-disruption control and has no post-disruption measurement. Plotting
    it as a zero is the specific mistake this omission avoids.

    `n_failed_excluded` is per scenario AND per algorithm here, which is
    where the S4 infeasibility surfaces -- all 28 failures are S4.
    """
    rows: list[dict[str, Any]] = []
    for scenario in style.SCENARIO_ORDER:
        if scenario == "S0":
            continue
        sc_all = df[df["scenario_id"] == scenario]
        if sc_all.empty:
            continue
        sc, _ = _successful(sc_all)
        for series in QUALITY_SERIES:
            grp = sc[sc["algorithm"] == series.algorithm]
            if grp.empty:
                continue
            post = pd.to_numeric(grp[series.post_col], errors="coerce")
            present = post.dropna()
            d_post = median_iqr(present)
            d_it = median_iqr(
                pd.to_numeric(grp["recovery_iterations"], errors="coerce")
            )
            d_wa = median_iqr(
                pd.to_numeric(grp["recovery_wallclock_s"], errors="coerce")
            )
            rows.append({
                "scenario_id": scenario,
                "series": series.key,
                "label": series.label,
                "algorithm": series.algorithm,
                "post_col": series.post_col,
                "n": int(len(grp)),
                "n_with_post_value": d_post["n"],
                "post_stretch_median": d_post["median"],
                "post_stretch_q1": d_post["q1"],
                "post_stretch_q3": d_post["q3"],
                "post_stretch_max": (
                    float(present.max()) if len(present) else None
                ),
                "post_exact_match_share": _share(
                    present,
                    (present <= 1.0 + EXACT_TOL).to_numpy(),
                    int(len(grp)),
                ),
                "recovery_iters_median": d_it["median"],
                "recovery_iters_q1": d_it["q1"],
                "recovery_iters_q3": d_it["q3"],
                "recovery_wall_median_s": d_wa["median"],
                "n_removed_edges_median": float(
                    pd.to_numeric(grp["n_removed_edges"]).median()
                ),
                "n_failed_excluded": _n_failed(
                    sc_all, algorithm=series.algorithm
                ),
            })
    return pd.DataFrame(rows)


def congestion_effect_table(df: pd.DataFrame) -> pd.DataFrame:
    """Light vs heavy, paired on (od, seed), on the UNDISRUPTED solve.

    Every row stamps `congestion_source` and `baseline_graph`, because the
    SIGN of this effect is a property of the congestion model and not of
    the algorithm: Phase 4/4d found congestion HELPED ACO on BPR /
    `config_speeds`; this sweep is real SUMO on `sumo_speeds` and finds it
    HURTS. Any claim from this table must name its model (CLAUDE.md Phase 9
    requirement 2).

    On comparing stretch across traffic levels: the COSTS are not
    comparable across differently-weighted graphs (CLAUDE.md), but each
    stretch is already normalised by Dijkstra ON ITS OWN level, so the
    dimensionless ratios are.
    """
    ok, _ = _successful(df)
    initial = _dedup_initial(ok)
    # The initial solve ignores the scenario entirely, so the pairing key
    # here drops scenario_id and warm_start and keeps (od, seed).
    keys = ("od_source", "od_target", "seed")
    rows: list[dict[str, Any]] = []
    for series in QUALITY_SERIES:
        sel = initial[initial["algorithm"] == series.algorithm]
        if sel.empty or sel[series.pre_col].notna().sum() == 0:
            continue
        res = paired_wilcoxon(
            sel, series.pre_col, "traffic_level", "light", "heavy", keys=keys,
        )
        shares: dict[str, float | None] = {}
        for traffic in style.TRAFFIC_ORDER:
            g = sel[sel["traffic_level"] == traffic]
            v = pd.to_numeric(g[series.pre_col], errors="coerce").dropna()
            shares[traffic] = _share(
                v, (v <= WITHIN_10PCT).to_numpy(), int(len(g))
            )
        # The binary within-10% flip count: the same effect in its most
        # legible form -- how many pairs pass under one level only.
        bin_df = sel.assign(
            _pass=(pd.to_numeric(sel[series.pre_col], errors="coerce")
                   <= WITHIN_10PCT).astype(float)
        )
        bin_res = paired_wilcoxon(
            bin_df, "_pass", "traffic_level", "light", "heavy", keys=keys,
        )
        rows.append({
            "series": series.key,
            "label": series.label,
            "algorithm": series.algorithm,
            "value_col": series.pre_col,
            "congestion_source": sorted(sel["congestion_source"].unique()),
            "baseline_graph": sorted(sel["baseline_graph"].unique()),
            "n_pairs": res["n_pairs"],
            "median_light": res["median_a"],
            "median_heavy": res["median_b"],
            "median_paired_ratio_heavy_over_light": res["median_paired_ratio"],
            "within_10pct_light": shares.get("light"),
            "within_10pct_heavy": shares.get("heavy"),
            "n_light_better": res["n_a_better"],
            "n_heavy_better": res["n_b_better"],
            "n_tied": res["n_tied"],
            "statistic": res["statistic"],
            "pvalue": res["pvalue"],
            # In the binary frame "better" means a higher pass indicator,
            # so the a/b-better counts invert: b_better == light-only pass.
            "n_pass_light_only": bin_res["n_b_better"],
            "n_pass_heavy_only": bin_res["n_a_better"],
            "n_failed_excluded": _n_failed(df, algorithm=series.algorithm),
        })
    return pd.DataFrame(rows)


def failure_table(df: pd.DataFrame) -> pd.DataFrame:
    """Every recorded failure, grouped by cause.

    Exists so that no table has to say "excluded" without the reader being
    able to see WHAT was excluded (CLAUDE.md: failures are recorded, never
    silently dropped).
    """
    bad = df[df["failed"].astype(bool)]
    cols = ["failure_reason", "scenario_id", "algorithm", "n"]
    if bad.empty:
        return pd.DataFrame(columns=cols)
    return (
        bad.groupby(["failure_reason", "scenario_id", "algorithm"])
        .size().rename("n").reset_index()
        .sort_values("n", ascending=False, ignore_index=True)
    )


# --- headline figures ------------------------------------------------------


def headline_numbers(df: pd.DataFrame) -> dict[str, Any]:
    """The handful of numbers a reader should leave with.

    Each is phrased the way CLAUDE.md requires: the warm-start result as a
    PAIRED median ratio plus the 1-iteration share (never a ratio of
    medians), Physarum's quality as pooled-vs-model-only (never pooled
    alone), and the congestion effect with its model named.
    """
    ok, _ = _successful(df)
    phys = ok[ok["algorithm"] == "physarum"]
    out: dict[str, Any] = {}

    out["warm_vs_cold"] = {
        algo: {
            metric: paired_wilcoxon(
                ok[ok["algorithm"] == algo], metric,
                "warm_start", True, False,
            )
            for metric in ("recovery_iterations", "recovery_wallclock_s")
        }
        for algo in ("physarum", "aco")
    }

    warm_iters = pd.to_numeric(
        phys.loc[phys["warm_start"].astype(bool), "recovery_iterations"],
        errors="coerce",
    ).dropna()
    cold_iters = pd.to_numeric(
        phys.loc[~phys["warm_start"].astype(bool), "recovery_iterations"],
        errors="coerce",
    ).dropna()
    out["physarum_bimodality"] = {
        "n_warm": int(warm_iters.size),
        "n_cold": int(cold_iters.size),
        "warm_share_exactly_1_iteration": float((warm_iters == 1).mean()),
        "cold_share_exactly_1_iteration": float((cold_iters == 1).mean()),
        "cold_min_iterations": float(cold_iters.min()),
        "warm_median_iterations": float(warm_iters.median()),
        "cold_median_iterations": float(cold_iters.median()),
        "note": (
            "A ratio of these medians is NOT a valid statistic: the warm "
            "distribution has modes at 1 and ~50. Use the paired median "
            "ratio in warm_vs_cold."
        ),
    }

    # Undisrupted quality, pooled vs model-only, per traffic level.
    initial = _dedup_initial(phys)
    pooled_vs_walk: dict[str, Any] = {}
    for traffic in style.TRAFFIC_ORDER:
        g = initial[initial["traffic_level"] == traffic]
        pooled = pd.to_numeric(g["stretch"], errors="coerce").dropna()
        walk = pd.to_numeric(g["walk_best_stretch"], errors="coerce").dropna()
        pooled_vs_walk[traffic] = {
            "n": int(len(g)),
            "pooled_exact_match_share": _share(
                pooled, (pooled <= 1.0 + EXACT_TOL).to_numpy(), int(len(g))
            ),
            "walk_only_exact_match_share": _share(
                walk, (walk <= 1.0 + EXACT_TOL).to_numpy(), int(len(g))
            ),
            "walk_produced_any_path_share": float(
                (pd.to_numeric(g["walk_success_count"], errors="coerce") > 0)
                .mean()
            ),
        }
    out["physarum_pooled_vs_model_only"] = pooled_vs_walk
    out["physarum_walk_any_path_share_all_rows"] = float(
        (pd.to_numeric(phys["walk_success_count"], errors="coerce") > 0).mean()
    )

    cong = congestion_effect_table(df)
    aco_row = cong[cong["series"] == "aco"]
    out["aco_congestion_effect_sumo"] = (
        _jsonable(aco_row.to_dict(orient="records")[0])
        if len(aco_row) else None
    )
    out["congestion_model_caveat"] = (
        "These are SUMO / sumo_speeds numbers. Phase 4/4d measured the "
        "OPPOSITE sign on BPR / config_speeds, so the direction of the "
        "congestion effect is a property of the congestion model. Never "
        "write 'under congestion' unqualified."
    )

    classical = ok[ok["algorithm"].isin(("dijkstra", "astar"))]
    out["classical_sanity"] = {
        str(algo): {
            "median_stretch": float(
                pd.to_numeric(g["stretch"], errors="coerce").median()
            ),
            "max_post_disruption_stretch": float(
                pd.to_numeric(
                    g["post_disruption_stretch"], errors="coerce"
                ).max()
            ),
            "median_recovery_wallclock_s": float(
                pd.to_numeric(
                    g["recovery_wallclock_s"], errors="coerce"
                ).median()
            ),
            "median_recovery_iterations": float(
                pd.to_numeric(
                    g["recovery_iterations"], errors="coerce"
                ).median()
            ),
        }
        for algo, g in classical.groupby("algorithm")
    }
    return out


# --- assembly --------------------------------------------------------------


def _plain(value: Any) -> Any:
    """Collapse numpy/pandas scalars to plain Python for JSON."""
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return None if not np.isfinite(f) else f
    if isinstance(value, np.ndarray):
        return [_plain(v) for v in value.tolist()]
    return value


def _jsonable(obj: Any) -> Any:
    """Recursively make `obj` JSON-serializable, NaN -> null."""
    if isinstance(obj, pd.DataFrame):
        return [_jsonable(r) for r in obj.to_dict(orient="records")]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    return _plain(obj)


def compute_all(
    df: pd.DataFrame, *, n_boot: int = N_BOOT, with_ci: bool = True,
) -> dict[str, Any]:
    """Every table and headline number above, JSON-serializable.

    Keys are stable and self-describing because the Phase 9 dashboard reads
    this bundle: renaming one silently breaks a panel that nothing in
    Python will type-check.
    """
    ok, n_failed = _successful(df)
    meta = {
        "n_rows": int(len(df)),
        "n_columns": int(df.shape[1]),
        "n_rows_successful": int(len(ok)),
        "n_failed": n_failed,
        "failed_share": n_failed / len(df) if len(df) else None,
        "n_od_pairs": int(df.groupby(list(CLUSTER_KEYS)).ngroups),
        "n_cells_designed": int(df.groupby(list(CELL_KEYS)).ngroups),
        "n_cells_with_data": int(ok.groupby(list(CELL_KEYS)).ngroups),
        "algorithm_counts": {
            str(k): int(v) for k, v in df["algorithm"].value_counts().items()
        },
        "config_hash": sorted(df["config_hash"].astype(str).unique()),
        "baseline_hash": sorted(df["baseline_hash"].astype(str).unique()),
        "baseline_graph": sorted(df["baseline_graph"].astype(str).unique()),
        "congestion_source": sorted(
            df["congestion_source"].astype(str).unique()
        ),
        "scenarios": sorted(df["scenario_id"].astype(str).unique()),
        "traffic_levels": sorted(df["traffic_level"].astype(str).unique()),
        "seeds_per_algorithm": {
            str(a): sorted(int(s) for s in g["seed"].unique())
            for a, g in df.groupby("algorithm")
        },
        "statistics": {
            "central_tendency": "median + IQR (never means: right-skewed)",
            "paired_test": "scipy.stats.wilcoxon, exact zeros dropped",
            "pairing_keys": list(PAIR_KEYS),
            "bootstrap": "two-stage clustered: OD pairs, then seeds within",
            "bootstrap_cluster_keys": list(CLUSTER_KEYS),
            "bootstrap_within_keys": list(WITHIN_KEYS),
            "n_boot": int(n_boot) if with_ci else 0,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "ci_level": 0.95,
            "physarum_pooling": (
                "NEVER pooled across path provenance: 'physarum' is the "
                "with-fallback series, 'physarum_walk' is the model's own "
                "max-flux extraction."
            ),
        },
    }
    return {
        "meta": meta,
        "headline": _jsonable(headline_numbers(df)),
        "quality": _jsonable(quality_table(df, n_boot=n_boot, with_ci=with_ci)),
        "recovery": _jsonable(
            recovery_table(df, n_boot=n_boot, with_ci=with_ci)
        ),
        "warm_vs_cold": _jsonable(warm_cold_table(df)),
        "provenance": _jsonable(provenance_table(df)),
        "scenario": _jsonable(scenario_table(df)),
        "congestion_effect": _jsonable(congestion_effect_table(df)),
        "failures": _jsonable(failure_table(df)),
    }
