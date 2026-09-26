"""Web dashboard JSON bundle export.

IMPLEMENTED IN: Phase 9, driven by scripts/05_export_web.py. Load the
`artifact-design` skill before writing the dashboard page.
Will contain: export of a pre-computed JSON bundle (downsampled
convergence traces to ~100 points, simplified edge geometry) consumed by
the self-contained HTML dashboard in web/, published as an Artifact, with
an interactive OD-pair selector, the three routes drawn on Bengaluru
street geometry, a disruption toggle showing re-routing, convergence
traces, and summary tables. Must stay well under the 16 MB artifact size
limit.
"""
