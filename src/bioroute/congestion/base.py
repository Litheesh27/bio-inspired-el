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

PHASE 2D -- the shared free-flow baseline. "Free-flow" used to mean two
different things: the graph's `travel_time` came from the conservative
Bengaluru speeds in configs/area_central_blr.yaml, while SUMO used its own
faster OSM/netconvert speeds, so SUMO *congested* times could land below
the graph's *free-flow* times and no ratio was comparable across providers.
network/graph.py now defines a second baseline graph re-annotated from the
SUMO net, and `load_baseline_graph` selects between them. Every provider
here stamps `baseline`/`baseline_hash` into `TrafficSnapshot.meta` (via
`network.graph.baseline_meta`) so a result row always records which
denominator produced it -- and BPRProvider/SUMOProvider additionally fold
the baseline into their snapshot CACHE KEY, because the two graphs share an
area-config hash while carrying different travel times on every edge.

PHASE 4D ADDITION -- concurrency-safe snapshot caching. This module also
owns the on-disk snapshot cache primitives shared by BPRProvider and
SUMOProvider (`write_json_atomic`, `read_json_cache`) and the serial
pre-warm helper (`prewarm_snapshots`).

The race they fix: both providers used to write their cache via
`path.with_suffix(".json.tmp")` -- a temp name derived *deterministically*
from the cache key, so every concurrent writer of the same snapshot used
the SAME temp file. Under joblib parallelism (Phase 4c's 30-pair run, 12
workers, cold cache) this produced two distinct failures:

  1. Two writers interleave in one temp file: writer B truncates it while
     writer A is mid-`json.dump`, so whatever lands at the final path can
     be a spliced/truncated document.
  2. `tmp.replace(path)` raises `PermissionError` on Windows. POSIX
     tolerates replacing a destination that another process has open for
     reading; Windows does not -- `MoveFileEx(REPLACE_EXISTING)` fails
     with ERROR_ACCESS_DENIED / ERROR_SHARING_VIOLATION while any handle
     is open on either the source temp file or the destination. A reader
     hitting the cache at the wrong moment, or a second writer still
     holding the shared temp file, is enough. That is exactly what killed
     Phase 4c's run halfway.

The fix has three parts, all of them required:
  * a UNIQUE temp file per writer (pid + thread id + random suffix), so
    writers can never interleave in one file;
  * `os.replace` with bounded exponential-backoff retry on
    `PermissionError`/`OSError`, plus a "did another writer already put a
    valid file there?" check before giving up -- concurrent writers of a
    deterministic snapshot are producing identical bytes, so losing the
    race is success, not failure;
  * fsync before replace, and VALIDATION on read: a cache file that is
    missing, unparseable, or structurally incomplete is treated as absent
    and recomputed, never silently half-used.

`prewarm_snapshots` is the belt-and-braces half: Phase 8 calls it once,
serially, before any parallel section, so the parallel workers only ever
READ the cache. Concurrent readers of a fully-written file are safe on
every platform; it is the concurrent *writers* that are hard.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable, Sequence

import networkx as nx

from bioroute.congestion.snapshot import TrafficSnapshot
from bioroute.network.graph import baseline_meta


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
            # PHASE 2D: every snapshot records WHICH free-flow baseline the
            # graph it came from carries, so a result row is never ambiguous
            # about the denominator its ratios were computed against.
            meta={"n_edges": len(travel_time), **baseline_meta(self._G)},
        )


# ==========================================================================
# Phase 4d: concurrency-safe on-disk snapshot cache
# ==========================================================================
#
# Shared by BPRProvider and SUMOProvider. See the module docstring for the
# race these exist to close.

# Bounded retry schedule for the atomic replace. Windows can hold a handle
# on the destination (a concurrent reader) or on our temp file (an AV
# scanner) for a short, unpredictable window; 12 attempts with exponential
# backoff capped at 0.5 s covers ~3 s total, which is far longer than any
# hold observed here.
CACHE_REPLACE_RETRIES = 12
CACHE_RETRY_BASE_DELAY_S = 0.01
CACHE_RETRY_MAX_DELAY_S = 0.5

