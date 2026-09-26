"""Router ABC: the common contract every routing algorithm implements.

IMPLEMENTED IN: Phase 3.
Will contain: `Router` abstract base class with
`solve(graph, source, target, weights) -> Result`,
`apply_disruption(removed_edges)`, `step()` for iterative methods (ACO,
Physarum), and a serializable `state` property so warm-start (retaining
pheromone/conductivity across a disruption) is explicit rather than
incidental. Also defines the `Result` record shape (path, cost, iterations,
failed flag, etc.) that classical.py, aco.py, and physarum.py all return.
"""
