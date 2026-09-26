"""TrafficSnapshot construction helpers shared by bpr.py and sumo.py.

IMPLEMENTED IN: Phase 2.
Will contain: shared helpers for building/validating TrafficSnapshot
objects (e.g. ensuring every graph edge has a congested travel time,
falling back to free-flow travel_time where unmatched, and computing the
edge-match coverage report used as Phase 2's exit criterion, expected
>=85%).
"""
