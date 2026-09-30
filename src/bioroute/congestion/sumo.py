"""SUMO-backed congestion provider and pipeline helpers.

IMPLEMENTED IN: Phase 2b/2c, driven by scripts/02_run_sumo.py.

Pipeline (2b): netconvert (OSM XML -> net.net.xml, deliberately WITHOUT
``--geometry.remove`` so way-ID traceability survives for
network/ids.py -- see ``run_netconvert``), ``randomTrips.py`` at two
``--period`` values for light/heavy demand (with ``--validate`` so
``duarouter`` runs as part of trip generation and only routable trips
survive), and a headless ``sumo`` run with an ``<edgeData>`` additional
file at 300 s intervals producing ``data/sumo/edgedata_{level}.xml``
(per-edge traveltime and density per bin).

Join (2c): ``SUMOProvider`` parses the edgedata files, aggregates each SUMO
edge's traveltime into a single simulation-wide, length-weighted mean (with
implausible-value clipping, see ``_clip_implausible_traveltime``), and asks
``network/ids.py`` to fold that down to one value per graph edge (u, v, k).

PHASE 2D
--------
Two changes, both driven by measurement (docs/tuning/sumo_calibration.md):

* ``period_heavy`` was recalibrated 0.6 -> 1.2. At 0.6, 30.5% of vehicles
  teleported -- i.e. jumped past a blockage instead of driving through it,
  so the edges they skipped carried travel times that never happened. The
  congestion ratio that setting produced was largely manufactured by those
  jam waits. ``_parse_sumo_stats`` now extracts the per-category teleport
  breakdown and the completed-trip statistics so this stays visible on
  every run rather than having to be re-derived from the log.
* Every snapshot records which free-flow BASELINE graph it was produced
  against (``meta["baseline"]``), and the baseline is folded into the
  snapshot cache key. Unmatched graph edges fall back to the graph's own
  ``travel_time``, so a SUMO snapshot is not independent of the baseline --
  and the two baseline graphs share an area-config hash, so without this a
  sumo-speeds run would silently read back config-speeds numbers. See
  ``network/graph.py``'s PHASE 2D section.

The ``implausible_cap_factor`` clipping was re-examined here and KEPT: the
prior that implausible values come from gridlock turned out to be wrong.
A zero-teleport, fully-drained light run still clips 1.53% of samples and
still contains a 2,141x-free-flow sample; removing the cap moves that run's
length-weighted ratio from 1.22x to 1.83x. The values come from SUMO's
per-interval traveltime being length/mean-speed over whatever vehicles
occupied the edge in that bin, which a single car stopped at a signal can
dominate. That is a sampling artefact present at every demand level.

$SUMO_HOME resolution
----------------------
$SUMO_HOME is set at Machine level (to ``C:\\Sumo\\``, note the trailing
backslash) but is NOT inherited by a plain subprocess unless explicitly
passed through, and even when present must have any trailing separator
stripped before being joined with ``bin`` / ``tools`` (an unstripped
``C:\\Sumo\\`` joined with ``bin`` yields the still-valid-looking but
easy-to-get-wrong ``C:\\Sumo\\\\bin``; more importantly, several call sites
compare or hash this path, where the trailing separator would silently
break equality/caching). ``resolve_sumo_home`` is the ONE place this
resolution happens; every other function in this module and in
``scripts/02_run_sumo.py`` calls it rather than reading ``os.environ``
directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import networkx as nx
import yaml

from bioroute.config import AreaConfig, config_hash
from bioroute.congestion.base import (
    CongestionProvider,
    load_snapshot_cache,
    save_snapshot_cache,
)
from bioroute.congestion.snapshot import EdgeKey, TrafficSnapshot
from bioroute.network.graph import baseline_hash_of, baseline_meta, baseline_of
from bioroute.network.ids import (
    ParsedSumoEdge,
    coverage_report,
    compute_edge_travel_times,
    geometric_fallback_match,
    load_sumo_edges,
    match_sumo_edges_to_graph,
)

_DEFAULT_SUMO_HOME = "C:/Sumo"

# Bumped whenever the edgedata -> graph-edge aggregation RULE changes,
# independent of any YAML edit, so stale on-disk snapshots are never
# silently reused across an algorithm change. Mirrors BPRProvider's
# `_ASSIGNMENT_VERSION`. v2: `network/ids.compute_edge_travel_times` now
# works in PACE (graph_length * sum(tt_i) / sum(L_i)) instead of taking a
# length-weighted mean of the matched segments' travel times, which was
# dimensionally wrong -- see that module's docstring.
_AGGREGATION_VERSION = "v2-length-weighted-pace"


def resolve_sumo_home() -> str:
    """Resolve $SUMO_HOME from the environment, falling back to the known
    install location on this machine, with any trailing path separator
    normalized away. This is the ONE place SUMO_HOME resolution happens;
    every other function takes the result as a parameter rather than
    re-reading the environment."""
    raw = os.environ.get("SUMO_HOME") or _DEFAULT_SUMO_HOME
    return raw.rstrip("/\\")


def sumo_bin(name: str, sumo_home: str | None = None) -> str:
    """Path to a SUMO binary (netconvert/sumo/duarouter/...) under
    $SUMO_HOME/bin, as a string suitable for a subprocess arg list."""
    home = sumo_home or resolve_sumo_home()
    return str(Path(home) / "bin" / f"{name}.exe")


def sumo_tool(name: str, sumo_home: str | None = None) -> str:
    """Path to a SUMO python tool (randomTrips.py, etc.) under
    $SUMO_HOME/tools."""
    home = sumo_home or resolve_sumo_home()
    return str(Path(home) / "tools" / name)


def _subprocess_env(sumo_home: str) -> dict[str, str]:
    env = dict(os.environ)
    env["SUMO_HOME"] = sumo_home
    return env


# ---------------------------------------------------------------------------
# 2b.1: netconvert
# ---------------------------------------------------------------------------

_WARNING_SUMMARY_RE = re.compile(r"^Warning: (\d+) total messages of type: (.*)$")


def parse_netconvert_warnings(stderr_text: str) -> dict[str, Any]:
    """Summarize netconvert's stderr: total warning-line count, plus the
    "N total messages of type: ..." rollup lines netconvert prints for
    repeated warning categories."""
    lines = stderr_text.splitlines()
    n_warnings = sum(1 for line in lines if line.startswith("Warning:"))
    categories: dict[str, int] = {}
    for line in lines:
        m = _WARNING_SUMMARY_RE.match(line)
        if m:
            categories[m.group(2)] = int(m.group(1))
    return {"n_warning_lines": n_warnings, "rollup_categories": categories}


def run_netconvert(
    osm_xml_path: str | Path,
    net_out_path: str | Path,
    extra_flags: list[str],
    sumo_home: str | None = None,
) -> dict[str, Any]:
    """Run netconvert: OSM XML -> net.net.xml. Deliberately does NOT accept
    a --geometry.remove flag in `extra_flags` silently -- callers should
    not pass it (see module docstring); this function does not itself
    filter the flag list because the flags are project-controlled config,
    not user input, but the omission is load-bearing and documented here
    for anyone editing configs/sumo.yaml.
    """
    home = sumo_home or resolve_sumo_home()
    Path(net_out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sumo_bin("netconvert", home),
        "--osm-files",
        str(osm_xml_path),
        "-o",
        str(net_out_path),
        *extra_flags,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=_subprocess_env(home), timeout=600
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"netconvert failed (exit {proc.returncode}):\n{proc.stderr}"
        )
    warnings = parse_netconvert_warnings(proc.stderr)
    warnings["returncode"] = proc.returncode
    return warnings


# ---------------------------------------------------------------------------
# 2b.2: demand (randomTrips.py -> duarouter, via --route-file/--validate)
# ---------------------------------------------------------------------------


def generate_demand(
    net_path: str | Path,
    out_dir: str | Path,
    level: str,
    period: float,
    seed: int,
    demand_end: float,
    python_exe: str | None = None,
    sumo_home: str | None = None,
) -> dict[str, Path]:
    """Run randomTrips.py with --route-file (which internally invokes
    duarouter and --validate to keep only routable trips) at a given
    --period, producing trips_{level}.trips.xml and routes_{level}.rou.xml
    under out_dir. Reproducible given `seed`.
    """
    home = sumo_home or resolve_sumo_home()
    py = python_exe or sys.executable
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trips_path = out_dir / f"trips_{level}.trips.xml"
    routes_path = out_dir / f"routes_{level}.rou.xml"

    cmd = [
        py,
        sumo_tool("randomTrips.py", home),
        "-n",
        str(net_path),
        "-o",
        str(trips_path),
        "-r",
        str(routes_path),
        "-b",
        "0",
        "-e",
        str(demand_end),
        "--period",
        str(period),
        "--seed",
        str(seed),
        "--validate",
        "--fringe-factor",
        "5",
        "--vehicle-class",
        "passenger",
        "--prefix",
        level,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=_subprocess_env(home), timeout=600
    )
    if proc.returncode != 0 or not routes_path.exists():
        raise RuntimeError(
            f"randomTrips.py failed for level={level} (exit {proc.returncode}):\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
    return {"trips": trips_path, "routes": routes_path}


def write_edgedata_additional(level: str, out_dir: str | Path, freq: float) -> Path:
    """Write the <additional><edgeData .../></additional> file `sumo`
    consumes. The `file=` attribute inside is intentionally a bare filename
    (not a path) because sumo resolves it relative to the additional
    file's own directory, so a path prefix here would be joined twice."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    add_path = out_dir / f"edgedata_{level}.add.xml"
    edgedata_filename = f"edgedata_{level}.xml"
    add_path.write_text(
        "<additional>\n"
        f'    <edgeData id="edgeData_{level}" file="{edgedata_filename}" freq="{freq}"/>\n'
        "</additional>\n",
        encoding="utf-8",
    )
    return add_path


