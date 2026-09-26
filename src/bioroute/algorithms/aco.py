"""Ant Colony Optimization router (MAX-MIN Ant System).

IMPLEMENTED IN: Phase 4.
Will contain: `ACORouter(Router)` implementing MAX-MIN Ant System (tau_min/
tau_max bounds, required at this graph scale since vanilla AS stagnates),
transition rule p ~ tau^alpha * eta^beta with eta = 1/travel_time
(alpha=1, beta swept over {2,3,5}, rho=0.1, m=30 ants), per-ant tabu sets,
max_steps = 3 * graph diameter with dead-ended/over-budget ants discarded
(not repaired), iteration-best pheromone deposit Delta_tau = Q / L_best,
a convergence check (iteration-best path unchanged for k=20 iterations AND
pheromone branching factor below threshold), and two configurable
disruption-response strategies: (a) zero severed edges only, (b) local
pheromone reset in an n-hop neighbourhood of the break.
Exit criterion: on >=90% of a 30-OD-pair sample, ACO reaches
stretch <= 1.10 within the iteration budget; the beta/rho tuning sweep
that achieves this is recorded, not discarded.
"""