# Reads get a much shorter budget: the only retryable read failure is the
# brief window during another process's `os.replace`.
CACHE_READ_RETRIES = 6
CACHE_READ_BASE_DELAY_S = 0.005
CACHE_READ_MAX_DELAY_S = 0.1

# Structural keys every cached snapshot document must carry. A file missing
# any of them is treated as corrupt (= absent) rather than partially used.
_REQUIRED_SNAPSHOT_KEYS = ("level", "time_bin", "source", "travel_time", "meta")

# Jitter source for the retry backoff. Deliberately a PRIVATE Random
# instance rather than the `random` module functions: CLAUDE.md requires
# that nothing experimental depends on unseeded RNG, and drawing from the
# global stream here would perturb any caller that seeded it.
_RETRY_JITTER = random.Random()


def _unique_tmp_path(path: Path) -> Path:
    """A temp path unique to this writer.

    The whole Phase 4c crash traces back to the previous
    `path.with_suffix('.json.tmp')`, which is a pure function of the cache
    key and therefore IDENTICAL across concurrent writers of the same
    snapshot. pid + thread id + 8 random hex digits makes a collision
    impossible in practice, even across machines sharing a cache dir."""
    unique = f"{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    return path.with_name(f"{path.name}.{unique}.tmp")


def _is_sharing_violation(exc: OSError) -> bool:
    """True for the errors that mean 'someone has a handle open on this
    file', which is the retryable case."""
    winerror = getattr(exc, "winerror", None)
    # 5 = ERROR_ACCESS_DENIED, 32 = ERROR_SHARING_VIOLATION,
    # 33 = ERROR_LOCK_VIOLATION.
    return isinstance(exc, PermissionError) or winerror in (5, 32, 33)


