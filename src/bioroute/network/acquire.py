"""Road network acquisition from OpenStreetMap / Overpass.

IMPLEMENTED IN: Phase 1.
Will contain: a single Overpass fetch of the Central Bengaluru bbox that
produces two aligned artifacts from the same snapshot — (a) raw OSM XML
saved to data/raw/central_blr.osm.xml (consumed by netconvert in Phase 2),
and (b) an OSMnx MultiDiGraph via ox.graph_from_bbox(..., network_type=
"drive", simplify=True). The Overpass snapshot timestamp is recorded in
cache metadata so both artifacts can be traced back to one fetch.
"""