def run_simulation(
    net_path: str | Path,
    route_path: str | Path,
    additional_path: str | Path,
    begin: float,
    end: float,
    seed: int,
    tripinfo_path: str | Path,
    sumo_home: str | None = None,
) -> dict[str, Any]:
    """Run headless sumo. Returns parsed vehicle-count / teleport
    statistics from stdout (sumo's own --duration-log.statistics report)."""
    home = sumo_home or resolve_sumo_home()
    cmd = [
        sumo_bin("sumo", home),
        "--net-file",
        str(net_path),
        "--route-files",
        str(route_path),
        "--additional-files",
        str(additional_path),
        "--begin",
        str(begin),
        "--end",
        str(end),
        "--seed",
        str(seed),
        "--no-step-log",
        "--duration-log.statistics",
        "--tripinfo-output",
        str(tripinfo_path),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=_subprocess_env(home), timeout=1200
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sumo simulation failed (exit {proc.returncode}):\n{proc.stderr}")
    return _parse_sumo_stats(proc.stdout + proc.stderr)


def _parse_sumo_stats(text: str) -> dict[str, Any]:
    """Pull the vehicle census out of sumo's own end-of-run report.

    PHASE 2D widened this. Teleports are the calibration signal that
    matters -- a teleport is a vehicle that JUMPED past a blockage instead
    of driving through it, so every edge it skipped gets a travel time that
    never happened -- and the raw total hid which kind. The per-category
    breakdown ("Jam", "Yield", "Wrong Lane") is what distinguishes genuine
    gridlock from a handful of lane-choice artefacts, so it is parsed out
    rather than left in the log.

    ``mean_speed_mps``/``mean_duration_s``/... come from the
    ``Statistics (avg of N)`` block, which ``--duration-log.statistics``
    prints and which covers COMPLETED trips only -- hence
    ``n_trips_completed``, the gap to ``inserted`` being the vehicles still
    circulating when the clock ran out.
    """
    stats: dict[str, Any] = {}
    m = re.search(r"Inserted:\s*(\d+)(?:\s*\(Loaded:\s*(\d+)\))?", text)
    if m:
        stats["inserted"] = int(m.group(1))
        stats["loaded"] = int(m.group(2)) if m.group(2) else int(m.group(1))
    m = re.search(r"Running:\s*(\d+)", text)
    if m:
        stats["running_at_end"] = int(m.group(1))
    m = re.search(r"Waiting:\s*(\d+)", text)
    if m:
        stats["waiting_at_end"] = int(m.group(1))
    m = re.search(r"Teleports:\s*(\d+)\s*(?:\(([^)]*)\))?", text)
    if m:
        stats["teleports"] = int(m.group(1))
        categories: dict[str, int] = {}
        if m.group(2):
            for part in m.group(2).split(","):
                if ":" in part:
                    key, value = part.split(":", 1)
                    try:
                        categories[key.strip()] = int(value.strip())
                    except ValueError:
                        continue
        stats["teleport_categories"] = categories

    if stats.get("inserted"):
        stats["teleport_pct_of_inserted"] = 100.0 * stats.get("teleports", 0) / stats["inserted"]
        stats["running_pct_of_inserted"] = (
            100.0 * stats.get("running_at_end", 0) / stats["inserted"]
        )

    # The `Statistics (avg of N)` block. Anchored on that header rather than
    # searched globally, because sumo ALSO prints a bare `Duration:` in its
    # `Performance:` block -- that one is wall-clock seconds, and a plain
    # `re.search` picks it up first and silently reports it as the mean trip
    # duration.
    m = re.search(r"Statistics \(avg of (\d+)\):(.*?)(?:\n\S|\Z)", text, re.S)
    if m:
        stats["n_trips_completed"] = int(m.group(1))
        block = m.group(2)
        for key, label in (
            ("mean_route_length_m", "RouteLength"),
            ("mean_speed_mps", "Speed"),
            ("mean_duration_s", "Duration"),
            ("mean_waiting_time_s", "WaitingTime"),
            ("mean_timeloss_s", "TimeLoss"),
            ("mean_depart_delay_s", "DepartDelay"),
        ):
            mm = re.search(rf"{label}:\s*([0-9.]+)", block)
            if mm:
                stats[key] = float(mm.group(1))
    return stats


# ---------------------------------------------------------------------------
# 2c: parse edgedata, clip implausible values, aggregate to one value per
# SUMO edge across the whole simulation.
# ---------------------------------------------------------------------------


def _clip_implausible_traveltime(tt: float, freeflow_tt: float, cap_factor: float) -> float:
    """SUMO reports very large per-interval traveltime for an edge where a
    vehicle sat through a jam or a teleport wait (observed values up to
    100000s on this network's heavy-demand run, on edges whose
    length/speed-limit implies a free-flow traversal of a few seconds).
    Passing that raw into the length-weighted mean would let a single
    stalled vehicle dominate an edge's whole-simulation average and
    corrupt anything downstream that assumes travel time scales with
    congestion, not with "a car happened to be stuck here". Clipping
    (rather than discarding) preserves the direction of the congestion
    signal -- a genuinely jammed edge still ends up much slower than
    free-flow, just not implausibly so -- and is applied uniformly rather
    than singling out specific edges."""
    cap = max(freeflow_tt * cap_factor, 1.0)
    return min(tt, cap)


def parse_edgedata(
    path: str | Path,
    freeflow_tt_by_sumo_id: dict[str, float],
    cap_factor: float,
) -> tuple[dict[str, float], dict[str, float], dict[str, Any]]:
    """Parse one data/sumo/edgedata_{level}.xml file. Returns:
      - traveltime: sumo_edge_id -> sampledSeconds-weighted mean traveltime
        across all intervals (clipped per-interval before weighting),
        for edges with at least one observation.
      - sampled_seconds: sumo_edge_id -> total sampledSeconds observed
        (used for zero-traffic-fraction reporting).
      - report: {n_edges_total, n_edges_with_traffic, frac_zero_traffic,
        n_intervals}, plus the PHASE 2D clipping census
        (n_interval_samples, n_samples_clipped, frac_samples_clipped,
        max_sample_ratio_to_freeflow) so it is always visible whether
        `implausible_cap_factor` is actually doing anything on this run
        rather than silently being a no-op.
    """
    tree = ET.parse(str(path))
    root = tree.getroot()

    weighted_sum: dict[str, float] = {}
    weight_total: dict[str, float] = {}
    sampled_seconds: dict[str, float] = {}
    all_ids: set[str] = set()
    n_intervals = 0
    n_samples = 0
    n_clipped = 0
    max_ratio = 0.0

    for interval in root.findall("interval"):
        n_intervals += 1
        for edge_el in interval.findall("edge"):
            sid = edge_el.get("id")
            all_ids.add(sid)
            ss = float(edge_el.get("sampledSeconds", "0.0"))
            sampled_seconds[sid] = sampled_seconds.get(sid, 0.0) + ss
            tt_raw = edge_el.get("traveltime")
            if tt_raw is None or ss <= 0:
                continue
            tt = float(tt_raw)
            ff = freeflow_tt_by_sumo_id.get(sid, tt)
            n_samples += 1
            if ff > 0:
                max_ratio = max(max_ratio, tt / ff)
            clipped = _clip_implausible_traveltime(tt, ff, cap_factor)
            if clipped < tt:
                n_clipped += 1
            tt = clipped
            weighted_sum[sid] = weighted_sum.get(sid, 0.0) + tt * ss
            weight_total[sid] = weight_total.get(sid, 0.0) + ss

    traveltime = {sid: weighted_sum[sid] / weight_total[sid] for sid in weighted_sum}

    n_edges_total = len(all_ids)
    n_with_traffic = sum(1 for sid in all_ids if sampled_seconds.get(sid, 0.0) > 0)
    report = {
        "n_edges_total": n_edges_total,
        "n_edges_with_traffic": n_with_traffic,
        "n_edges_zero_traffic": n_edges_total - n_with_traffic,
        "frac_zero_traffic": (
            (n_edges_total - n_with_traffic) / n_edges_total if n_edges_total else 1.0
        ),
        "n_intervals": n_intervals,
        # Phase 2d clipping census. `frac_samples_clipped == 0` means the
        # `implausible_cap_factor` guard fired on nothing at all for this
        # run -- worth knowing, and worth KEEPING the guard for anyway (it
        # costs one `min` per sample and is the only thing standing between
        # a future gridlocked run and a corrupted edge mean).
        "n_interval_samples": n_samples,
        "n_samples_clipped": n_clipped,
        "frac_samples_clipped": (n_clipped / n_samples) if n_samples else 0.0,
        "max_sample_ratio_to_freeflow": max_ratio,
        "cap_factor": cap_factor,
    }
    return traveltime, sampled_seconds, report


def _load_yaml(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _canonical_hash(d: dict[str, Any]) -> str:
    canonical = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


class SUMOProvider(CongestionProvider):
    """CongestionProvider backed by parsed SUMO edgedata, joined to graph
    edges via network/ids.py. Implements exactly the same interface as
    BPRProvider/FreeFlowProvider: algorithms cannot tell which one they are
    talking to.
    """

    def __init__(
        self,
        G: nx.MultiDiGraph,
        area_cfg: AreaConfig,
        sumo_cfg_path: str | Path = "configs/sumo.yaml",
        net_path: str | Path = "data/sumo/net.net.xml",
        edgedata_paths: dict[str, str | Path] | None = None,
        cache_dir: str | Path | None = None,
        sumolib_module=None,
    ):
        self._G = G
        self._area_cfg = area_cfg
        self._raw_cfg = _load_yaml(sumo_cfg_path)
        self._net_path = Path(net_path)
        self._edgedata_paths = {
            level: Path(p)
            for level, p in (
                edgedata_paths
                or {
                    "light": "data/sumo/edgedata_light.xml",
                    "heavy": "data/sumo/edgedata_heavy.xml",
                }
            ).items()
        }
        self._area_hash = config_hash(area_cfg)
        # PHASE 2D: the graph's free-flow BASELINE is folded into the cache
        # key. Unmatched graph edges fall back to the graph's own
        # `travel_time` (see network/ids.compute_edge_travel_times), so a
        # SUMO snapshot is not independent of which baseline graph it was
        # built on -- and both baseline graphs share the same AREA hash.
        self._sumo_cfg_hash = _canonical_hash(
            {
                **self._raw_cfg,
                "_aggregation_version": _AGGREGATION_VERSION,
                "_baseline": baseline_of(G),
                "_baseline_hash": baseline_hash_of(G),
            }
        )
        self._cache_dir = Path(cache_dir) if cache_dir is not None else Path(
            self._raw_cfg.get("cache_dir", "data/results/congestion_cache")
        )

        if sumolib_module is None:
            home = resolve_sumo_home()
            tools_path = str(Path(home) / "tools")
            if tools_path not in sys.path:
                sys.path.insert(0, tools_path)
            import sumolib as sumolib_module  # noqa: PLC0415

        self._net = sumolib_module.net.readNet(str(self._net_path))
        self._sumo_edges: list[ParsedSumoEdge] = load_sumo_edges(
            str(self._net_path), sumolib_module=sumolib_module
        )

        self._sumo_length = {se.sumo_id: se.length_m for se in self._sumo_edges}
        self._freeflow_tt = {
            se.sumo_id: se.length_m / max(self._net.getEdge(se.sumo_id).getSpeed(), 0.1)
            for se in self._sumo_edges
        }

        self._match = match_sumo_edges_to_graph(G, self._sumo_edges)
        cap_factor = float(self._raw_cfg.get("implausible_cap_factor", 20.0))

        self._parsed_by_level: dict[str, tuple[dict[str, float], dict[str, float], dict]] = {}
        self._coverage_by_level: dict[str, dict] = {}
        for level, path in self._edgedata_paths.items():
            if path.exists():
                self._parsed_by_level[level] = parse_edgedata(
                    path, self._freeflow_tt, cap_factor
                )

        self._n_fallback_by_level: dict[str, int] = {}
        self._travel_time_by_level: dict[str, dict[EdgeKey, float]] = {}
        for level, (traveltime, _sampled, _report) in self._parsed_by_level.items():
            tt_map, n_fallback = compute_edge_travel_times(
                G, self._match, traveltime, self._sumo_length
            )
            self._travel_time_by_level[level] = tt_map
            self._n_fallback_by_level[level] = n_fallback

        # Geometric fallback is applied once (per net/graph, not per level)
        # if ID-based coverage is below 85%; recorded flags for reporting.
        self._id_coverage = coverage_report(G, self._match, n_fallback=0)
        self._geometric_fallback_applied = False
        self._id_only_coverage_pct = self._id_coverage["coverage_pct"]
        if self._id_coverage["coverage_pct"] < 85.0:
            n_new = geometric_fallback_match(
                G, self._sumo_edges, self._net, self._match
            )
            self._geometric_fallback_applied = True
            self._n_geometric_newly_matched = n_new
            # Recompute per-level travel times now that more edges are matched.
            for level, (traveltime, _sampled, _report) in self._parsed_by_level.items():
                tt_map, n_fallback = compute_edge_travel_times(
                    G, self._match, traveltime, self._sumo_length
                )
                self._travel_time_by_level[level] = tt_map
                self._n_fallback_by_level[level] = n_fallback
        else:
            self._n_geometric_newly_matched = 0

        # n_fallback (edges with no traveltime observation) is level-specific
        # (an edge may see traffic in "heavy" but not "light"), so the
        # coverage report keeps them separately rather than picking one
        # level's number arbitrarily. n_unmatched_edges/coverage_pct, by
        # contrast, are level-independent -- ID matching does not depend on
        # which demand level's edgedata is being read.
        self._final_coverage = coverage_report(G, self._match, n_fallback=0)
        self._final_coverage["n_fallback_no_traveltime_observation_by_level"] = dict(
            self._n_fallback_by_level
        )
        del self._final_coverage["n_fallback_no_traveltime_observation"]

    # -- CongestionProvider interface ------------------------------------

    @property
    def levels(self) -> tuple[str, ...]:
        return tuple(self._parsed_by_level.keys())

    @property
    def n_time_bins(self) -> int:
        return 1

    def snapshot(self, level: str, time_bin: int = 0) -> TrafficSnapshot:
        if level not in self._travel_time_by_level:
            raise ValueError(
                f"Unknown SUMO level {level!r}; expected one of "
                f"{tuple(self._travel_time_by_level.keys())}"
            )
        if time_bin != 0:
            raise ValueError("SUMOProvider aggregates over the whole simulation: time_bin must be 0")

        cache_path = self._cache_path(level)
        cached = self._load_cache(cache_path)
        if cached is not None:
            return cached

        travel_time = self._travel_time_by_level[level]
        report = self._parsed_by_level[level][2]
        snap = TrafficSnapshot(
            level=level,
            time_bin=time_bin,
            source="sumo",
            travel_time=dict(travel_time),
            meta={
                "area_config_hash": self._area_hash,
                "sumo_config_hash": self._sumo_cfg_hash,
                "n_fallback_edges": self._n_fallback_by_level[level],
                "coverage_pct": self._final_coverage["coverage_pct"],
                "frac_sumo_edges_zero_traffic": report["frac_zero_traffic"],
                "geometric_fallback_applied": self._geometric_fallback_applied,
                **baseline_meta(self._G),
            },
        )
        self._save_cache(cache_path, snap)
        return snap

    # -- Reporting helpers -------------------------------------------------

    def coverage_report(self) -> dict:
        return dict(self._final_coverage)

    def id_only_coverage_pct(self) -> float:
        return self._id_only_coverage_pct

    def edgedata_report(self, level: str) -> dict:
        return dict(self._parsed_by_level[level][2])

    # -- Disk cache ---------------------------------------------------------

    def _cache_path(self, level: str) -> Path:
        name = f"sumo_{self._area_hash}_{self._sumo_cfg_hash}_{level}_bin0.json"
        return self._cache_dir / name

    # Phase 4d: identical treatment to BPRProvider -- this provider had the
    # SAME shared-temp-file cache-write race (`path.with_suffix('.json.tmp')`
    # is a pure function of the cache key, so concurrent writers collided and
    # `replace` raised PermissionError on Windows). Both now delegate to the
    # concurrency-safe primitives in congestion/base.py.

    @staticmethod
    def _load_cache(path: Path) -> TrafficSnapshot | None:
        """None means 'no usable cache entry': absent, partial or corrupt."""
        return load_snapshot_cache(path)

    @staticmethod
    def _save_cache(path: Path, snap: TrafficSnapshot) -> None:
        save_snapshot_cache(path, snap)