def write_json_atomic(
    path: str | Path,
    payload: dict[str, Any],
    *,
    retries: int = CACHE_REPLACE_RETRIES,
) -> bool:
    """Write `payload` as JSON to `path` so that concurrent writers and
    readers never observe a partial file.

    Returns True if this call's bytes landed at `path`, False if another
    writer won the race and a valid file was already there. Losing the race
    is a normal, successful outcome: cached snapshots are deterministic
    given their cache key, so every concurrent writer produces the same
    document.

    Raises OSError only if the replace could not be completed AND no valid
    file exists at `path` afterwards.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp_path(path)
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f)
            f.flush()
            # fsync before the replace: without it a crash between replace
            # and writeback can leave a zero-length file at the final path,
            # which is exactly the "silently reused corrupt cache" failure.
            os.fsync(f.fileno())

        last_exc: OSError | None = None
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return True
            except OSError as exc:
                if not _is_sharing_violation(exc):
                    raise
                last_exc = exc
            # Someone else may have completed the same write while we were
            # blocked. If a VALID file is now there, we are done.
            # retries=1: this loop already has its own backoff, so the read
            # must not nest a second one.
            if read_json_cache(path, retries=1) is not None:
                return False
            delay = min(CACHE_RETRY_BASE_DELAY_S * (2**attempt), CACHE_RETRY_MAX_DELAY_S)
            time.sleep(delay * (0.5 + _RETRY_JITTER.random()))

        if read_json_cache(path) is not None:
            return False
        raise OSError(
            f"Could not atomically replace {path} after {retries} attempts and "
            f"no valid cache file is present: {last_exc!r}"
        ) from last_exc
    finally:
        # Never leak temp files, whatever happened above.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def read_json_cache(
    path: str | Path, *, retries: int = CACHE_READ_RETRIES
) -> dict[str, Any] | None:
    """Read a cached snapshot document, returning None if the file is
    absent, unreadable, unparseable, or structurally incomplete.

    Every failure mode collapses to None (= "recompute it") rather than an
    exception: a corrupt cache entry must never take down a sweep, and must
    never be partially believed.

    The one case that is RETRIED rather than reported as a miss is the
    Windows sharing-violation window: while another process is running
    `os.replace` onto this path, opening it raises PermissionError even
    though a perfectly good file is there. Measured in the concurrent-
    process test, that window is hit a couple of times per hundred reads
    under 10-way contention. Returning None there would be *safe* but
    wasteful -- it would send a Phase 8 worker off to recompute a
    10-increment BPR assignment for nothing -- so it gets a short bounded
    backoff. A file that genuinely does not exist returns None at once; a
    cold cache must not pay the retry budget."""
    path = Path(path)
    for attempt in range(max(1, retries)):
        try:
            with path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return None
        except OSError as exc:
            if not _is_sharing_violation(exc) or attempt == retries - 1:
                return None
            delay = min(CACHE_READ_BASE_DELAY_S * (2**attempt), CACHE_READ_MAX_DELAY_S)
            time.sleep(delay * (0.5 + _RETRY_JITTER.random()))
            continue
        if not _validate_snapshot_raw(raw):
            return None
        return raw
    return None


def _validate_snapshot_raw(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    if any(key not in raw for key in _REQUIRED_SNAPSHOT_KEYS):
        return False
    tt = raw["travel_time"]
    if not isinstance(tt, dict) or not tt:
        return False
    if not isinstance(raw["meta"], dict):
        return False
    return True


def snapshot_to_raw(snap: TrafficSnapshot) -> dict[str, Any]:
    """Serializable form of a TrafficSnapshot (the on-disk cache document)."""
    return {
        "level": snap.level,
        "time_bin": snap.time_bin,
        "source": snap.source,
        "meta": snap.meta,
        "travel_time": {f"{u}_{v}_{k}": val for (u, v, k), val in snap.travel_time.items()},
    }


def snapshot_from_raw(raw: dict[str, Any]) -> TrafficSnapshot | None:
    """Inverse of `snapshot_to_raw`. Returns None if any edge key or value
    fails to parse -- a structurally-valid JSON document can still hold
    garbage, and that is still a corrupt cache entry."""
    travel_time: dict[tuple[int, int, int], float] = {}
    try:
        for key, val in raw["travel_time"].items():
            parts = key.split("_")
            if len(parts) != 3:
                return None
            travel_time[(int(parts[0]), int(parts[1]), int(parts[2]))] = float(val)
    except (TypeError, ValueError, AttributeError):
        return None
    if not travel_time:
        return None
    try:
        return TrafficSnapshot(
            level=raw["level"],
            time_bin=raw["time_bin"],
            source=raw["source"],
            travel_time=travel_time,
            meta=raw["meta"],
        )
    except (ValueError, TypeError):
        # TrafficSnapshot's own validation (positive, finite travel times)
        # rejected the file -- corrupt, so recompute.
        return None


def save_snapshot_cache(path: str | Path, snap: TrafficSnapshot) -> bool:
    """Concurrency-safe cache write. Returns True if this writer's bytes
    landed, False if an equivalent file was already there."""
    return write_json_atomic(path, snapshot_to_raw(snap))


def load_snapshot_cache(path: str | Path) -> TrafficSnapshot | None:
    """Concurrency-safe, validating cache read. None means 'no usable cache
    entry' -- absent, partial and corrupt are not distinguished by the
    caller, which recomputes either way."""
    raw = read_json_cache(path)
    if raw is None:
        return None
    return snapshot_from_raw(raw)


def prewarm_snapshots(
    provider: CongestionProvider,
    levels: Sequence[str] | None = None,
    time_bins: Iterable[int] | None = None,
    verbose: bool = False,
) -> dict[tuple[str, int], TrafficSnapshot]:
    """SERIALLY materialise every snapshot a parallel section will need.

    Call this once, from the parent process, BEFORE spawning workers
    (Phase 8's runner must do so). After it returns the cache files exist
    and the workers only ever READ them -- and concurrent readers of a
    complete file are safe on every platform. The concurrency-safe write
    path above is the backstop for anything that still races; this is the
    cheap fix that stops the race being exercised at all.

    Returns {(level, time_bin): TrafficSnapshot} so the caller can also use
    the in-memory objects directly."""
    if levels is None:
        levels = provider.levels
    if time_bins is None:
        time_bins = range(provider.n_time_bins)
    time_bins = list(time_bins)

    out: dict[tuple[str, int], TrafficSnapshot] = {}
    for level in levels:
        for time_bin in time_bins:
            if verbose:
                print(f"  pre-warming snapshot level={level} time_bin={time_bin} ...", flush=True)
            out[(level, time_bin)] = provider.snapshot(level, time_bin)
    return out
