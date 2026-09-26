"""CongestionProvider interface and TrafficSnapshot data structure.

IMPLEMENTED IN: Phase 2a. This must be built and stable before congestion/
bpr.py and congestion/sumo.py so that Phases 3-9 never depend on which
congestion source is behind it.
Will contain: an abstract `CongestionProvider` with
`snapshot(level, time_bin) -> TrafficSnapshot`, and a `TrafficSnapshot`
type mapping every graph edge (u, v, k) to a congested travel time (and
optionally density). Algorithms never see the congestion source directly —
only a TrafficSnapshot — which is what keeps the SUMO/BPR swap free.
"""
