"""Metric computation from Router results.

IMPLEMENTED IN: Phase 6.
Will contain: functions computing each metric in schema.py from raw Router
Result objects and run traces — in particular `stretch = cost_alg /
cost_dijkstra` computed against Dijkstra on the SAME weighted graph
(stretch < 1.0 must raise/assert, it indicates a bug), and
post_disruption_stretch computed against Dijkstra recomputed on the
disrupted graph. peak_memory_kb uses tracemalloc. Exit criterion: a
3-algorithm x 5-OD-pair smoke run writes a well-formed Parquet file with
no nulls in required columns.
"""
