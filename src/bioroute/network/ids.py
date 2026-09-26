"""OSM-way-ID <-> SUMO-edge-ID <-> graph-edge (u,v,k) matching.

IMPLEMENTED IN: Phase 2c. This is flagged in the project plan as the single
highest-risk piece of the whole project.
Will contain: regex stripping of SUMO edge ID sign/split suffixes (e.g.
"123456", "-123456", "123456#2") to recover the underlying OSM way ID;
construction of way_id -> [sumo_edge_ids] and way_id -> [(u,v,k)] maps
(the latter using the normalized osmid lists from network/graph.py),
inverted into (u,v,k) -> [sumo_edge_ids]; direction-aware, length-weighted
averaging of matched SUMO edge travel times per graph edge; a fallback path
for edges that fail ID matching (falls back to free-flow travel_time, and
is counted for the coverage report); and, if ID-based coverage is below
85%, geometric matching (edge-midpoint nearest-neighbour + bearing
agreement within 30 degrees) as a secondary pass.
"""
