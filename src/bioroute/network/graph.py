"""Graph construction, normalization, and disk caching.

IMPLEMENTED IN: Phase 1.
Will contain: extraction of the largest strongly connected component from
the raw OSMnx MultiDiGraph, ox.add_edge_speeds / ox.add_edge_travel_times
to derive free-flow travel_time per edge, normalization of the osmid edge
attribute to always be list[int] (OSMnx yields a bare int for unmerged
edges and a list for simplified ones), persistence as GraphML keyed by a
config hash (see config.py), transparent reload-if-cached behavior, and a
human-readable network report (node/edge counts, total km, degree
distribution, mean edge length) printed after every build.
"""
