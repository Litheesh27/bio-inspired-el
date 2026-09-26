"""Configuration loading: YAML -> dataclasses, and seed management.

IMPLEMENTED IN: Phase 1 (introduced alongside network acquisition, but used
by every later phase).
Will contain: dataclasses mirroring configs/area_central_blr.yaml,
configs/algorithms.yaml, and configs/experiments/*.yaml; a loader that
validates and returns typed config objects; a deterministic config-hash
function (e.g. sha256 of canonicalized YAML) used to key cached artifacts
(graph files, SUMO outputs, results) so that changing a config invalidates
the cache; and a `set_global_seed(seed: int)` helper so every experiment run
carries an explicit, reproducible seed.
"""
