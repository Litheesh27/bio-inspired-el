"""Physarum polycephalum tube-reinforcement router.

IMPLEMENTED IN: Phase 5.
Will contain: `PhysarumRouter(Router)` implementing the Tero et al.
adaptive-conductivity model — per-edge conductivity D_ij, cost L_ij
(current congested travel time), conductance w_ij = D_ij / L_ij; each
iteration solves the weighted-Laplacian system L p = b (+I0 at source,
-I0 at sink, ground p_sink = 0) via scipy.sparse.linalg.spsolve on CSR;
flux Q_ij = w_ij * (p_i - p_j); adaptation
D_ij <- D_ij + dt * (f(|Q_ij|) - D_ij) with f(Q) = |Q|^mu / (1 + |Q|^mu),
mu ~= 1.8, plus a simpler D <- |Q| (Bonifaci) variant as a config switch.
Path extraction is a greedy maximum-flux walk from source with a visited
set, falling back to shortest path on the D > theta thresholded subgraph
if it stalls (the method that fired must be recorded, since silently
changing extraction method between runs would corrupt the stretch metric).
Warm-start disruption: zero D on severed edges, drop them from the
Laplacian, retain all other conductivities, keep iterating. One model
instance per OD pair (no multi-commodity flow), parallelized across OD
pairs with joblib.
Exit criterion: on an uncongested graph, Physarum converges to the exact
Dijkstra path (stretch = 1.0) on >=80% of OD pairs.
"""
