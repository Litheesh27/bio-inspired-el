"""Phase 0 environment setup check.

Verifies the development environment is correctly configured before any
later phase begins. Prints a PASS/FAIL line for every check and exits 0
only if all checks pass; exits 1 otherwise with an actionable summary of
what is missing and how to fix it.

Run with the project's own venv interpreter:
    .venv/Scripts/python.exe scripts/00_setup_check.py
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIREMENTS = [
    "osmnx",
    "networkx",
    "numpy",
    "scipy",
    "pandas",
    "pyarrow",
    "matplotlib",
    "yaml",  # pyyaml's import name
    "joblib",
    "tqdm",
    "requests",
    "pytest",
]

# Map import name -> distribution name (for version lookup), where they differ.
DIST_NAME = {"yaml": "pyyaml"}

results: list[tuple[bool, str]] = []


def record(ok: bool, message: str) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {message}")
    results.append((ok, message))


def check_python_version() -> None:
    major, minor = sys.version_info[:2]
    ok = (major, minor) == (3, 10)
    record(ok, f"Python version is 3.10.x (found {sys.version.split()[0]})")


def check_requirements() -> None:
    for import_name in REQUIREMENTS:
        dist_name = DIST_NAME.get(import_name, import_name)
        try:
            module = importlib.import_module(import_name)
            try:
                version = importlib.metadata.version(dist_name)
            except importlib.metadata.PackageNotFoundError:
                version = getattr(module, "__version__", "unknown")
            record(True, f"import {import_name} OK (version {version})")
        except ImportError as exc:
            record(False, f"import {import_name} FAILED ({exc})")


def check_bioroute_import() -> None:
    try:
        import bioroute  # noqa: F401

        record(True, f"import bioroute OK (version {bioroute.__version__})")
    except ImportError as exc:
        record(
            False,
            f"import bioroute FAILED ({exc}). "
            f"Run: .venv/Scripts/python.exe -m pip install -e .",
        )


def check_sumo_home() -> str | None:
    sumo_home = os.environ.get("SUMO_HOME")
    if not sumo_home:
        record(
            False,
            "SUMO_HOME env var is set (it is NOT set). "
            "Install Eclipse SUMO for Windows "
            "(https://www.eclipse.org/sumo/ -> Downloads -> Windows installer), "
            "then set SUMO_HOME (typically 'C:\\Program Files (x86)\\Eclipse\\Sumo') "
            "and restart the terminal.",
        )
        return None
    path = Path(sumo_home)
    if not path.exists():
        record(
            False,
            f"SUMO_HOME path exists (SUMO_HOME={sumo_home!r} does not exist on disk). "
            "Re-check the SUMO_HOME env var against your actual SUMO install location.",
        )
        return None
    record(True, f"SUMO_HOME is set and exists ({sumo_home})")
    return sumo_home


def _resolve_on_path_or_sumo_home(binary: str, sumo_home: str | None) -> str | None:
    found = shutil.which(binary)
    if found:
        return found
    if sumo_home:
        candidate = Path(sumo_home) / "bin" / f"{binary}.exe"
        if candidate.exists():
            return str(candidate)
    return None


def check_sumo_binaries(sumo_home: str | None) -> None:
    for binary in ("netconvert", "sumo"):
        resolved = _resolve_on_path_or_sumo_home(binary, sumo_home)
        if not resolved:
            record(
                False,
                f"{binary} found on PATH or under $SUMO_HOME/bin (NOT FOUND). "
                "Install Eclipse SUMO for Windows and ensure SUMO_HOME is set, "
                "or add $SUMO_HOME/bin to PATH.",
            )
            continue
        try:
            proc = subprocess.run(
                [resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            first_line = (proc.stdout or proc.stderr).strip().splitlines()[0] if (
                proc.stdout or proc.stderr
            ) else "unknown version"
            record(True, f"{binary} resolved at {resolved} ({first_line})")
        except Exception as exc:  # noqa: BLE001
            record(False, f"{binary} resolved at {resolved} but --version failed ({exc})")


def check_sumo_tools(sumo_home: str | None) -> None:
    if not sumo_home:
        record(
            False,
            "$SUMO_HOME/tools exists and sumolib imports "
            "(SKIPPED: SUMO_HOME is not set).",
        )
        return
    tools_path = Path(sumo_home) / "tools"
    if not tools_path.exists():
        record(
            False,
            f"$SUMO_HOME/tools exists ({tools_path} not found). "
            "Check the SUMO installation is complete.",
        )
        return
    sys.path.insert(0, str(tools_path))
    try:
        import sumolib  # noqa: F401

        record(True, f"sumolib imports OK after adding {tools_path} to sys.path")
    except ImportError as exc:
        record(
            False,
            f"sumolib imports after adding $SUMO_HOME/tools to sys.path "
            f"(FAILED: {exc})",
        )


def check_data_dirs() -> None:
    for sub in ("raw", "network", "sumo", "results"):
        d = REPO_ROOT / "data" / sub
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".setup_check_tmp"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            record(True, f"data/{sub}/ exists and is writable")
        except OSError as exc:
            record(False, f"data/{sub}/ exists and is writable (FAILED: {exc})")


def main() -> int:
    print("=== bioroute Phase 0 setup check ===\n")

    check_python_version()
    check_requirements()
    check_bioroute_import()
    sumo_home = check_sumo_home()
    check_sumo_binaries(sumo_home)
    check_sumo_tools(sumo_home)
    check_data_dirs()

    print()
    n_pass = sum(1 for ok, _ in results if ok)
    n_total = len(results)
    print(f"=== {n_pass}/{n_total} checks passed ===")

    if n_pass == n_total:
        print("All checks passed.")
        return 0

    print("\nFailed checks:")
    for ok, message in results:
        if not ok:
            print(f"  - {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
