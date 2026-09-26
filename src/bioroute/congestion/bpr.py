"""Analytic BPR (Bureau of Public Roads) volume-delay congestion fallback.

IMPLEMENTED IN: Phase 2a. Built before the SUMO pipeline so it unblocks
Phases 3-9 immediately; remains the fallback CongestionProvider whenever
SUMO edge-match coverage is insufficient or SUMO is unavailable.
Will contain: a `BPRCongestionProvider(CongestionProvider)` using the
standard BPR volume-delay function, synthetic OD demand, and incremental
all-or-nothing traffic assignment to produce a TrafficSnapshot per
(level, time_bin). Expected to be roughly 100 lines.
"""
