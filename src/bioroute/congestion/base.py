"""CongestionProvider interface and the free-flow baseline provider.

IMPLEMENTED IN: Phase 2a. This must be built and stable before congestion/
bpr.py and congestion/sumo.py so that Phases 3-9 never depend on which
congestion source is behind it.

`CongestionProvider` is the single abstraction every downstream phase talks
to: `.snapshot(level, time_bin) -> TrafficSnapshot`. Algorithms never see
the congestion source directly -- only a TrafficSnapshot -- which is what
keeps the SUMO/BPR swap free (see CLAUDE.md invariants).

`FreeFlowProvider` is the trivial provider that returns the graph's own
free-flow `travel_time` unchanged. It is not a stub: Phase 5's exit
criterion (Physarum reaches stretch = 1.0 on >=80% of OD pairs) is
explicitly measured on an *uncongested* graph, so this provider is a real
dependency of a later phase, not scaffolding.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import networkx as nx

from bioroute.congestion.snapshot import TrafficSnapshot


class CongestionProvider(ABC):
    """Abstract source of congestion snapshots."""

    @property
    @abstractmethod
    def levels(self) -> tuple[str, ...]:
        """The congestion level labels this provider supports, e.g.
        ("freeflow",) or ("light", "heavy")."""
        raise NotImplementedError

    @property
    @abstractmethod
    def n_time_bins(self) -> int:
        """Number of distinct time-of-day bins this provider can produce a
        snapshot for. A provider with no time-of-day variation returns 1."""
        raise NotImplementedError

    @abstractmethod
    def snapshot(self, level: str, time_bin: int = 0) -> TrafficSnapshot:
        """Return the TrafficSnapshot for the given level and time bin."""
        raise NotImplementedError


class FreeFlowProvider(CongestionProvider):
    """Returns the graph's own free-flow `travel_time` edge attribute,
    unchanged, regardless of the requested level/time_bin. Used wherever an
    uncongested baseline is required (notably Phase 5's exit criterion)."""

    def __init__(self, G: nx.MultiDiGraph):
        self._G = G

    @property
    def levels(self) -> tuple[str, ...]:
        return ("freeflow",)

    @property
    def n_time_bins(self) -> int:
        return 1

    def snapshot(self, level: str = "freeflow", time_bin: int = 0) -> TrafficSnapshot:
        travel_time = {
            (u, v, k): float(data["travel_time"])
            for u, v, k, data in self._G.edges(keys=True, data=True)
        }
        return TrafficSnapshot(
            level="freeflow",
            time_bin=time_bin,
            source="freeflow",
            travel_time=travel_time,
            meta={"n_edges": len(travel_time)},
        )
