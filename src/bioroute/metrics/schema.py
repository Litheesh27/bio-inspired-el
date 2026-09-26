"""Flat result record schema, persisted to Parquet.

IMPLEMENTED IN: Phase 6.
Will contain: the one-record-per-run schema with columns including
algorithm, od_pair, scenario, traffic_level, warm/cold, seed, config_hash,
wall_clock_timestamp, stretch, iters_to_converge, wallclock_to_converge,
time_to_first_valid_path, recovery_iterations, recovery_wallclock,
post_disruption_stretch, peak_memory_kb, and failed (bool, never silently
dropped). Required columns must never be null.
"""
