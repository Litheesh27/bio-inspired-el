# bioroute

A three-way benchmark of Dijkstra/A*, Ant Colony Optimization, and a
Physarum polycephalum tube-reinforcement model, routing on the real
Central Bengaluru road network under SUMO-simulated congestion. It
measures both routing quality and recovery speed after simulated road
closures.

See `CLAUDE.md` for the full phase plan, locked design decisions, and
project invariants.

## Setup

1. Create the Python 3.10 virtual environment (Python 3.10 specifically —
   `osmnx`/`geopandas`/`shapely` wheels are unreliable on newer versions):

   ```
   py -3.10 -m venv .venv
   ```

2. Install dependencies and the package itself (editable):

   ```
   .venv/Scripts/python.exe -m pip install -r requirements.txt
   .venv/Scripts/python.exe -m pip install -e .
   ```

3. Install [Eclipse SUMO](https://www.eclipse.org/sumo/) for Windows
   (manual step — the installer needs admin rights and a GUI). After
   installing, set the `SUMO_HOME` environment variable (typically
   `C:\Program Files (x86)\Eclipse\Sumo`) and restart your terminal.
   `sumolib`/`traci` are not pip packages; they ship with SUMO and are
   imported by adding `$SUMO_HOME/tools` to `sys.path`.

4. Run the setup check:

   ```
   .venv/Scripts/python.exe scripts/00_setup_check.py
   ```

   This prints a PASS/FAIL line for every environment requirement (Python
   version, each dependency, the `bioroute` package, SUMO_HOME, the
   `netconvert`/`sumo` binaries, `sumolib`, and writable data directories)
   and exits non-zero if anything is missing. Until SUMO is installed, the
   SUMO-related checks are expected to fail — everything else should pass.
