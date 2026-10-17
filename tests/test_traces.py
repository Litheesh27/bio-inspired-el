"""Phase 9b tests: the trace/path regeneration driver, on a toy graph.

Nothing here touches the real Bengaluru network, any SUMO artifact or the
results Parquet. A unit test that needed the 2,856-node graph and a
pre-warmed SUMO snapshot would take tens of seconds and would fail on a
checkout where those cached artifacts are absent -- which is most of them,
since `data/sumo/` is gitignored. The real-data agreement is checked
instead by `scripts/06_make_traces.py`'s cross-check against the Parquet,
which is where it belongs: that is an integration claim, not a unit one.

What IS covered here:

* the convergence trace's three structural invariants (monotone
  non-increasing in best cost, no longer than the budget, final value
  equals `best_cost()`);
* the downsampler keeping the first and last point, which is what makes a
  thinned curve still agree with the Parquet at the two values a reader
  checks;
* the congestion ratio being >= 1.0 wherever the snapshot is slower than
  free-flow, and exactly 1.0 where it is not slower at all.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import networkx as nx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bioroute.algorithms.base import run_to_convergence  # noqa: E402
from bioroute.algorithms.classical import DijkstraRouter  # noqa: E402
from bioroute.congestion.snapshot import TrafficSnapshot  # noqa: E402
from bioroute.viz.traces import congestion_ratios, downsample  # noqa: E402


# ==========================================================================
# Toy fixtures
# ==========================================================================


def _toy_graph() -> nx.MultiDiGraph:
    """A 5-node chain plus a slower direct shortcut.

    Colinear along a meridian at 0.001 deg (~111 m) spacing so that
    `travel_time` is physically consistent with `length / speed`, the same
    construction the Phase 3 tests use. The chain A-B-C-D-E is the
    optimum (4 x 11.1 s) and the A->E shortcut is deliberately worse
    (88.8 s).
    """
    G = nx.MultiDiGraph()
    coords = {
        "A": (0.0000, 0.0),
        "B": (0.0010, 0.0),
        "C": (0.0020, 0.0),
        "D": (0.0030, 0.0),
        "E": (0.0040, 0.0),
    }
    for node, (y, x) in coords.items():
        G.add_node(node, y=y, x=x)

    seg_m = 111.19
    for u, v in (("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")):
        G.add_edge(u, v, 0, length=seg_m, speed_kph=36.0, travel_time=seg_m / 10.0)
    G.add_edge("A", "E", 0, length=4 * seg_m, speed_kph=18.0,
               travel_time=4 * seg_m / 5.0)
    return G


def _freeflow_snapshot(G: nx.MultiDiGraph) -> TrafficSnapshot:
    return TrafficSnapshot(
        level="freeflow",
        time_bin=0,
        source="freeflow",
        travel_time={
            (u, v, k): float(d["travel_time"]) for u, v, k, d in G.edges(keys=True, data=True)
        },
    )


def _scaled_snapshot(G: nx.MultiDiGraph, scale: dict[tuple, float]) -> TrafficSnapshot:
    return TrafficSnapshot(
        level="heavy",
        time_bin=0,
        source="bpr",
        travel_time={
            (u, v, k): float(d["travel_time"]) * scale.get((u, v, k), 1.0)
            for u, v, k, d in G.edges(keys=True, data=True)
        },
    )


class _ToyIterativeRouter(DijkstraRouter):
    """A router that improves over several iterations, for the trace test.

    `DijkstraRouter` is one-shot, so it produces a length-1 trace and
    cannot exercise "monotone over many iterations". This subclass hands
    out a decreasing sequence of best costs and then holds, which is the
    shape every real iterative router here produces -- and it deliberately
    includes a repeated value, because a trace must be monotone
    NON-INCREASING, not strictly decreasing.
    """

    name = "toy_iterative"

    SEQUENCE = (None, 90.0, 60.0, 60.0, 44.5)

    def prepare(self, G, source, target, snapshot, seed):  # noqa: D102
        super().prepare(G, source, target, snapshot, seed)
        self._i = 0
        self._cost: float | None = None

    def step(self):  # noqa: D102
        if self._i < len(self.SEQUENCE):
            self._cost = self.SEQUENCE[self._i]
            self._i += 1
        return self._cost

    def best_cost(self):  # noqa: D102
        return self._cost

    def best_path(self):  # noqa: D102
        return None if self._cost is None else ["A", "B", "C", "D", "E"]

    def converged(self):  # noqa: D102
        return False


# ==========================================================================
# The convergence trace's structural invariants
# ==========================================================================


def test_trace_is_monotone_nonincreasing_and_ends_at_best_cost():
    """A best-cost trace may repeat a value but must never rise, must not
    exceed the budget, and must end on exactly what the router reports."""
    G = _toy_graph()
    snapshot = _freeflow_snapshot(G)
    budget = 8

    router = _ToyIterativeRouter()
    router.prepare(G, "A", "E", snapshot, seed=0)
    result = run_to_convergence(router, budget)

    assert len(result.trace) <= budget
    assert result.iterations == len(result.trace)

    finite = [v for v in result.trace if math.isfinite(v)]
    assert finite, "the toy router does find a path"
    for earlier, later in zip(finite, finite[1:]):
        assert later <= earlier, f"trace rose: {earlier} -> {later}"

    # `inf` entries only ever come FIRST: once a path exists it is never
    # unfound again, so an inf after a finite value would mean the router
    # lost its best path.
    first_finite = next(i for i, v in enumerate(result.trace) if math.isfinite(v))
    assert all(math.isfinite(v) for v in result.trace[first_finite:])

    assert result.trace[-1] == pytest.approx(router.best_cost())
    assert result.cost == pytest.approx(router.best_cost())


def test_trace_of_a_one_shot_router_has_length_one():
    """Dijkstra converges immediately, so its trace must be a single point
    rather than `budget` copies of the same number -- the Router contract
    says a one-shot router is never inflated to look iterative."""
    G = _toy_graph()
    snapshot = _freeflow_snapshot(G)

    router = DijkstraRouter()
    router.prepare(G, "A", "E", snapshot, seed=0)
    result = run_to_convergence(router, budget=25)

    assert result.iterations == 1
    assert len(result.trace) == 1
    assert result.trace[0] == pytest.approx(result.cost)
    assert result.converged


# ==========================================================================
# Downsampling
# ==========================================================================


def test_downsample_is_a_noop_below_the_ceiling():
    values = [5.0, 4.0, 3.0]
    idx, kept = downsample(values, max_points=10)
    assert idx == [0, 1, 2]
    assert kept == values


def test_downsample_preserves_first_and_last_points():
    values = [float(100 - i) for i in range(300)]
    idx, kept = downsample(values, max_points=25)

    assert len(kept) <= 26  # the endpoint forcing may add at most one
    assert idx[0] == 0
    assert idx[-1] == len(values) - 1
    assert kept[0] == values[0]
    assert kept[-1] == values[-1]
    assert idx == sorted(set(idx)), "indices must be unique and ascending"
    assert all(kept[i] == values[j] for i, j in enumerate(idx))


def test_downsample_rejects_a_ceiling_that_cannot_hold_both_endpoints():
    with pytest.raises(ValueError):
        downsample([1.0, 2.0, 3.0], max_points=1)


# ==========================================================================
# Congestion ratios
# ==========================================================================


def test_congestion_ratio_is_at_least_one_when_the_snapshot_is_slower():
    G = _toy_graph()
    scale = {("A", "B", 0): 2.5, ("B", "C", 0): 1.0, ("C", "D", 0): 4.0}
    snapshot = _scaled_snapshot(G, scale)

    ratios = congestion_ratios(G, snapshot)

    assert ratios[("A", "B", 0)] == pytest.approx(2.5)
    assert ratios[("C", "D", 0)] == pytest.approx(4.0)
    # Untouched edges report exactly free-flow, which is what makes "ratio
    # == 1.0" readable as "no congestion observed here".
    assert ratios[("B", "C", 0)] == pytest.approx(1.0)
    assert ratios[("D", "E", 0)] == pytest.approx(1.0)
    assert all(r >= 1.0 for r in ratios.values())
    assert len(ratios) == G.number_of_edges()


def test_congestion_ratio_skips_edges_missing_from_the_snapshot():
    """A coverage gap is reported as absent, never imputed as 1.0.

    Filling it with free-flow would put a congestion measurement on an
    edge where none exists, and figure 6 would draw it as a confidently
    uncongested road.
    """
    G = _toy_graph()
    full = _freeflow_snapshot(G)
    partial = TrafficSnapshot(
        level="light",
        time_bin=0,
        source="sumo",
        travel_time={
            key: value
            for key, value in full.travel_time.items()
            if key != ("D", "E", 0)
        },
    )

    ratios = congestion_ratios(G, partial)

    assert ("D", "E", 0) not in ratios
    assert len(ratios) == G.number_of_edges() - 1
