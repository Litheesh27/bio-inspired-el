"""Analytic BPR (Bureau of Public Roads) volume-delay congestion fallback.

IMPLEMENTED IN: Phase 2a. Built before the SUMO pipeline so it unblocks
Phases 3-9 immediately; remains the fallback CongestionProvider whenever
SUMO edge-match coverage is insufficient or SUMO is unavailable.

`BPRProvider` computes per-edge congested travel time via:

  1. Per-edge capacity (veh/h) from `highway` type and lane count
     (capacity_per_lane * lanes), both configurable in configs/congestion.yaml.
     OSM `lanes` is sparsely tagged in this study area, so a default lane
     count by highway type is used wherever the tag is missing; the number
     of edges that used the default vs. an actual tag is tracked and
     reported (`lane_stats()`).
  2. A seeded synthetic OD demand: N (source, target) node pairs sampled
     uniformly at random from the graph, each carrying a configurable trip
     volume.
  3. Incremental (all-or-nothing per increment) assignment: total demand is
     split into K increments; each increment is routed on the *current*
     BPR-congested travel times via Dijkstra, volumes accumulate on edges,
     and travel times are recomputed via the BPR formula after every
     increment. This approximates user equilibrium without a full
     Frank-Wolfe solver.

     Each congestion level ("light", "heavy") runs its OWN independent
     assignment from zero demand -- not two checkpoints of one shared ramp.
     Under user equilibrium, raising total demand can genuinely reduce flow
     on some individual edges because drivers reroute; forcing elementwise
     heavy >= light would have meant faking that away. The two levels do
     share the same sampled OD pairs per time_bin (so they differ only in
     demand volume, not in which trips exist), but each is routed on its own
     congested-cost trajectory.
  4. A demand multiplier per time bin, so congestion is time-varying.

Snapshots are deterministic given a seed and are cached to disk (keyed by
area-config hash + congestion-config hash + level + time_bin + seed) so
Phases 4-8's sweeps don't recompute a 10-increment assignment per run.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

import networkx as nx
import yaml

from bioroute.config import AreaConfig, config_hash
from bioroute.congestion.base import CongestionProvider
from bioroute.congestion.snapshot import EdgeKey, TrafficSnapshot


def _load_yaml(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Bumped whenever the assignment algorithm changes shape (independent of
# any YAML config edit) so stale on-disk snapshots are never silently
# reused across an algorithm change. v2: independent per-level assignment
# (each level runs its own ramp from zero demand), replacing v1's shared
# ramp where "light" was a checkpoint of the "heavy" run.
_ASSIGNMENT_VERSION = "v2-independent-per-level"


def _canonical_hash(d: dict[str, Any]) -> str:
    canonical = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _highway_of(data: dict) -> str:
    hwy = data.get("highway", "unclassified")
    if isinstance(hwy, list):
        hwy = hwy[0] if hwy else "unclassified"
    return str(hwy)


def _edge_lanes(data: dict, highway: str, raw_cfg: dict) -> tuple[int, bool]:
    """Return (lanes, was_tagged). `was_tagged` is True iff a usable OSM
    `lanes` tag was present on this edge; otherwise the highway-type default
    (or the global default) was used."""
    lanes_raw = data.get("lanes")
    if lanes_raw is not None:
        candidates = lanes_raw if isinstance(lanes_raw, list) else [lanes_raw]
        for c in candidates:
            try:
                lanes = int(float(c))
            except (TypeError, ValueError):
                continue
            if lanes > 0:
                return lanes, True
    default_lanes_by_hwy = raw_cfg["default_lanes_by_highway"]
    lanes = int(default_lanes_by_hwy.get(highway, raw_cfg["default_lanes"]))
    return lanes, False


def _min_weight_fn(tt_map: dict[EdgeKey, float]):
    """networkx multigraph-callable-weight convention: weight(u, v, d) where
    d is {key: attr_dict} for all parallel edges between u and v. See
    congestion/snapshot.py's TrafficSnapshot.nx_weight docstring for the
    full explanation of why this shape is required."""

    def _w(u, v, d):
        best = None
        for k in d.keys():
            val = tt_map.get((u, v, k))
            if val is None:
                continue
            if best is None or val < best:
                best = val
        return best

    return _w


class BPRProvider(CongestionProvider):
    """Analytic BPR volume-delay congestion provider with synthetic OD
    demand and incremental assignment."""

    def __init__(
        self,
        G: nx.MultiDiGraph,
        area_cfg: AreaConfig,
        congestion_cfg_path: str | Path = "configs/congestion.yaml",
        cache_dir: str | Path | None = None,
    ):
        self._G = G
        self._area_cfg = area_cfg
        self._raw_cfg = _load_yaml(congestion_cfg_path)
        self._seed = int(self._raw_cfg["seed"])
        self._area_hash = config_hash(area_cfg)
        # `_ASSIGNMENT_VERSION` is folded into the cache key so that changing
        # the assignment algorithm itself (e.g. shared-ramp -> independent
        # per-level assignment) invalidates on-disk snapshots even if the
        # YAML config content were ever unchanged.
        self._cong_hash = _canonical_hash(
            {**self._raw_cfg, "_assignment_version": _ASSIGNMENT_VERSION}
        )
        self._cache_dir = Path(cache_dir) if cache_dir is not None else Path(
            self._raw_cfg.get("cache_dir", "data/results/congestion_cache")
        )

        self._t0: dict[EdgeKey, float] = {
            (u, v, k): float(data["travel_time"])
            for u, v, k, data in G.edges(keys=True, data=True)
        }
        self._capacity, self._n_tagged, self._n_default = self._build_capacity()
        self._od_pairs = self._sample_od_pairs()

    # -- CongestionProvider interface ------------------------------------

    @property
    def levels(self) -> tuple[str, ...]:
        return ("light", "heavy")

    @property
    def n_time_bins(self) -> int:
        return int(self._raw_cfg["demand"]["time_bins"])

    def snapshot(self, level: str, time_bin: int = 0) -> TrafficSnapshot:
        if level not in self.levels:
            raise ValueError(f"Unknown BPR level {level!r}; expected one of {self.levels}")
        if not (0 <= time_bin < self.n_time_bins):
            raise ValueError(f"time_bin {time_bin} out of range [0, {self.n_time_bins})")

        cache_path = self._cache_path(level, time_bin)
        cached = self._load_cache(cache_path)
        if cached is not None:
            return cached

        # Each level runs its own independent incremental assignment from
        # zero demand (see _assign). Light and heavy therefore differ only
        # in total demand volume and are NOT guaranteed elementwise-ordered
        # on every edge -- see the module docstring.
        travel_time = self._assign(level, time_bin)
        snap = TrafficSnapshot(
            level=level,
            time_bin=time_bin,
            source="bpr",
            travel_time=travel_time,
            meta={
                "area_config_hash": self._area_hash,
                "congestion_config_hash": self._cong_hash,
                "seed": self._seed,
                "n_od_pairs": len(self._od_pairs),
                "n_lanes_tagged": self._n_tagged,
                "n_lanes_default": self._n_default,
            },
        )
        self._save_cache(cache_path, snap)
        return snap

    # -- Reporting helpers -------------------------------------------------

    def lane_stats(self) -> tuple[int, int]:
        """Return (n_edges_with_tagged_lanes, n_edges_with_default_lanes)."""
        return self._n_tagged, self._n_default

    # -- Capacity / demand construction ------------------------------------

    def _build_capacity(self) -> tuple[dict[EdgeKey, float], int, int]:
        cap_per_lane = self._raw_cfg["capacity_veh_h_per_lane"]
        default_cap = self._raw_cfg["default_capacity_veh_h_per_lane"]
        capacity: dict[EdgeKey, float] = {}
        n_tagged = 0
        n_default = 0
        for u, v, k, data in self._G.edges(keys=True, data=True):
            highway = _highway_of(data)
            lanes, was_tagged = _edge_lanes(data, highway, self._raw_cfg)
            if was_tagged:
                n_tagged += 1
            else:
                n_default += 1
            per_lane = float(cap_per_lane.get(highway, default_cap))
            capacity[(u, v, k)] = per_lane * lanes
        return capacity, n_tagged, n_default

    def _sample_od_pairs(self) -> list[tuple[int, int]]:
        rng = random.Random(self._seed)
        nodes = sorted(self._G.nodes())
        n_pairs = int(self._raw_cfg["demand"]["n_od_pairs"])
        pairs: list[tuple[int, int]] = []
        attempts = 0
        max_attempts = n_pairs * 50
        while len(pairs) < n_pairs and attempts < max_attempts:
            attempts += 1
            s, t = rng.sample(nodes, 2)
            pairs.append((s, t))
        return pairs

    # -- Incremental (approximate user-equilibrium) assignment -------------

    def _assign(self, level: str, time_bin: int) -> dict[EdgeKey, float]:
        """Run ONE independent incremental all-or-nothing assignment from
        zero demand up to ``level``'s total demand for this time_bin. Each
        level gets its own volume trajectory and its own BPR-recomputed
        costs at every increment -- it is a full, independently-converged
        approximate equilibrium, not a checkpoint borrowed from a bigger
        run. The only thing shared across levels is the OD sample
        (``self._od_pairs``), so the two levels differ only in demand
        volume, not in which trips exist."""
        G = self._G
        alpha = float(self._raw_cfg["bpr"]["alpha"])
        beta = float(self._raw_cfg["bpr"]["beta"])
        demand_cfg = self._raw_cfg["demand"]
        n_increments = int(demand_cfg["n_increments"])
        multiplier = float(demand_cfg["time_bin_multipliers"][time_bin])
        total = float(demand_cfg[f"trips_per_pair_{level}"]) * multiplier
        increment_per_pair = total / n_increments

        volume: dict[EdgeKey, float] = {e: 0.0 for e in self._t0}
        current_tt: dict[EdgeKey, float] = dict(self._t0)

        for _ in range(n_increments):
            weight_fn = _min_weight_fn(current_tt)
            for (s, t) in self._od_pairs:
                try:
                    path = nx.shortest_path(G, s, t, weight=weight_fn)
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    continue
                for u, v in zip(path, path[1:]):
                    edge_dict = G.get_edge_data(u, v)
                    best_k, best_t = None, None
                    for k in edge_dict.keys():
                        tt = current_tt.get((u, v, k))
                        if tt is None:
                            continue
                        if best_t is None or tt < best_t:
                            best_t, best_k = tt, k
                    if best_k is None:
                        continue
                    volume[(u, v, best_k)] += increment_per_pair

            for e, t0_val in self._t0.items():
                v = volume[e]
                c = self._capacity[e]
                current_tt[e] = t0_val * (1.0 + alpha * (v / c) ** beta)

        return current_tt

    # -- Disk cache ---------------------------------------------------------

    def _cache_path(self, level: str, time_bin: int) -> Path:
        name = (
            f"bpr_{self._area_hash}_{self._cong_hash}_{level}_bin{time_bin}"
            f"_seed{self._seed}.json"
        )
        return self._cache_dir / name

    @staticmethod
    def _load_cache(path: Path) -> TrafficSnapshot | None:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        travel_time = {
            tuple(int(x) for x in key.split("_")): float(val)
            for key, val in raw["travel_time"].items()
        }
        return TrafficSnapshot(
            level=raw["level"],
            time_bin=raw["time_bin"],
            source=raw["source"],
            travel_time=travel_time,
            meta=raw["meta"],
        )

    @staticmethod
    def _save_cache(path: Path, snap: TrafficSnapshot) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "level": snap.level,
            "time_bin": snap.time_bin,
            "source": snap.source,
            "meta": snap.meta,
            "travel_time": {
                f"{u}_{v}_{k}": val for (u, v, k), val in snap.travel_time.items()
            },
        }
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(raw, f)
        tmp.replace(path)
