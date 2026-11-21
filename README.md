# Runnr

Acalanes Data Fest 2026 project. A 3D "what if" simulator for San Francisco's 911 fire and EMS response, built from real 911 records.

**Live:** https://runnr-cbc8.onrender.com (free plan: the first load after idle takes ~50 s)

## The idea

When you call 911, the minutes until help arrives matter. Runnr replays real SF 911 calls in a simulation: each call goes to the nearest free unit, and that unit drives there, stays busy, then returns to its station. We check the simulation against real recorded arrival times, then ask questions like:

- Which neighborhoods wait longest today?
- What happens if Station X closes?
- Where would one extra ambulance help most?
- What breaks if call volume doubles, for example in a heat wave?

The results are shown on a 3D map of the city, with neighborhoods raised by how long help takes to arrive.

## Data

[SF Fire Department Calls for Service](https://data.sf.gov/Public-Safety/Fire-Department-and-Emergency-Medical-Services-Dis/nuek-vuh3) (DataSF), public, about 14k incidents a month. Each record has the timestamps (received, dispatched, en route, on scene, available), the unit, its station, the location, the neighborhood and the call type.

## How it works

1. **Travel times are learned from real trips**, by station, area and hour of day, so roads, hills and traffic are already included.
2. **Simulation:** real calls are replayed in order, using real dispatch delays and real time spent on scene.
3. **Validation:** a held-out month is replayed and simulated arrival times are compared with the real ones, against two simple baselines.
4. **Scenarios:** close a station, add a unit, or change call volume, and see the impact by neighborhood.

Full spec, MVP scope, validation targets, limits and demo script: [docs/PRD.md](docs/PRD.md).

## Run it

```bash
python3 src/backend/app.py      # http://localhost:8000
python3 src/backend/sim.py      # validation table
python3 scripts/fetch_data.py   # re-download DataSF data (already committed in data/sf/)
```

Python 3.10+, no extra packages. For the "Ask a what-if" box, copy `config/.env.example` to `.env` and add an Anthropic key. Deployed on Render from `render.yaml`.

## Layout

```
src/backend/    app.py (server) · sim.py (model + simulator) · routing.py (streets) · nl.py (Haiku) · paths.py
src/frontend/   index.html (3D map, deck.gl)
scripts/        dataset downloads
data/sf/        source data: calls, stations, neighborhoods, population, buildings
data/build/     generated: runtime model, routes, validation results
docs/           PRD
config/         .env.example
```

## Validation (held-out October 2026, 13,417 incidents)

| Model | Median error per call | Neighborhood r | Neighborhood error |
|---|---|---|---|
| Baseline: city median | 153 s | n/a | 28 s |
| Baseline: straight-line, one speed | 100 s | 0.72 | 32 s |
| **Runnr (learned travel + calibration)** | **90 s** | **0.92** | **16 s** |

Reproduce: `python3 src/backend/sim.py`. After changing the model, run `python3 src/backend/sim.py --export` to rebuild `data/build/runtime.pkl.gz`, the small file the server loads.

## Status

- [x] Data download and cleaning (300,081 incidents, Nov 2024 to Oct 2026)
- [x] Travel-time model and simulator
- [x] Validation numbers
- [x] Live 3D replay: satellite map, 3D buildings, 3D vehicles on real streets, what-if scenarios, Haiku Ask box (`src/frontend/index.html`)
- [x] Render deploy: https://runnr-cbc8.onrender.com (auto-deploys on push to `main`)
- [ ] Slides

The earlier idea, Runnr as a lab-protocol checker, is in git history (commit `fa7e4a4`).
