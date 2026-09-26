"""Disruption scenario definitions, selected relative to the optimal path.

IMPLEMENTED IN: Phase 7.
Will contain: generators for scenarios S0-S6 — S0 no disruption (control),
S1 single random edge on the optimal path, S2 highest-betweenness edge on
the path, S3/S4 3 or 5 simultaneous path edges, S5 corridor closure (every
edge of one named OSM way), S6 intersection closure (a node and all
incident edges). Each generator enforces the hard constraint that source
and target remain connected after removal, resampling on failure and
logging the rejection count (a high rejection rate is itself a network-
fragility finding). Scenarios are reproducible under a fixed seed.
"""
