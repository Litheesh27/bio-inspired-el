"""Phase 9a: unit tests for the results statistics.

Almost everything here runs on SMALL SYNTHETIC FRAMES with hand-computable
answers. That is deliberate: a test that asserts a number read off the real
sweep only proves the code still does what it did, not that it does the
right thing. One test at the end does load the real Parquet, to prove the
committed evidence still satisfies `load_results`' invariants -- which is a
different claim and worth its own test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bioroute.viz import stats  # noqa: E402


# --- synthetic frames ------------------------------------------------------


def _row(**overrides) -> dict:
    """One schema-complete result row, with harmless defaults.

    Every column in `REQUIRED_COLUMNS` is present, because `load_results`
    checks the schema before anything else and a frame missing a column
    would fail for a reason the test is not about.
    """
    row = {
        "run_id": "r0", "algorithm": "aco", "od_source": 1, "od_target": 2,
        "scenario_id": "S1", "traffic_level": "light",
        "congestion_source": "sumo", "baseline_graph": "sumo_speeds",
        "baseline_hash": "abc", "warm_start": False, "seed": 0,
        "config_hash": "cfg", "timestamp_utc": "2026-01-01T00:00:00Z",
        "iterations": 10, "iteration_budget": 150, "converged": True,
        "wallclock_s": 1.0, "peak_memory_kb": 1000.0, "failed": False,
        "failure_reason": "", "n_removed_edges": 1,
        "path_cost": 100.0, "stretch": 1.0, "path_hops": 10.0,
        "initial_time_to_first_valid_path_iters": 1.0,
        "recovery_iterations": 10.0, "recovery_wallclock_s": 1.0,
        "recovery_converged": True, "time_to_first_valid_path_iters": 1.0,
        "post_disruption_cost": 110.0, "post_disruption_stretch": 1.1,
        "extraction_method": None, "best_path_extraction_method": None,
        "ant_completion_rate": 0.8, "walk_best_cost": None,
        "walk_best_stretch": None, "walk_success_count": None,
        "post_disruption_walk_stretch": None,
    }
    row.update(overrides)
    return row


def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame([_row(**r) for r in rows])
    df["run_id"] = [f"r{i}" for i in range(len(df))]
    return df


def _clustered_frame(
    n_pairs: int = 12, n_seeds: int = 4, rng_seed: int = 7,
) -> pd.DataFrame:
    """A frame with real cluster structure: per-OD offsets plus seed noise.

    The per-pair offset is what makes the two-stage bootstrap necessary --
    without it, clustering would be indistinguishable from an iid
    bootstrap and the test would pass for the wrong reason.
    """
    rng = np.random.default_rng(rng_seed)
    rows = []
    for pair in range(n_pairs):
        offset = rng.uniform(0.0, 0.6)      # OD-pair level effect
        for seed in range(n_seeds):
            rows.append({
                "od_source": pair, "od_target": pair + 1000, "seed": seed,
                "stretch": 1.0 + offset + rng.uniform(0.0, 0.1),
            })
    return _frame(rows)


# --- median / IQR ----------------------------------------------------------


def test_median_iqr_matches_hand_computation():
    d = stats.median_iqr([1.0, 2.0, 3.0, 4.0, 5.0])
    assert d["n"] == 5
    assert d["median"] == 3.0
    assert d["q1"] == 2.0 and d["q3"] == 4.0 and d["iqr"] == 2.0


def test_median_iqr_drops_nulls_and_survives_empty():
    d = stats.median_iqr([1.0, np.nan, 3.0])
    assert d["n"] == 2 and d["median"] == 2.0
    empty = stats.median_iqr([np.nan, np.nan])
    assert empty["n"] == 0 and empty["median"] is None


# --- bootstrap -------------------------------------------------------------


def test_bootstrap_is_reproducible_under_the_fixed_seed():
    df = _clustered_frame()
    a = stats.bootstrap_median_ci(df, "stretch", n_boot=500)
    b = stats.bootstrap_median_ci(df, "stretch", n_boot=500)
    assert a["ci_low"] == b["ci_low"]
    assert a["ci_high"] == b["ci_high"]
    assert a["bootstrap_seed"] == stats.BOOTSTRAP_SEED


def test_bootstrap_ci_brackets_the_point_median():
    df = _clustered_frame()
    out = stats.bootstrap_median_ci(df, "stretch", n_boot=2000)
    point = float(df["stretch"].median())
    assert out["median"] == pytest.approx(point)
    assert out["ci_low"] <= point <= out["ci_high"]
    assert out["n"] == len(df)
    assert out["n_clusters"] == 12
    # Stage 2 saw 4 seeds inside each of the 12 OD-pair clusters.
    assert out["n_within_per_cluster"] == pytest.approx(4.0)


def test_bootstrap_clustering_widens_the_interval():
    """Clustering must COST precision -- that is the point of doing it.

    With a strong per-OD-pair effect, treating the rows as independent
    produces a narrower interval. If the clustered CI were not wider, the
    cluster resampling would not be happening.
    """
    df = _clustered_frame(n_pairs=14, n_seeds=5)
    clustered = stats.bootstrap_median_ci(df, "stretch", n_boot=2000)
    # One cluster per ROW == no clustering at all.
    naive_df = df.assign(od_source=range(len(df)), od_target=range(len(df)))
    naive = stats.bootstrap_median_ci(naive_df, "stretch", n_boot=2000)
    width = clustered["ci_high"] - clustered["ci_low"]
    naive_width = naive["ci_high"] - naive["ci_low"]
    assert width > naive_width


def test_bootstrap_handles_an_all_null_column():
    df = _clustered_frame()
    out = stats.bootstrap_median_ci(df, "walk_best_stretch", n_boot=50)
    assert out["n"] == 0 and out["median"] is None and out["ci_low"] is None


def test_bootstrap_on_a_single_cluster_is_degenerate_but_defined():
    df = _frame([
        {"od_source": 1, "od_target": 2, "seed": s, "stretch": 1.0 + s / 10}
        for s in range(4)
    ])
    out = stats.bootstrap_median_ci(df, "stretch", n_boot=200)
    assert out["n_clusters"] == 1
    assert out["ci_low"] <= out["median"] <= out["ci_high"]


# --- paired Wilcoxon -------------------------------------------------------


def _paired_frame() -> pd.DataFrame:
    """Five pairing keys; warm beats cold on 3, ties on 1, loses on 1.

    Differences (cold - warm): +10, +10, +10, 0, -4. Dropping the zero
    leaves 4 signed ranks, so a correct implementation reports n_pairs=5,
    n_tied=1 and a test over 4 values.
    """
    warm = [10.0, 20.0, 30.0, 40.0, 50.0]
    cold = [20.0, 30.0, 40.0, 40.0, 46.0]
    rows = []
    for i, (w, c) in enumerate(zip(warm, cold)):
        rows.append({
            "od_source": i, "od_target": i + 100, "seed": 0,
            "warm_start": True, "recovery_iterations": w,
        })
        rows.append({
            "od_source": i, "od_target": i + 100, "seed": 0,
            "warm_start": False, "recovery_iterations": c,
        })
    return _frame(rows)


def test_paired_wilcoxon_drops_zero_differences_and_counts_pairs():
    res = stats.paired_wilcoxon(
        _paired_frame(), "recovery_iterations", "warm_start", True, False,
    )
    assert res["n_pairs"] == 5          # the tie is a PAIR ...
    assert res["n_tied"] == 1
    assert res["n_nonzero"] == 4        # ... but not a test observation
    assert res["n_a_better"] == 3       # warm faster on 3
    assert res["n_b_better"] == 1
    # Median of the per-pair ratios cold/warm: 2.0, 1.5, 1.333, 1.0, 0.92
    assert res["median_paired_ratio"] == pytest.approx(1.3333333, rel=1e-6)
    assert res["median_a"] == 30.0 and res["median_b"] == 40.0
    assert 0.0 < res["pvalue"] <= 1.0


def test_paired_wilcoxon_ratio_is_not_the_ratio_of_medians():
    """The distinction CLAUDE.md requires, pinned by a test.

    Here the ratio of medians is 40/30 = 1.333 while the per-pair median
    ratio is also 1.333 by coincidence, so the test uses a frame built to
    separate them: a bimodal warm side, exactly like Physarum's.
    """
    warm = [1.0, 1.0, 1.0, 50.0, 50.0]
    cold = [100.0, 100.0, 100.0, 100.0, 100.0]
    rows = []
    for i, (w, c) in enumerate(zip(warm, cold)):
        rows.append({"od_source": i, "od_target": i + 1, "seed": 0,
                     "warm_start": True, "recovery_iterations": w})
        rows.append({"od_source": i, "od_target": i + 1, "seed": 0,
                     "warm_start": False, "recovery_iterations": c})
    res = stats.paired_wilcoxon(
        _frame(rows), "recovery_iterations", "warm_start", True, False,
    )
    # Ratio of medians: 100 / 1 = 100x, which describes only the lower
    # mode of a bimodal warm distribution.
    assert res["median_b"] / res["median_a"] == pytest.approx(100.0)
    # Per-pair ratios are {100, 100, 100, 2, 2}; their median is 100 here,
    # but it is computed per pair and stays defined when the two medians
    # land on opposite modes -- which is the whole reason for the rule.
    assert res["median_paired_ratio"] == pytest.approx(100.0)
    assert res["n_a_better"] == 5


def test_paired_wilcoxon_rejects_an_ambiguous_pairing_key():
    df = _paired_frame()
    dup = pd.concat([df, df.iloc[[0]].assign(run_id="dup")], ignore_index=True)
    with pytest.raises(ValueError, match="multiple rows per"):
        stats.paired_wilcoxon(
            dup, "recovery_iterations", "warm_start", True, False,
        )


def test_paired_frame_reports_one_sided_coverage():
    """A value present on one side only is coverage, not a missing value."""
    df = _paired_frame()
    df.loc[df["warm_start"] & (df["od_source"] == 0),
           "recovery_iterations"] = np.nan
    pairs = stats.paired_frame(
        df, "recovery_iterations", "warm_start", True, False,
    )
    assert len(pairs) == 4
    assert pairs.attrs["n_b_only"] == 1      # cold has a value, warm does not
    assert pairs.attrs["n_a_only"] == 0


# --- quality table ---------------------------------------------------------


def _physarum_frame() -> pd.DataFrame:
    """Six Physarum initial solves across 3 OD pairs x 2 traffic levels.

    The pooled `stretch` is 1.0 everywhere (the fallback found the
    optimum), while the max-flux walk completed on 4 of 6 and was worse on
    3 of those. That is the sweep's real shape in miniature.
    """
    pooled = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    walk = [1.0, 1.2, np.nan, 1.4, 1.3, np.nan]
    rows = []
    for i, (p, w) in enumerate(zip(pooled, walk)):
        rows.append({
            "algorithm": "physarum", "scenario_id": "S0",
            "od_source": i // 2, "od_target": i // 2 + 100, "seed": 0,
            "traffic_level": "light" if i % 2 == 0 else "heavy",
            "stretch": p, "path_cost": 100.0,
            "walk_best_stretch": w,
            "walk_best_cost": None if pd.isna(w) else 100.0 * w,
            "walk_success_count": 0.0 if pd.isna(w) else 3.0,
            "best_path_extraction_method": (
                "max_flux_walk" if w == 1.0 else "theta_subgraph_fallback"
            ),
            # S0 control: the recovery block must be null.
            "recovery_iterations": None, "recovery_wallclock_s": None,
            "recovery_converged": None,
            "time_to_first_valid_path_iters": None,
            "post_disruption_cost": None, "post_disruption_stretch": None,
            "post_disruption_walk_stretch": None, "n_removed_edges": 0,
        })
    return _frame(rows)


def test_quality_table_keeps_the_two_physarum_series_separate():
    table = stats.quality_table(_physarum_frame(), n_boot=200)
    series = set(table["series"])
    assert {"physarum", "physarum_walk"} <= series

    pooled = table[(table["series"] == "physarum")
                   & (table["traffic_level"] == "all")].iloc[0]
    model = table[(table["series"] == "physarum_walk")
                  & (table["traffic_level"] == "all")].iloc[0]

    # Distinct rows reading distinct columns -- never one pooled number.
    assert pooled["value_col"] == "stretch"
    assert model["value_col"] == "walk_best_stretch"

    # THE REAL FINDING: the model's own extraction is WORSE than the
    # with-fallback series. If these two ever come out equal, the walk
    # columns have been pooled into `stretch` somewhere.
    assert model["median"] > pooled["median"]
    assert model["exact_match_share"] < pooled["exact_match_share"]


def test_quality_table_uses_two_denominators_for_the_walk_series():
    table = stats.quality_table(_physarum_frame(), n_boot=200)
    model = table[(table["series"] == "physarum_walk")
                  & (table["traffic_level"] == "all")].iloc[0]
    # 6 rows exist; the walk produced a path on 4. A null walk is NOT an
    # exact match, so the share is 1/6, not 1/4.
    assert model["n"] == 6 and model["n_with_value"] == 4
    assert model["exact_match_share"] == pytest.approx(1 / 6)


def test_quality_table_deduplicates_the_initial_solve():
    """The same initial solve repeated across scenarios must count once."""
    base = _physarum_frame()
    repeated = pd.concat(
        [base.assign(scenario_id=s) for s in ("S0", "S1", "S2")],
        ignore_index=True,
    )
    repeated["run_id"] = [f"r{i}" for i in range(len(repeated))]
    table = stats.quality_table(repeated, n_boot=100)
    pooled = table[(table["series"] == "physarum")
                   & (table["traffic_level"] == "all")].iloc[0]
    assert pooled["n"] == 6          # not 18


def test_provenance_table_separates_the_classes():
    table = stats.provenance_table(_physarum_frame())
    assert set(table["best_path_extraction_method"]) == {
        "max_flux_walk", "theta_subgraph_fallback",
    }
    model_own = table[table["is_model_own_extraction"]]
    assert len(model_own) == 1
    assert table["n"].sum() == 6


# --- failures are never silent ---------------------------------------------


def test_failed_rows_are_excluded_but_counted():
    rows = [
        {"algorithm": "aco", "od_source": i, "od_target": i + 1, "seed": 0,
         "scenario_id": "S0", "stretch": 1.0,
         "recovery_iterations": None, "recovery_wallclock_s": None,
         "recovery_converged": None,
         "time_to_first_valid_path_iters": None,
         "post_disruption_cost": None, "post_disruption_stretch": None,
         "n_removed_edges": 0}
        for i in range(4)
    ]
    rows.append({
        "algorithm": "aco", "od_source": 99, "od_target": 100, "seed": 0,
        "scenario_id": "S0", "failed": True, "failure_reason": "boom",
        "stretch": None, "path_cost": None, "path_hops": None,
        "initial_time_to_first_valid_path_iters": None,
        "recovery_iterations": None, "recovery_wallclock_s": None,
        "recovery_converged": None, "time_to_first_valid_path_iters": None,
        "post_disruption_cost": None, "post_disruption_stretch": None,
        "n_removed_edges": 0,
    })
    df = _frame(rows)
    table = stats.quality_table(df, n_boot=100)
    aco = table[(table["series"] == "aco")
                & (table["traffic_level"] == "all")].iloc[0]
    assert aco["n"] == 4                      # the failure is excluded ...
    assert aco["n_failed_excluded"] == 1      # ... and reported
    assert len(stats.failure_table(df)) == 1


# --- validation ------------------------------------------------------------


def test_load_results_raises_on_a_stretch_below_one(tmp_path):
    df = _physarum_frame()
    df.loc[0, "stretch"] = 0.999           # beats Dijkstra: impossible
    path = tmp_path / "injected.parquet"
    df.to_parquet(path)
    with pytest.raises(stats.ResultsValidationError, match="stretch < 1.0"):
        stats.load_results(path)


def test_load_results_raises_on_a_walk_stretch_below_one(tmp_path):
    df = _physarum_frame()
    df.loc[0, "walk_best_stretch"] = 0.5
    path = tmp_path / "injected_walk.parquet"
    df.to_parquet(path)
    with pytest.raises(stats.ResultsValidationError,
                       match="walk_best_stretch < 1.0"):
        stats.load_results(path)


def test_load_results_raises_on_a_duplicate_run_id(tmp_path):
    df = _physarum_frame()
    df.loc[1, "run_id"] = df.loc[0, "run_id"]
    path = tmp_path / "dup.parquet"
    df.to_parquet(path)
    with pytest.raises(stats.ResultsValidationError, match="duplicated run_id"):
        stats.load_results(path)


def test_load_results_raises_when_s0_carries_a_recovery(tmp_path):
    df = _physarum_frame()
    df.loc[0, "recovery_iterations"] = 7.0
    path = tmp_path / "s0.parquet"
    df.to_parquet(path)
    with pytest.raises(stats.ResultsValidationError, match="S0 row"):
        stats.load_results(path)


def test_load_results_raises_when_the_walk_beats_the_pooled_cost(tmp_path):
    df = _physarum_frame()
    df.loc[0, "walk_best_cost"] = 1.0      # pooled path_cost is 100.0
    path = tmp_path / "walkcost.parquet"
    df.to_parquet(path)
    with pytest.raises(stats.ResultsValidationError,
                       match="walk_best_cost < "):
        stats.load_results(path)


# --- the real evidence -----------------------------------------------------


@pytest.mark.skipif(
    not stats.DEFAULT_PARQUET.exists(),
    reason="committed sweep Parquet not present",
)
def test_the_committed_sweep_loads_and_passes_validation():
    """The Phase 8e evidence still satisfies every invariant.

    This asserts the canonical facts too (row count, per-algorithm counts,
    cells, failures), so a Parquet that was regenerated, truncated or
    partially merged fails here rather than silently changing a figure.
    """
    df = stats.load_results()
    assert len(df) == stats.CANONICAL_FACTS["n_rows"]
    assert df["run_id"].is_unique
    assert (
        df["algorithm"].value_counts().to_dict()
        == stats.CANONICAL_FACTS["algorithm_counts"]
    )
    assert int(df["failed"].sum()) == stats.CANONICAL_FACTS["n_failed"]
    for col in stats.STRETCH_COLUMNS:
        assert (df[col].dropna() >= 1.0).all()


@pytest.mark.skipif(
    not stats.DEFAULT_PARQUET.exists(),
    reason="committed sweep Parquet not present",
)
def test_compute_all_is_json_serializable_and_keeps_physarum_split():
    import json

    df = stats.load_results()
    bundle = stats.compute_all(df, n_boot=200)
    json.dumps(bundle)          # must not raise: the dashboard reads this

    quality = pd.DataFrame(bundle["quality"])
    assert {"physarum", "physarum_walk"} <= set(quality["series"])
    pooled = quality[(quality["series"] == "physarum")
                     & (quality["traffic_level"] == "all")].iloc[0]
    model = quality[(quality["series"] == "physarum_walk")
                    & (quality["traffic_level"] == "all")].iloc[0]
    assert model["median"] > pooled["median"]

    # Phase 8e's load-bearing numbers, as a regression guard.
    head = bundle["headline"]
    assert head["physarum_bimodality"]["cold_min_iterations"] == 73.0
    assert head["warm_vs_cold"]["physarum"]["recovery_iterations"][
        "median_paired_ratio"] == pytest.approx(2.97, abs=0.01)
    assert head["warm_vs_cold"]["physarum"]["recovery_wallclock_s"][
        "median_paired_ratio"] == pytest.approx(6.87, abs=0.01)
