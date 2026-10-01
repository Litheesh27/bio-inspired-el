"""The full experiment matrix definition.

IMPLEMENTED IN: Phase 8.

This module is PURE DATA. It builds, from a YAML config plus the cached
graph, the complete list of things the sweep will run -- and it does so
without touching a router, a provider or the disk beyond reading configs.
That is deliberate: `scripts/03_run_experiments.py --dry-run` must be able
to print the exact cell count and the projected cost of the sweep without
executing one solve, and the test suite must be able to assert the matrix
arithmetic on a toy graph in milliseconds.

--------------------------------------------------------------------------
THE TWO LEVELS: CELL vs. RUN
--------------------------------------------------------------------------

A **cell** is one routing problem:  (OD pair, scenario, traffic condition).
A **run** is one measured solve inside a cell: (algorithm, warm/cold, seed).

The distinction is load-bearing, not cosmetic. Every run in a cell must be
scored against the SAME Dijkstra denominator on the SAME disrupted graph,
or `stretch` stops being comparable across algorithms and the project's
central invariant is silently violated (CLAUDE.md: "never compare stretch
across differently-weighted graphs"). So the cell owns:

  * the pre-disruption Dijkstra reference cost,
  * the disruption edge set (generated ONCE, from the cell's own scenario
    seed -- never from the algorithm seed, or ACO seed 3 would be solving a
    different closure than ACO seed 4 and the comparison would be unpaired),
  * the disrupted graph and its post-disruption Dijkstra reference cost.

The runner computes those four things once per cell and passes them into
`run_and_collect` through its precomputed arguments. `matrix.py` guarantees
that grouping exists in the first place by emitting cells, not a flat list
of runs.

--------------------------------------------------------------------------
SEEDS PER ALGORITHM, AND WHY THEY DIFFER
--------------------------------------------------------------------------

A seed is only worth paying for if it changes the answer.

  * **ACO is stochastic** -- Phase 4d showed one OD pair giving
    {1.000, 1.000, 1.000, 1.233, 1.446} across five seeds, and a
    single-seed run of the exit criterion could honestly have reported
    either a pass or a miss. 5 seeds.
  * **Dijkstra / A\\* are fully deterministic.** Five identical rows that
    differ only in `wallclock_s` are not five measurements. 1 seed.
  * **Physarum, as implemented in Phase 5, is also fully deterministic**:
    `PhysarumRouter` stores the seed and never reads it; `D_init` is a
    uniform constant over every edge and no RNG is touched anywhere in the
    module. Extra seeds would therefore produce byte-identical paths. The
    config ships 1 seed with that stated reason, and the knob is a config
    value (`seeds:`) so re-enabling 3 is a one-line change the day
    initialisation actually varies.

--------------------------------------------------------------------------
WARM / COLD
--------------------------------------------------------------------------

Warm-start is "retain the pheromone / conductivity across the closure";
cold-start is the control that re-initialises. It is only a distinction for
a router with adaptation state to retain. `_OneShotRouter.apply_disruption`
documents that Dijkstra and A* behave identically either way, so emitting
both would double the classical row count with duplicate evidence. The
classical algorithms get `warm_start=False`, once.

S0 is the no-disruption control: nothing is severed, so there is nothing to
recover warm or cold from, and every algorithm gets `warm_start=False`.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from bioroute.metrics.collectors import make_run_id

NodeId = Any

#: Scenario ids this phase knows about. Kept as a literal tuple rather than
#: imported from `disruption.scenarios` so the matrix stays importable (and
#: testable) without networkx being involved.
ALL_SCENARIOS: tuple[str, ...] = ("S0", "S1", "S2", "S3", "S4", "S5", "S6")

CONTROL_SCENARIO = "S0"

#: Algorithms with no internal adaptation state: one mode, one seed.
CLASSICAL_KIND = "classical"
ITERATIVE_KIND = "iterative"

#: The OD draws that are already SPENT on tuning and on the Phase 4d exit
#: criterion. Phase 8's sample must be disjoint from both or its numbers are
#: contaminated by the pairs the hyperparameters were chosen on.
#: (2024 = the Phase 4/4b/4c tuning + criterion draw, of which the FIRST 10
#: were the tuning set; 4242 = the Phase 4d held-out criterion draw.)
SPENT_PAIR_DRAWS: tuple[tuple[int, int], ...] = ((2024, 30), (4242, 30))


# ==========================================================================
# OD sampling
# ==========================================================================


def legacy_draw_od_pairs(nodes: Sequence[NodeId], seed: int, n_pairs: int) -> list[tuple]:
    """Reproduce EXACTLY the OD draw used by Phase 4's tuning and by
    `scripts/measure_aco_criterion.py`.

    Not used to draw Phase 8's sample -- only to reconstruct the samples
    Phase 8 must avoid. It must therefore stay bit-identical to the
    original (`random.Random(seed).sample(nodes, 2)` over `sorted(G.nodes())`),
    which is why it is duplicated here rather than refactored: changing the
    procedure would silently change which pairs are considered "spent".
    """
    rng = random.Random(seed)
    nodes = list(nodes)
    return [tuple(rng.sample(nodes, 2)) for _ in range(n_pairs)]


def spent_pairs(nodes: Sequence[NodeId]) -> set[tuple]:
    """Every ORDERED OD pair already used by tuning or by Phase 4d."""
    out: set[tuple] = set()
    for seed, n in SPENT_PAIR_DRAWS:
        out.update(legacy_draw_od_pairs(nodes, seed, n))
    return out


def spent_pairs_unordered(nodes: Sequence[NodeId]) -> set[frozenset]:
    """The same pairs, direction-insensitive. A reversed pair is a different
    routing problem on a directed graph, but it shares the corridor, so
    Phase 8 excludes it too rather than arguing about it later."""
    return {frozenset(p) for p in spent_pairs(nodes)}


def _haversine_m(y1: float, x1: float, y2: float, x2: float) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    r = 6371000.0
    p1, p2 = math.radians(y1), math.radians(y2)
    dp = p2 - p1
    dl = math.radians(x2 - x1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class ODPair:
    """One origin-destination pair, with the stratum it was drawn for."""

    source: NodeId
    target: NodeId
    stratum: str
    distance_m: float

    def as_tuple(self) -> tuple:
        return (self.source, self.target)


@dataclass(frozen=True)
class ODSampleConfig:
    """How the 30 OD pairs are drawn.

    `seed` must differ from every entry in `SPENT_PAIR_DRAWS`; the drawn
    sample is additionally filtered against those draws (ordered AND
    unordered), so out-of-sampleness is enforced, not merely hoped for.
    """

    n_pairs: int = 30
    strata: tuple[str, ...] = ("short", "medium", "long")
    seed: int = 8080
    pool_size: int = 4000
    exclude_spent: bool = True


def sample_od_pairs(G, cfg: ODSampleConfig) -> list[ODPair]:
    """Draw `cfg.n_pairs` OD pairs, stratified evenly by straight-line
    distance, deterministically from `cfg.seed`.

    Procedure (fixed, so it is reproducible from the config alone):
      1. `nodes = sorted(G.nodes())` -- the project's standard ordering;
         `random.Random.sample` is order-sensitive, and the spent draws
         above were made on exactly this ordering.
      2. draw a pool of `pool_size` candidate ordered pairs;
      3. drop any pair that collides with a spent draw (ordered or
         unordered) and any duplicate within the pool;
      4. cut the pool at its own 1/3 and 2/3 distance quantiles -- the band
         edges come from the sampled distribution, not from hard-coded
         metres, so the strata stay balanced if the study area ever changes;
      5. take the first `n_pairs / len(strata)` survivors of each band IN
         DRAW ORDER.

    Taking them in draw order (rather than, say, the most extreme ones)
    keeps each stratum a uniform sample of its band instead of a sample of
    its edge.
    """
    nodes = sorted(G.nodes())
    if len(nodes) < 2:
        raise ValueError("Graph needs at least 2 nodes to draw OD pairs")
    n_strata = len(cfg.strata)
    if cfg.n_pairs % n_strata:
        raise ValueError(
            f"n_pairs ({cfg.n_pairs}) must divide evenly across "
            f"{n_strata} strata {cfg.strata}"
        )
    per_stratum = cfg.n_pairs // n_strata

    for spent_seed, _ in SPENT_PAIR_DRAWS:
        if cfg.seed == spent_seed:
            raise ValueError(
                f"OD seed {cfg.seed} is one of the already-spent draws "
                f"{[s for s, _ in SPENT_PAIR_DRAWS]}; Phase 8's sample must be "
                "out-of-sample with respect to tuning and the Phase 4d criterion."
            )

    banned = spent_pairs(nodes) if cfg.exclude_spent else set()
    banned_unordered = spent_pairs_unordered(nodes) if cfg.exclude_spent else set()

    rng = random.Random(cfg.seed)
    pool: list[tuple[tuple, float]] = []
    seen: set[tuple] = set()
    for _ in range(max(cfg.pool_size, cfg.n_pairs * 10)):
        s, t = rng.sample(nodes, 2)
        pair = (s, t)
        if pair in seen or pair in banned or frozenset(pair) in banned_unordered:
            continue
        seen.add(pair)
        d = _haversine_m(
            float(G.nodes[s]["y"]),
            float(G.nodes[s]["x"]),
            float(G.nodes[t]["y"]),
            float(G.nodes[t]["x"]),
        )
        pool.append((pair, d))

    if len(pool) < cfg.n_pairs:
        raise ValueError(
            f"OD pool has only {len(pool)} usable pairs for {cfg.n_pairs} requested; "
            "raise pool_size or shrink n_pairs."
        )

    distances = sorted(d for _, d in pool)
    lo = distances[len(distances) // 3]
    hi = distances[(2 * len(distances)) // 3]

    def stratum_of(d: float) -> str:
        if d < lo:
            return cfg.strata[0]
        if d < hi:
            return cfg.strata[1]
        return cfg.strata[-1]

    buckets: dict[str, list[ODPair]] = {name: [] for name in cfg.strata}
    for (s, t), d in pool:
        name = stratum_of(d)
        if len(buckets[name]) < per_stratum:
            buckets[name].append(ODPair(s, t, name, float(d)))

    short_of = [n for n in cfg.strata if len(buckets[n]) < per_stratum]
    if short_of:
        raise ValueError(
            f"Could not fill strata {short_of} with {per_stratum} pairs each "
            f"from a pool of {len(pool)}; raise pool_size."
        )

    out: list[ODPair] = []
    for name in cfg.strata:
        out.extend(buckets[name])
    return out


def od_overlap_report(pairs: Sequence[ODPair], G) -> dict[str, Any]:
    """Prove (rather than assert) that the sample is out-of-sample.

    Returns the ordered and unordered intersection sizes against the
    tuning / Phase 4d draws, plus the shared-endpoint count as context --
    with thousands of nodes a few shared endpoints are expected by chance
    and do NOT make a pair in-sample.
    """
    nodes = sorted(G.nodes())
    mine = {p.as_tuple() for p in pairs}
    mine_unordered = {frozenset(p.as_tuple()) for p in pairs}
    mine_nodes = {n for p in pairs for n in p.as_tuple()}

    report: dict[str, Any] = {
        "n_pairs": len(pairs),
        "n_nodes_in_graph": len(nodes),
        "by_draw": {},
        "ordered_overlap_total": 0,
        "unordered_overlap_total": 0,
    }
    for seed, n in SPENT_PAIR_DRAWS:
        old = legacy_draw_od_pairs(nodes, seed, n)
        old_set = set(old)
        old_unordered = {frozenset(p) for p in old}
        old_nodes = {x for p in old for x in p}
        entry = {
            "n_pairs": len(old),
            "ordered_overlap": len(mine & old_set),
            "unordered_overlap": len(mine_unordered & old_unordered),
            "shared_endpoint_nodes": len(mine_nodes & old_nodes),
        }
        # The first 10 of the 2024 draw were the ACO tuning set specifically.
        if seed == 2024:
            tuning = set(old[:10])
            entry["ordered_overlap_with_tuning_subset"] = len(mine & tuning)
        report["by_draw"][str(seed)] = entry
        report["ordered_overlap_total"] += entry["ordered_overlap"]
        report["unordered_overlap_total"] += entry["unordered_overlap"]
    return report


# ==========================================================================
# Config
# ==========================================================================


@dataclass(frozen=True)
class AlgorithmSpec:
    """One algorithm's place in the matrix."""

    name: str
    kind: str = ITERATIVE_KIND
    seeds: tuple[int, ...] = (0,)
    budget: int = 100
    #: Extra keyword arguments handed to the router constructor.
    options: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_classical(self) -> bool:
        return self.kind == CLASSICAL_KIND

    @property
    def warm_modes(self) -> tuple[bool, ...]:
        """Warm/cold modes for a DISRUPTION scenario. A classical router has
        one mode -- see the module docstring."""
        return (False,) if self.is_classical else (False, True)


