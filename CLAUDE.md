# Medio: notes for Claude Code

Acalanes Data Fest 2026 project: a live 3D replay of San Francisco 911 fire/EMS response with what-if scenarios.

## Layout

- `src/backend/`: Python stdlib only, no installs
  - `app.py`: HTTP server and API
  - `sim.py`: travel-time model, simulator, validation
  - `routing.py`: street routing over OpenStreetMap
  - `nl.py`: Claude Haiku turns typed text into a simulation plan (`plan_scenario`) and a checked headline (`narrate`); the engine is `simulate_plan` in `app.py`
  - `paths.py`: every file location
- `src/frontend/index.html`: the whole UI, one file (deck.gl 8.9 from unpkg)
- `scripts/`: one-off downloads (`fetch_data.py`, `fetch_buildings.py`)
- `data/sf/`: source datasets, committed
- `data/build/`: generated artifacts the server loads, committed so Render needs no build step
- `data/raw/`: large downloads, gitignored
- `docs/`: PRD and notes
- `config/.env.example`: key names. The real `.env` sits at the repo root, gitignored.

## Commands

```bash
python3 src/backend/app.py               # http://localhost:8000 (PORT env overrides)
python3 src/backend/sim.py               # validation table + sanity checks, writes data/build/validation.json
python3 src/backend/sim.py --export      # rebuild data/build/runtime.pkl.gz after model changes
python3 src/backend/app.py --precompute  # then rebuild data/build/scenarios.pkl.gz (clicked scenarios answer instantly on Render)
python3 src/backend/app.py --best-spots  # rebuild data/build/best_spots.json (~1 min)
python3 src/backend/routing.py --build   # rebuild data/build/routes.pkl.gz from data/raw/osm/roads.json
python3 src/backend/nl.py                # live Haiku self-test (costs ~$0.0003)
python3 scripts/fetch_data.py            # re-download DataSF datasets into data/sf/
python3 scripts/fetch_elevation.py       # rebuild data/sf/elevation.json (ground heights for Google 3D mode)
```

## Rules

- Commit and push after each working change; a teammate follows the repo live.
- Never commit `.env` or print keys.
- On this Mac, python.org Python can't verify TLS certificates: downloads use `curl`, and git or network commands need the sandbox disabled.
- Deploy: Render Blueprint from `render.yaml`. Every push to `main` redeploys. Set `ANTHROPIC_API_KEY` in Render's environment for the Ask box.
