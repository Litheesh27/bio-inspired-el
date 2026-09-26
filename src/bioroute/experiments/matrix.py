"""The full experiment matrix definition.

IMPLEMENTED IN: Phase 8.
Will contain: construction of the run matrix — 30 OD pairs (stratified
10/10/10 by short/medium/long straight-line distance, fixed seed) x 7
scenarios (S0-S6) x 2 traffic levels x 3 algorithms x {warm, cold} x 5
seeds (ACO stochastic; 3 inits for Physarum) — as an iterable of run
specs, each with a stable run hash for resume-from-completed support.
"""
