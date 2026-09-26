"""Router ABC: the common contract every routing algorithm implements.

IMPLEMENTED IN: Phase 3. This contract is load-bearing for Phase 4 (ACO),
Phase 5 (Physarum) and Phase 8 (the experiment runner) -- all three build
on it directly, so its shape is deliberate rather than incidental:

* It must serve a one-shot algorithm (Dijkstra: computes once, done) and an
  iterative one (ACO/Physarum: repeated `step()` calls toward convergence)
  through the *same* interface, so `run_to_convergence` below is the one
  measurement driver used for every algorithm in every phase.
* Warm-start vs. cold-start is explicit, not incidental: `apply_disruption`
  takes a `warm: bool` that the caller must decide, and `state()`/
  `load_state()` make an algorithm's internal adaptation state (pheromone
  matrix, conductivity vector, ...) a serializable, inspectable object
  rather than private mutable fields. This is the core methodological axis
  of the whole project (see CLAUDE.md "Recovery protocol").
* `RouterResult.trace` and the iteration at which the first valid path
  appeared are recorded by `run_to_convergence` itself, not left to each
  algorithm to remember to log -- Phase 6 needs `time_to_first_valid_path`
  measured identically across algorithms.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from bioroute.congestion.snapshot import TrafficSnapshot


@dataclass
class RouterResult:
    """The result of running a Router to convergence (or to budget
    exhaustion). Produced by `run_to_convergence`, not by individual
    algorithms directly, so every algorithm's result has identical shape.

    Attributes:
        path: best path found (node list), or None if no valid path was
            ever found within budget.
        cost: cost of `path` under the snapshot in force (see
            TrafficSnapshot.cost_of_path for the path-cost convention), or
            None if `path` is None.
        iterations: number of `step()` calls actually made. For a one-shot
            router this is exactly 1 -- never inflated to look iterative.
        converged: whether the router's own `converged()` criterion fired
            before the iteration budget was exhausted.
        trace: best cost found so far, recorded once per iteration (index i
            = best cost after the (i+1)-th step). Used for Phase 9's
            convergence figures. Entries are `math.inf` for iterations
            before any valid path was found.
        time_to_first_valid_path_iters: iteration index (1-based) at which
            a valid path was first found, or None if never. Measured
            separately from `iterations`/`converged` because a method can
            find *a* valid path quickly and a *good* one slowly (see
            CLAUDE.md's honest-reporting note).
        wallclock_s: total wall-clock time spent in `step()` calls.
        extras: algorithm-specific diagnostics (e.g. branching factor,
            path-extraction method used, dead-end count) that don't belong
            in the shared schema but are useful for debugging/reporting.
    """

    path: list[int] | None
    cost: float | None
    iterations: int
    converged: bool
    trace: list[float] = field(default_factory=list)
    time_to_first_valid_path_iters: int | None = None
    wallclock_s: float = 0.0
    extras: dict[str, Any] = field(default_factory=dict)


class Router(ABC):
    """Common contract for Dijkstra/A* (Phase 3), ACO (Phase 4), and
    Physarum (Phase 5). A single instance is used for one (source, target,
    snapshot) routing problem; a fresh instance is created per OD pair /
    scenario / seed."""

    #: Human-readable algorithm name, used in metrics rows and figures.
    name: str = "router"

    @abstractmethod
    def prepare(
        self,
        G: nx.MultiDiGraph,
        source: int,
        target: int,
        snapshot: TrafficSnapshot,
        seed: int,
    ) -> None:
        """Initialize the router for a new routing problem. Must be called
        exactly once before any `step()` call. Resets all internal state
        (pheromone, conductivity, tabu sets, ...) -- this is the "cold
        start" entry point; warm-start continuation across a disruption
        goes through `apply_disruption(warm=True)` instead, never through a
        second `prepare()` call."""
        raise NotImplementedError

    @abstractmethod
    def step(self) -> float | None:
        """Perform one iteration of the algorithm and return the current
        best cost found so far (across all iterations up to and including
        this one), or None if no valid path has been found yet.

        For a one-shot router (Dijkstra, A*): the first call computes the
        full solution and returns its cost (or None if unreachable); every
        subsequent call is a no-op that returns the same value again
        without doing extra work or advancing `iterations`.
        """
        raise NotImplementedError

    @abstractmethod
    def best_path(self) -> list[int] | None:
        """Best path found so far, or None if none found yet."""
        raise NotImplementedError

    @abstractmethod
    def best_cost(self) -> float | None:
        """Cost of `best_path()`, or None if none found yet."""
        raise NotImplementedError

    @abstractmethod
    def converged(self) -> bool:
        """Whether this router's own convergence criterion has fired. A
        one-shot router is converged as soon as it has produced its single
        result (iteration 1)."""
        raise NotImplementedError

    @abstractmethod
    def apply_disruption(self, removed_edges: list[tuple[int, int, int]], warm: bool) -> None:
        """Apply a disruption (edge removals) to this router's view of the
        graph.

        warm=True: retain all internal adaptation state (pheromone /
            conductivity / etc.) and only invalidate the severed edges --
            this is the warm-start recovery protocol that is the project's
            primary experiment.
        warm=False: reinitialize internal state completely (equivalent to
            calling `prepare()` again on the disrupted graph) -- the
            cold-start control.

        Either way, subsequent `step()` calls resume iterating (for
        one-shot routers, the next `step()` recomputes on the disrupted
        graph) and `converged()`/best_path()/best_cost() must reflect the
        disrupted graph only.
        """
        raise NotImplementedError

    @abstractmethod
    def state(self) -> dict[str, Any]:
        """A serializable snapshot of this router's internal adaptation
        state (empty dict for stateless routers like Dijkstra/A*)."""
        raise NotImplementedError

    @abstractmethod
    def load_state(self, state: dict[str, Any]) -> None:
        """Restore internal adaptation state previously produced by
        `state()`. Used to resume a warm-started router across process
        boundaries (e.g. the Phase 8 experiment runner)."""
        raise NotImplementedError


def run_to_convergence(router: Router, budget: int) -> RouterResult:
    """Drive `router.step()` up to `budget` iterations (or until
    `router.converged()` fires, whichever comes first), recording the
    identical measurements for every algorithm: per-iteration best-cost
    trace, the iteration of first valid path, iteration count, and
    wall-clock time.

    `router.prepare(...)` must already have been called (and, for a
    disruption-recovery measurement, `router.apply_disruption(...)`) before
    this is invoked -- `run_to_convergence` only drives `step()`, it does
    not itself call `prepare`.
    """
    trace: list[float] = []
    first_valid_iter: int | None = None
    start = time.perf_counter()
    iterations = 0

    for i in range(1, budget + 1):
        cost = router.step()
        iterations = i
        if cost is None:
            trace.append(float("inf"))
        else:
            trace.append(cost)
            if first_valid_iter is None:
                first_valid_iter = i
        if router.converged():
            break

    wallclock_s = time.perf_counter() - start

    return RouterResult(
        path=router.best_path(),
        cost=router.best_cost(),
        iterations=iterations,
        converged=router.converged(),
        trace=trace,
        time_to_first_valid_path_iters=first_valid_iter,
        wallclock_s=wallclock_s,
        extras={},
    )
