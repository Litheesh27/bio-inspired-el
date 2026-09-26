"""Resumable, parallel experiment runner.

IMPLEMENTED IN: Phase 8, driven by scripts/03_run_experiments.py.
Will contain: joblib.Parallel execution over OD pairs/run specs from
matrix.py, per-run try/except so one failure never kills the sweep,
incremental Parquet append of metrics.schema records, and resume-from-
completed logic keyed by each run's hash (so a killed/restarted sweep
does not redo finished runs). Supports a --smoke mode (5 OD pairs) and a
full-sweep mode. Exit criterion: full sweep completes with a failed rate
under 5%, and the results Parquet loads cleanly into pandas. A fixed seed
must reproduce identical results across two runs.
"""
