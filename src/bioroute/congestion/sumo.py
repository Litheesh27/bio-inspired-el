"""SUMO-backed congestion provider.

IMPLEMENTED IN: Phase 2b, driven by scripts/02_run_sumo.py.
Will contain: `SUMOCongestionProvider(CongestionProvider)` wrapping a
pipeline of netconvert (OSM XML -> net.net.xml, with
--roundabouts.guess --ramps.guess --junctions.join --tls.guess-signals,
and deliberately WITHOUT --geometry.remove so way-ID traceability survives
for network/ids.py), randomTrips.py at two --period values for light/heavy
demand, duarouter for routes, and a headless sumo run with an <edgeData>
additional file at 300s intervals producing
data/sumo/edgedata_{level}.xml (per-edge traveltime and density per bin).
Snapshots are built by joining edgedata back to graph edges via
network/ids.py.
"""
