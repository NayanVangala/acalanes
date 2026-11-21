# Runnr

Acalanes Data Fest 2026 project. A 3D "what if" simulator for San Francisco's 911 fire and EMS response, built from real 911 records.

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

Full spec, MVP scope, validation targets, limits and demo script: [PRD.md](PRD.md).

## Run it

```bash
python3 fetch_data.py   # download calls (already committed as data/calls.csv.gz)
python3 app.py          # http://localhost:8000
```

Python 3.10+, no extra packages. Deployed on Render (`python3 app.py`, binds to `$PORT`).

## Status

- [ ] Data download and cleaning
- [ ] Travel-time model and simulator
- [ ] Validation numbers
- [ ] 3D map with scenario controls
- [ ] Render deploy
- [ ] Slides

The earlier idea, Runnr as a lab-protocol checker, is in git history (commit `fa7e4a4`).
