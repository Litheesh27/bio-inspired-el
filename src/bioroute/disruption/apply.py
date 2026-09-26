"""Applying a disruption scenario's edge removal to a graph/algorithm state.

IMPLEMENTED IN: Phase 7.
Will contain: the mechanics of actually removing/zeroing selected edges on
a live graph or Router instance for a given scenario, shared across
classical/ACO/Physarum so the same removal semantics apply to all three,
and verification that every post-disruption path contains zero severed
edges.
"""