@dataclass(frozen=True)
class Condition:
    """One traffic condition: a congestion source, a level, a time bin."""

    congestion_source: str
    level: str
    time_bin: int = 0

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.congestion_source, self.level, self.time_bin)

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.congestion_source}:{self.level}@{self.time_bin}"


@dataclass(frozen=True)
class ExperimentConfig:
    """The resolved experiment definition. Everything that can change a
    NUMBER is in here and therefore in `config_hash`; everything that only
    changes how the sweep is executed (workers, output dir) is not."""

    name: str
    area_config_path: str
    baseline_graph: str
    conditions: tuple[Condition, ...]
    scenarios: tuple[str, ...]
    algorithms: tuple[AlgorithmSpec, ...]
    od: ODSampleConfig
    scenario_seed: int
    disruption: Mapping[str, Any]
    #: Where netconvert's flag list comes from; it is part of the identity
    #: of the `sumo_speeds` baseline artifact, so it belongs in the hash.
    sumo_config_path: str = "configs/sumo.yaml"
    # -- execution-only, excluded from the hash ---------------------------
    output_dir: str = "data/results"
    shard_dirname: str = ""
    workers: int = 14
    flush_every: int = 5
    allow_inadmissible_astar: bool = False
    cost_estimate_s_per_solve: Mapping[str, float] = field(default_factory=dict)
    smoke: Mapping[str, Any] = field(default_factory=dict)
    area_config_hash: str = ""

    # ------------------------------------------------------------------
    def algorithm(self, name: str) -> AlgorithmSpec:
        for spec in self.algorithms:
            if spec.name == name:
                return spec
        raise KeyError(name)

    def to_canonical_dict(self) -> dict[str, Any]:
        """Exactly the fields that can change a result value."""
        return {
            "area_config_path": self.area_config_path,
            "area_config_hash": self.area_config_hash,
            "baseline_graph": self.baseline_graph,
            "sumo_config_path": self.sumo_config_path,
            "conditions": [list(c.key) for c in self.conditions],
            "scenarios": list(self.scenarios),
            "algorithms": [
                {
                    "name": a.name,
                    "kind": a.kind,
                    "seeds": list(a.seeds),
                    "budget": a.budget,
                    "options": dict(sorted(a.options.items())),
                }
                for a in self.algorithms
            ],
            "od": {
                "n_pairs": self.od.n_pairs,
                "strata": list(self.od.strata),
                "seed": self.od.seed,
                "pool_size": self.od.pool_size,
                "exclude_spent": self.od.exclude_spent,
            },
            "scenario_seed": self.scenario_seed,
            "disruption": dict(sorted(self.disruption.items())),
        }

    @property
    def config_hash(self) -> str:
        """Short sha256 over the canonical dict.

        Stamped onto every result row and folded into every `run_id`, so
        changing any experimental parameter invalidates resume (CLAUDE.md:
        "changing a config must invalidate the cache") instead of silently
        mixing rows from two different definitions into one Parquet file.
        """
        canonical = json.dumps(
            self.to_canonical_dict(), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

    @property
    def shard_dir(self) -> Path:
        name = self.shard_dirname or f"shards_{self.config_hash}"
        return Path(self.output_dir) / name

    @property
    def final_path(self) -> Path:
        return Path(self.output_dir) / f"final_{self.config_hash}.parquet"


class ConfigError(ValueError):
    """A malformed or unsafe experiment config."""


def _as_condition(raw: Mapping[str, Any]) -> Condition:
    try:
        return Condition(
            congestion_source=str(raw["congestion_source"]),
            level=str(raw["level"]),
            time_bin=int(raw.get("time_bin", 0)),
        )
    except KeyError as exc:  # pragma: no cover - config typo path
        raise ConfigError(f"condition entry missing key {exc}") from exc


def _as_algorithm(raw: Mapping[str, Any]) -> AlgorithmSpec:
    name = str(raw["name"])
    kind = str(raw.get("kind", ITERATIVE_KIND))
    if kind not in (CLASSICAL_KIND, ITERATIVE_KIND):
        raise ConfigError(f"algorithm {name}: unknown kind {kind!r}")
    n_seeds = int(raw.get("seeds", 1))
    if n_seeds < 1:
        raise ConfigError(f"algorithm {name}: seeds must be >= 1")
    seed_start = int(raw.get("seed_start", 0))
    seeds = tuple(seed_start + i for i in range(n_seeds))
    budget = int(raw.get("budget", 1))
    if budget < 1:
        raise ConfigError(f"algorithm {name}: budget must be >= 1")
    return AlgorithmSpec(
        name=name,
        kind=kind,
        seeds=seeds,
        budget=budget,
        options=dict(raw.get("options", {}) or {}),
    )


def _deep_merge(base: dict[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, val in over.items():
        if isinstance(val, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(dict(out[key]), val)
        else:
            out[key] = val
    return out


def load_experiment_config(
    path: str | Path,
    smoke: bool = False,
    overrides: Mapping[str, Any] | None = None,
) -> ExperimentConfig:
    """Load a `configs/experiments/*.yaml` into an `ExperimentConfig`.

    `smoke=True` applies the file's own `smoke:` block as a deep-merged
    override. The smoke overrides live in the CONFIG, not in code, so the
    smoke sweep is as reproducible and as inspectable as the real one.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if smoke:
        raw = _deep_merge(raw, raw.get("smoke", {}) or {})
    if overrides:
        raw = _deep_merge(raw, overrides)
    raw.pop("smoke_applied", None)

    scenarios = tuple(str(s) for s in raw.get("scenarios", ALL_SCENARIOS))
    unknown = [s for s in scenarios if s not in ALL_SCENARIOS]
    if unknown:
        raise ConfigError(f"unknown scenarios {unknown}; expected {ALL_SCENARIOS}")

    conditions = tuple(_as_condition(c) for c in raw.get("conditions", []))
    if not conditions:
        raise ConfigError("config defines no traffic conditions")

    algorithms = tuple(_as_algorithm(a) for a in raw.get("algorithms", []))
    if not algorithms:
        raise ConfigError("config defines no algorithms")
    names = [a.name for a in algorithms]
    if len(set(names)) != len(names):
        raise ConfigError(f"duplicate algorithm names in config: {names}")

    od_raw = raw.get("od", {}) or {}
    od = ODSampleConfig(
        n_pairs=int(od_raw.get("n_pairs", 30)),
        strata=tuple(str(s) for s in od_raw.get("strata", ("short", "medium", "long"))),
        seed=int(od_raw.get("seed", 8080)),
        pool_size=int(od_raw.get("pool_size", 4000)),
        exclude_spent=bool(od_raw.get("exclude_spent", True)),
    )

    out_raw = raw.get("output", {}) or {}
    par_raw = raw.get("parallel", {}) or {}

    cfg = ExperimentConfig(
        name=str(raw.get("name", Path(path).stem)),
        area_config_path=str(raw.get("area_config", "configs/area_central_blr.yaml")),
        baseline_graph=str(raw.get("baseline_graph", "sumo_speeds")),
        sumo_config_path=str(raw.get("sumo_config", "configs/sumo.yaml")),
        conditions=conditions,
        scenarios=scenarios,
        algorithms=algorithms,
        od=od,
        scenario_seed=int(raw.get("scenario_seed", 1337)),
        disruption=dict(raw.get("disruption", {}) or {}),
        output_dir=str(out_raw.get("dir", "data/results")),
        shard_dirname=str(out_raw.get("shard_dirname", "") or ""),
        workers=int(par_raw.get("workers", 14)),
        flush_every=int(par_raw.get("flush_every", 5)),
        allow_inadmissible_astar=bool(raw.get("allow_inadmissible_astar", False)),
        cost_estimate_s_per_solve=dict(raw.get("cost_estimate_s_per_solve", {}) or {}),
        smoke=dict(raw.get("smoke", {}) or {}),
    )
    validate_config(cfg)
    return cfg


def with_area_hash(cfg: ExperimentConfig, area_hash: str) -> ExperimentConfig:
    """Fold the area config's hash into the experiment config hash.

    Done as a separate step because reading the area config requires the
    `config` module and (for the hash to mean anything) the actual YAML on
    disk, while `load_experiment_config` must stay importable in a test
    that has neither.
    """
    return replace(cfg, area_config_hash=str(area_hash))


def validate_config(cfg: ExperimentConfig) -> None:
    """Reject configurations that would produce numbers we cannot defend.

    The one hard refusal is the A*-admissibility trap from Phase 2d:
    A*'s heuristic is a free-flow great-circle bound computed from the
    BASELINE GRAPH's speeds, so it is admissible only while snapshot times
    are >= that graph's free-flow times. That holds for FreeFlow and BPR
    always, and for SUMO on `sumo_speeds` -- but NOT for SUMO on
    `config_speeds`, where 242 edges carry SUMO times below the config-speed
    free-flow time. Running A* there yields paths that are not optimal while
    claiming to be, and the Phase 3 equality test passed on that
    configuration only by luck of the sampled pairs.
    """
    if cfg.baseline_graph not in ("config_speeds", "sumo_speeds"):
        raise ConfigError(
            f"baseline_graph {cfg.baseline_graph!r} must be 'config_speeds' or 'sumo_speeds'"
        )

    has_astar = any(a.name == "astar" for a in cfg.algorithms)
    sumo_conditions = [c for c in cfg.conditions if c.congestion_source == "sumo"]
    if has_astar and sumo_conditions and cfg.baseline_graph == "config_speeds":
        message = (
            "REFUSING to run A* on SUMO snapshots over the 'config_speeds' baseline: "
            "A*'s great-circle heuristic is INADMISSIBLE there (Phase 2d: 242 edges "
            "carry a SUMO travel time below the config-speed free-flow time), so A* "
            "would report sub-optimal paths as optimal. Fix one of: "
            "baseline_graph: sumo_speeds (the project default), drop the sumo "
            "conditions, or drop astar from `algorithms`. Set "
            "`allow_inadmissible_astar: true` only to measure the breakage itself."
        )
        if not cfg.allow_inadmissible_astar:
            raise ConfigError(message)
        print("WARNING: " + message, flush=True)

    for spec in cfg.algorithms:
        if spec.is_classical and len(spec.seeds) > 1:
            raise ConfigError(
                f"algorithm {spec.name!r} is classical (deterministic) but is "
                f"configured with {len(spec.seeds)} seeds; identical rows are not "
                "repeated measurements. Set seeds: 1."
            )


# ==========================================================================
# The matrix itself
# ==========================================================================


@dataclass(frozen=True)
class RunSpec:
    """One measured solve: a single row of the results Parquet."""

    run_id: str
    algorithm: str
    od_source: NodeId
    od_target: NodeId
    stratum: str
    scenario_id: str
    congestion_source: str
    traffic_level: str
    time_bin: int
    warm_start: bool
    seed: int
    budget: int
    config_hash: str
    baseline_graph: str


@dataclass(frozen=True)
class CellSpec:
    """One routing problem and every run measured on it.

    The runs in a cell share one Dijkstra denominator, one disruption edge
    set and one disrupted graph -- that sharing is the whole reason this
    type exists (see the module docstring).
    """

    od: ODPair
    scenario_id: str
    condition: Condition
    scenario_seed: int
    runs: tuple[RunSpec, ...]

    @property
    def is_control(self) -> bool:
        return self.scenario_id == CONTROL_SCENARIO

    @property
    def key(self) -> tuple:
        return (
            self.od.source,
            self.od.target,
            self.scenario_id,
            self.condition.congestion_source,
            self.condition.level,
            self.condition.time_bin,
        )


def build_matrix(
    cfg: ExperimentConfig,
    od_pairs: Sequence[ODPair],
    baseline_graph: str | None = None,
) -> list[CellSpec]:
    """Enumerate every cell and every run, as data.

    Nothing here touches a graph, a provider or a router -- `--dry-run`
    calls exactly this and prints the counts.
    """
    baseline = baseline_graph or cfg.baseline_graph
    config_hash = cfg.config_hash
    cells: list[CellSpec] = []

    for od in od_pairs:
        for scenario_id in cfg.scenarios:
            control = scenario_id == CONTROL_SCENARIO
            for condition in cfg.conditions:
                runs: list[RunSpec] = []
                for spec in cfg.algorithms:
                    # S0 severs nothing, so there is no warm/cold axis at
                    # all; a disruption scenario gives it to iterative
                    # routers only.
                    modes = (False,) if control else spec.warm_modes
                    for warm in modes:
                        for seed in spec.seeds:
                            run_id = make_run_id(
                                algorithm=spec.name,
                                od_source=od.source,
                                od_target=od.target,
                                scenario_id=scenario_id,
                                traffic_level=condition.level,
                                congestion_source=condition.congestion_source,
                                baseline_graph=baseline,
                                warm_start=warm,
                                seed=seed,
                                config_hash=config_hash,
                            )
                            runs.append(
                                RunSpec(
                                    run_id=run_id,
                                    algorithm=spec.name,
                                    od_source=od.source,
                                    od_target=od.target,
                                    stratum=od.stratum,
                                    scenario_id=scenario_id,
                                    congestion_source=condition.congestion_source,
                                    traffic_level=condition.level,
                                    time_bin=condition.time_bin,
                                    warm_start=warm,
                                    seed=seed,
                                    budget=spec.budget,
                                    config_hash=config_hash,
                                    baseline_graph=baseline,
                                )
                            )
                cells.append(
                    CellSpec(
                        od=od,
                        scenario_id=scenario_id,
                        condition=condition,
                        scenario_seed=cfg.scenario_seed,
                        runs=tuple(runs),
                    )
                )
    return cells


def all_runs(cells: Iterable[CellSpec]) -> list[RunSpec]:
    return [run for cell in cells for run in cell.runs]


def matrix_summary(cells: Sequence[CellSpec]) -> dict[str, Any]:
    """Counts, broken down the way the Phase 8 report has to state them."""
    runs = all_runs(cells)
    by_algorithm: dict[str, int] = {}
    by_scenario: dict[str, int] = {}
    by_condition: dict[str, int] = {}
    by_stratum: dict[str, int] = {}
    warm_counts = {"warm": 0, "cold": 0}
    seeds_by_algorithm: dict[str, set[int]] = {}
    for run in runs:
        by_algorithm[run.algorithm] = by_algorithm.get(run.algorithm, 0) + 1
        by_scenario[run.scenario_id] = by_scenario.get(run.scenario_id, 0) + 1
        ckey = f"{run.congestion_source}:{run.traffic_level}"
        by_condition[ckey] = by_condition.get(ckey, 0) + 1
        by_stratum[run.stratum] = by_stratum.get(run.stratum, 0) + 1
        warm_counts["warm" if run.warm_start else "cold"] += 1
        seeds_by_algorithm.setdefault(run.algorithm, set()).add(run.seed)

    od_pairs = {(c.od.source, c.od.target) for c in cells}
    return {
        "n_cells": len(cells),
        "n_runs": len(runs),
        "n_od_pairs": len(od_pairs),
        "n_unique_run_ids": len({r.run_id for r in runs}),
        "runs_by_algorithm": dict(sorted(by_algorithm.items())),
        "seeds_by_algorithm": {
            k: sorted(v) for k, v in sorted(seeds_by_algorithm.items())
        },
        "runs_by_scenario": dict(sorted(by_scenario.items())),
        "runs_by_condition": dict(sorted(by_condition.items())),
        "runs_by_stratum": dict(sorted(by_stratum.items())),
        "runs_by_mode": warm_counts,
    }


def project_cost(
    cells: Sequence[CellSpec],
    seconds_per_solve: Mapping[str, float],
    workers: int = 1,
    default_seconds: float = 1.0,
) -> dict[str, Any]:
    """Project the sweep's cost from a per-algorithm seconds-per-SOLVE map.

    A *run* is one or two *solves*: the initial solve always, plus a
    recovery solve for every scenario except the S0 control. That is why
    the model is per-solve rather than per-run -- a disruption row costs
    roughly twice a control row for the same algorithm.

    The parallel figure is `serial / workers` and nothing more. It is a
    LOWER BOUND presented for planning only: this machine's throughput
    swings up to 3.4x with thermal state (CLAUDE.md, Phase 4b), so a
    wall-clock number taken off a parallel run is not a performance
    measurement and must never be reported as one.
    """
    per_algorithm: dict[str, float] = {}
    n_solves: dict[str, int] = {}
    for cell in cells:
        solves = 1 if cell.is_control else 2
        for run in cell.runs:
            rate = float(seconds_per_solve.get(run.algorithm, default_seconds))
            per_algorithm[run.algorithm] = (
                per_algorithm.get(run.algorithm, 0.0) + rate * solves
            )
            n_solves[run.algorithm] = n_solves.get(run.algorithm, 0) + solves

    serial_s = sum(per_algorithm.values())
    workers = max(1, int(workers))
    return {
        "seconds_per_solve_used": dict(sorted(seconds_per_solve.items())),
        "n_solves_by_algorithm": dict(sorted(n_solves.items())),
        "serial_seconds_by_algorithm": dict(sorted(per_algorithm.items())),
        "serial_seconds": serial_s,
        "serial_hours": serial_s / 3600.0,
        "workers": workers,
        "parallel_hours_lower_bound": serial_s / 3600.0 / workers,
    }
