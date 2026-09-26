"""Classical shortest-path routers: Dijkstra and A*.

IMPLEMENTED IN: Phase 3.
Will contain: `DijkstraRouter(Router)` using networkx.shortest_path with
the congested edge weight, and `AStarRouter(Router)` with a great-circle /
max-speed admissible heuristic. Dijkstra recompute is also the baseline
recovery mechanism after a disruption (it has no iterative "step" mode).
Exit criterion for this module: A* cost equals Dijkstra cost on every OD
pair tested (100 random pairs on the real network) — a mismatch means the
heuristic is inadmissible and must be fixed before Phase 4/5 proceed.
"""
