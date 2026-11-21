# Runnr

**When someone in San Francisco calls 911, how long until help arrives, which neighborhoods wait longest, and what would change if the city moved stations or ambulances?**

Runnr is a simulator of San Francisco's fire and EMS response, built from 300,081 real 911 incidents and tested against a month of real arrival times it never saw. You can close a station, add an ambulance or double the call volume, and see each neighborhood's wait change. The results are shown as a live 3D replay of a real day: engines, ambulances and police driving San Francisco's streets.

- **Live:** https://runnr-cbc8.onrender.com. It's on Render's free plan, so the first load after 15 idle minutes takes 30–50 s while the server wakes.
- **Event:** Acalanes Data Fest 2026, a student data science and AI hackathon (Nov 21, 2026, BRIDGEGOOD Studio, Oakland). Each team presents for 5 minutes.
- **Team:** Nayan Vangala, Arshdeep.

This README is the full project context: what it is, how every piece works, every number, every caveat, and why things were built the way they were. Someone reading only this file should understand the project as well as the people who built it.

---

## 1. The one-minute version

1. **Data.** SF publishes every fire and medical 911 call: which unit went, when it was dispatched, when it arrived and when it was free again. We use 2 years of it (Nov 2024 – Oct 2026, 300,081 incidents), plus real station, hospital and police locations, the street network, census population and building heights.
2. **Model.** From 333,000 real trips we learn how long it takes to drive from each station to each 500 m block of the city at each time of day. Roads, hills and traffic are already in those times, because the trips really happened.
3. **Simulator.** It replays every real call in order. Each call gets the unit SF would actually send: the call's own first-due station if that unit is free, otherwise the one that can arrive soonest. Busy units stay busy. The output is a predicted "time until help arrives" for every call.
4. **Validation.** Everything is fit on data before October 2026, then October is replayed and compared with what really happened. By neighborhood the simulation matches reality with **r = 0.94**, a typical error of **16 seconds**, and it beats two simpler baselines.
5. **What-if.** Change the city (close or add stations and units, scale call volume), replay the same month, and compare every neighborhood with today.

**No AI calculates any number.** Claude Haiku is used only to turn a typed question ("close the two stations nearest Bayview") into control settings. Every number comes from steps 2–4, which are deterministic Python.

---

## 2. Headline findings (simulator v2, held-out October 2026)

| Finding | Number |
|---|---|
| Citywide median wait (call received → first unit on scene) | **7:24**; 39.5% of calls take over 8 minutes |
| Slowest neighborhood | **Visitacion Valley 9:54**; 80% of calls over 8 min |
| Fastest neighborhoods | Western Addition 6:41, Hayes Valley 6:41, Mission 6:43 |
| Gap between slowest and fastest | about **3¼ minutes**, same city and same fire department |
| The "slow zone" | Lakeshore, Excelsior, Visitacion Valley, Twin Peaks: ≥ 60% of calls over 8 min. **74,863 residents, 13,739 aged 65+** (ACS 2019–2023) |
| Equity pattern (41 neighborhoods, correlation with median wait) | % Asian r = 0.30, % Hispanic r = 0.27, % White r = −0.39, % Black ≈ 0.00. A pattern, not proof of cause: distance from the downtown station cluster drives it. |
| **+2 ambulances at Stations 43 (Excelsior) and 44 (Visitacion Valley)** | Visitacion Valley **73 s faster**, Excelsior 27 s faster, no neighborhood meaningfully slower (worst +5 s) |
| Close Stations 1, 8 and 36 (downtown) | South of Market +16 s, Tenderloin +8 s. Downtown stations are dense, so neighbors cover. |
| Heat wave, 2× calls | Citywide 7:33 (+9 s); worst Presidio Heights +23 s |
| How SF really dispatches | **86.5%** of first-arriving engines come from the call's own first-due station (63% of trucks); ambulances rove (0.2%) |
| Hospital diversion (last 12 months) | **SF General, the only Level 1 trauma center, was on ambulance diversion 2,548 h, about 29% of the time**. UCSF Parnassus 1,254 h, Kaiser SF 1,120 h, CPMC Van Ness 748 h. |
| Ambulance time lost at hospitals | Example from the data: ambulance AM122 reached the hospital at 1:14 AM and wasn't available until 2:27 AM, more than an hour at the ER |

Caveats: the volume scenario resamples extra calls at random times, so small per-neighborhood changes (about ±20 s) in that scenario are partly noise. Treat the heat-wave per-neighborhood numbers as rough.

---

## 3. Data sources

All public. "DataSF" is San Francisco's open data portal (data.sfgov.org / data.sf.gov, Socrata API).

| Dataset | ID / source | What we use | Size | File |
|---|---|---|---|---|
| Fire Dept & EMS Dispatched Calls for Service | DataSF `nuek-vuh3` | every unit dispatched: received, dispatch, en-route, on-scene, available timestamps; unit id/type; first-due station area; location; neighborhood; call type; priority | 300,081 incidents, Nov 2024 – Oct 2026 | `data/sf/calls.csv.gz` |
| Same dataset, transport fields | DataSF `nuek-vuh3` | `transport_dttm`, `hospital_dttm`: which units took a patient to hospital | 15,521 transports, Sep–Oct 2026 | `data/sf/transports.csv.gz` |
| City-owned facilities (fire and police) | DataSF `wt27-4jim` | real fire station and SFPD district station locations | 44 fire stations (40 with units), 10 police stations | `data/sf/stations.json`, `data/sf/police_stations.json` |
| Analysis Neighborhoods | DataSF `j2bu-swwd` | neighborhood boundaries, simplified to ~4 m (42,667 → 7,279 vertices) | 41 neighborhoods | `data/sf/hoods.geojson` |
| Population & demographics (ACS 2019–2023) | DataSF `4qbq-hvtt` | population, age 65+, race by neighborhood | 42 neighborhoods | `data/sf/people.json` |
| Law Enforcement Dispatched Calls (closed) | DataSF `2zdj-bwza` | real SFPD dispatches for the replay month | 23,702 calls (Oct 2026) | `data/sf/police_calls.csv.gz` |
| Hospital Diversions | DataSF `t4sf-777q` | which hospitals have emergency departments, and their diversion hours | 10 hospitals | `data/sf/hospitals.json` |
| Licensed healthcare facility locations | CA HHS (data.chhs.ca.gov) | hospital coordinates. The VA isn't state-licensed, so its location is approximate, from its address. | 10 | (joined into `hospitals.json`) |
| Street centerlines | DataSF `3psu-pn9h` | the drivable street network for routing | 16,378 active segments | `data/raw/streets/` (not committed) |
| Building footprints with LiDAR heights | DataSF `ynuv-fyni` | 3D buildings in satellite mode (`hgt_median_m`) | 93,017 kept of 177,023 | `data/sf/buildings.json` (12 MB, 2.8 MB gzipped) |
| Terrain | AWS Terrain Tiles (terrarium, USGS 3DEP) | ground height grid so vehicles sit on Google's 3D terrain | 513 × 485 grid, ~35 m cells | `data/sf/elevation.json` |
| Imagery | Esri World Imagery (free tiles) / Google Photorealistic 3D Tiles (Map Tiles API key) | backdrop only | — | — |

**Cleaning rules (calls):**
- Drop records without an on-scene time or a location, or located outside SF.
- Drop dispatch delays under 0 or over 60 min, and responses over 3 h.
- Count trips for travel-time learning only if the trip took 0–30 min **and** the unit had been idle ≥ 15 min first, so it was most likely starting from its station.

**Data we wanted but couldn't get:**
- **Rest of the Bay Area:** San Jose publishes fire incidents, but times are rounded to the minute and locations are street names only. Oakland's last dispatch dataset is from 2012. Berkeley and the counties have nothing public. So the simulator is SF-only.
- **Flock camera data:** not public (police only).
- **OpenStreetMap roads:** every Overpass server failed during the event, so routing uses DataSF street centerlines.

---

## 4. How the model works (`src/backend/sim.py`)

### 4.1 Unit homes
- Station-numbered units (E42 = Engine 42 at Station 42; same for trucks) live at their station's real coordinates.
- Units without a fixed station, which is most ambulances (MEDIC, PRIVATE), get a "home" at the median location of the calls they reached fastest (under 4 min).
- 170 units in total.

### 4.2 Travel-time model (`TravelModel`)
For each trip that started at home (idle ≥ 15 min), we record the travel time (on-scene − en-route) under three keys:
1. `(home, 500 m cell, hour band)`: median, used if ≥ 5 trips
2. else `(home, cell)` over all hours: median, if ≥ 5 trips
3. else `(hour band, straight-line distance bin of 250 m)`: median, forced to be monotone (never faster when farther)

- **Hour bands:** 0 = 0–6, 1 = 6–10, 2 = 10–16, 3 = 16–20, 4 = 20–24 (local SF time).
- **Scale:** 332,948 trips learned, 10,448 station × cell × hour medians.
- `from_point()` estimates travel from any location (used for units still at a scene or a hospital) with the distance-bin medians.

### 4.3 Simulator (`simulate`), version 2
Calls are replayed in order. For each call:
1. **Dispatch time** = the call's real received time + its real call-to-dispatch delay. We don't model call-taking.
2. **Units sent:** the same number and types of units that really went (e.g. 1 engine + 1 ambulance).
3. **Choosing each unit:**
   - **Engines and trucks:** the call's own first-due station's unit is preferred if it's free right now. Otherwise, whichever unit of that type can arrive soonest.
   - **Ambulances:** always the soonest.
   - **Arrival** = when the unit becomes free + the real turnout time of the unit it replaces + travel time.
4. **Where units are:** after a call, a unit is at the scene, or at the nearest hospital if the real unit transported a patient, until it has had time to drive home. If it's dispatched before then, travel starts from where it is.
5. **Busy time:** the unit is busy for the real on-scene → available time of the unit it replaces (this includes transport and hospital handoff).
6. **On duty:** a unit only works on days it really worked (a proxy for staffing). Added units work every day.
7. **Calibration:** the simulator is systematically about a minute optimistic, because real dispatch has frictions it doesn't model. We add the median real-minus-simulated gap measured on September 2026, which comes *before* the held-out month, per hour band: 0–6 h +60 s, 6–10 h +56 s, 10–16 h +57 s, 16–20 h +47 s, 20–24 h +48 s.
8. **Volume scenarios** (e.g. 2×): extra calls are copies of real calls shifted to random times in the month (seeded), or calls are randomly dropped for volumes under 1.

**Output:** per call, the simulated "call received → first unit on scene" time. These are summarized per neighborhood (median, % over 8 min), citywide, and as a per-dispatch log for the replay.

### 4.4 Validation (`python3 src/backend/sim.py`)
Fit on Nov 2024 – Sep 2026, test on **October 2026 (13,417 incidents, never seen)**:

| Model | Median error per call | Neighborhood r | Neighborhood median error | % calls > 8 min (real: 45.6%) |
|---|---|---|---|---|
| Baseline: everyone gets the city median | 153 s | n/a | 28 s | 0% |
| Baseline: straight-line distance, one speed | 100 s | 0.72 | 32 s | 39.2% |
| Runnr v1 (units always leave from home, flat calibration) | 90 s | 0.92 | 16 s | 39.6% |
| **Runnr v2 (first-due dispatch, unit positions, calibration by hour)** | **91 s** | **0.94** | **16 s** | 39.6% |

- **"Neighborhood r":** the correlation between simulated and real neighborhood median response, across neighborhoods with ≥ 30 calls. This is what the app is built to compare.
- **Per-call error:** about 90 s. One call depends on things the model can't see, so the app compares neighborhoods and options; it doesn't predict a single call.
- **Sanity checks (asserted):** closing a station or adding load never makes the city faster.
- **What changed v1 → v2:**
  - Tracking unit positions (scene or hospital) alone barely moved accuracy.
  - The real gain came from first-due dispatch, which is how SF actually dispatches: neighborhood r went from 0.92 to 0.94.

### 4.5 Police (not simulated)
SFPD doesn't publish unit IDs, so there's nothing to model. Police cars are **replayed from real SFPD records**:
- real dispatch, on-scene and clear times
- each drawn leaving its district station (in reality officers are usually already on patrol, so the drawn drive is a simplification)
- scenarios don't change police

On the default replay day (Fri Oct 16), that's 805 calls.

---

## 5. Architecture

```
src/backend/
  app.py      HTTP server (stdlib http.server), API, gzip, cache warm-up
  sim.py      travel-time model, simulator, validation, export
  routing.py  street graph (DataSF centerlines), shortest-path trees, route()
  nl.py       Claude Haiku what-if parser (stdlib urllib, no SDK)
  paths.py    every file location
src/frontend/index.html   the whole UI (deck.gl 8.9 from unpkg, no build step)
scripts/      fetch_data.py · fetch_buildings.py · fetch_hospitals.py · fetch_elevation.py · fetch_police.py
data/sf/      source datasets (committed)
data/build/   generated: runtime.pkl.gz (model + fleet + calibration + held-out month), routes.pkl.gz, validation.json
data/raw/     big downloads (gitignored)
docs/PRD.md   original spec
config/.env.example
render.yaml   Render Blueprint
CLAUDE.md     notes for Claude Code
```

**Python standard library only.** There's nothing to install, so the Render build step is `true`.

### 5.1 API (`src/backend/app.py`)
| Endpoint | Does |
|---|---|
| `GET /` | the app |
| `GET /api/meta` | fleet, period, replay days, baseline results, and a replay of the busiest day (Fri Oct 16). Prebuilt and gzipped at startup. |
| `POST /api/simulate` `{closed:[station], added:[[station,"MEDIC"\|"ENGINE"\|"TRUCK"]], volume:0.5–3, day:<ordinal>}` | runs the whole held-out month with that scenario (< 1 s) and returns per-neighborhood + citywide results and a replay of the chosen day. Results are cached in a small LRU of 24. |
| `POST /api/ask` `{text}` | Claude Haiku turns the text into `{closed, added, volume, explanation}`, or `{error}`. 400 on unusable questions, 503 if no key. |
| `GET /api/config` | `{google_maps_key}` for the browser (a Map Tiles key, restricted by domain in Google Cloud) |
| `GET /data/<file>` | whitelisted data files, gzipped and cached |

**Replay trip format** (array per dispatch): `[unit, type, from_lng, from_lat, to_lng, to_lat, depart, arrive, free, back_home, hood, call_time, call_type, street_path|null]`
- Times are seconds after local midnight, and include the calibration.
- `street_path` is the precomputed street route from the unit's home. It's null (drawn straight) when the unit leaves from a scene or hospital, which is 13% of trips on the default day.

**Police format:** `[district, fx, fy, tx, ty, dispatch, onscene, clear, call_type, priority, path]`.

### 5.2 Routing (`src/backend/routing.py`)
- **Network:** DataSF street centerlines, treated as undirected (emergency vehicles ignore one-way rules), largest connected component only: 32,929 nodes, 39,477 edges.
- **Paths:** one Dijkstra shortest-path tree per unit home, fire station and police station (about 156 sources), stored as predecessor arrays in `data/build/routes.pkl.gz` (about 9 MB).
- **Lookup:** `route(home_key, lng, lat)` snaps the point to the nearest street, backtracks the tree, simplifies the path (8 m) and appends the exact point.
- **Timing:** comes from the travel-time model, not the route length. Calls more than about 600 m from any street fall back to a straight line.

### 5.3 Frontend (`src/frontend/index.html`)
- **Map:** deck.gl 8.9. In Google mode it's Google Photorealistic 3D Tiles; otherwise Esri satellite imagery with the rest of the Bay dimmed. The camera is limited to SF.
- **Google detail:** full detail over SF only. Elsewhere, tile refinement is capped by depth (rest of the Bay 15, beyond 9), and tiles get sharper as you zoom in.
- **Ground heights:** vehicles, trails, calls and buildings are placed at `elevation + GEOID (−32.5 m) + 0.6 m`, so they sit on Google's terrain. Google's tiles are measured from the WGS84 ellipsoid, and SF's geoid is about 32.5 m below it. The camera pivots on the ground height under it.
- **What's drawn:**
  - 3D meshes: firehouses, police stations, hospitals (helipads on the big ones), engines, trucks, ambulances and police cars, with light bars flashing while driving to a call
  - pulsing beacons on every station and hospital
  - neighborhood response-time colors, the 500 m grid, and the dispatch trails
- **Panels:**
  - top strip: verdict, median wait, % over 8 min, with ± chips for scenarios
  - tabs: What if / Results / Map
  - Results tab: "How the numbers are made" panel and the accuracy table
  - timeline with a calls-per-hour chart, a one-line dispatch feed, a floating legend
  - Arshdeep's "How fast would help reach you?" address lookup
- **Controls:** drag, scroll or pinch; arrow keys move; Shift+arrows rotate and tilt; + and − zoom; R resets; Space plays and pauses. Clicking a vehicle follows it; the "street level" button drops in behind a moving vehicle.
- **AI transparency:** a note under the Ask box. After a question, "Read as: close Stations 17, 25" shows exactly what the AI set.
- **Performance:** static layers are memoized; the map draws before the 911 data arrives; buildings are skipped in Google mode. About 55 fps on a laptop.

### 5.4 The AI part (`src/backend/nl.py`)
- **Model and output:** Claude Haiku (`claude-haiku-5-5`) with low effort and JSON-schema structured output.
- **Context it gets:** the station list, plus each neighborhood's 4 nearest stations, so "near Bayview" resolves to real station numbers.
- **Checking what comes back:** unknown stations or types are dropped, volume is clamped to 0.5–3, and closures and additions are capped at 20 each.
- **Cost and speed:** about $0.0001 per question after the first call caches the prompt; 2–4 s.
- **Key:** `ANTHROPIC_API_KEY` (env or `.env`), stripped of a trailing newline from dashboard paste.
- **Role:** it never sees 911 data and never produces a number.

---

## 6. Run, rebuild, deploy

```bash
python3 src/backend/app.py               # http://localhost:8000 (PORT overrides)
python3 src/backend/sim.py               # validation table + sanity checks -> data/build/validation.json
python3 src/backend/sim.py --export      # rebuild data/build/runtime.pkl.gz (the server loads only this + routes)
python3 src/backend/routing.py --build   # rebuild data/build/routes.pkl.gz
python3 src/backend/nl.py                # live Haiku self-test (~$0.0003)
python3 scripts/fetch_data.py            # re-download DataSF data into data/sf/
python3 scripts/fetch_elevation.py       # rebuild the terrain grid
python3 scripts/fetch_hospitals.py       # rebuild hospitals + diversion hours
```

- **Python:** 3.10+. Downloads use `curl`, because python.org builds on macOS often can't verify certificates.
- **Environment** (`config/.env.example`, copy to `.env`; never commit it):
  - `ANTHROPIC_API_KEY`: Ask box
  - `GOOGLE_MAPS_API_KEY`: Google 3D city (Map Tiles API, restricted to `runnr-cbc8.onrender.com/*` and `localhost:8000/*`)
- **Deploy:** Render Blueprint (`render.yaml`, free plan, Python 3.12). Every push to `main` redeploys in about a minute. Both keys are set in Render → Environment.
- **Server footprint:** startup ~5 s (it pre-builds the default payload and gzips the data files); memory ~150 MB; responses ~1 ms after warm-up.

---

## 7. Limits (say these out loud, the rubric rewards it)
- **Per-call error:** about 90 s. Built to compare neighborhoods and options (16 s error, r = 0.94), not to predict one call.
- **Not modeled:** crew staffing changes, mutual aid from neighboring cities, hospital wait times (only in busy time), and changes to which station is first-due when one closes.
- **Data assumptions:** the future looks like the past (same places and times of calls). On-duty status is a per-day proxy.
- **Simplifications:**
  - Units leaving a scene or hospital use distance-based travel estimates and are drawn in a straight line.
  - Police are replayed, not simulated, and drawn from their district station.
  - Buildings in satellite mode use median roof height, so Salesforce Tower shows 181 m, not 326 m.
- **Statistics:** the equity correlations are across only 41 neighborhoods, and distance from stations is the likely cause.

---

## 8. Questions a mentor or judge will ask

- **"Isn't this just a Google Maps wrapper?"** No. Google only draws the backdrop: turn off "Google 3D city" in the Map tab and it runs on free satellite imagery. The core is the data pipeline, the learned travel model, the simulator and the validation. None of that exists in any Google API.
- **"Isn't an LLM just predicting the outcomes?"** No. The AI only reads typed questions and fills in the controls ("Read as: …" shows exactly what it set). Every number comes from `sim.py`: same input, same output. Run `python3 src/backend/sim.py` offline, with no key and no internet, and it prints the accuracy table.
- **"How do you know it's right?"** We held out October 2026 and compared simulated with real arrival times: neighborhood r = 0.94, a 16 s neighborhood error, better than two baselines.
- **"Why first-due dispatch?"** The data shows 86.5% of first engines come from the call's own station. Adding that rule improved accuracy.
- **"Why not the whole Bay Area?"** No other Bay Area city publishes unit-level dispatch times with coordinates.

---

## 9. How we got here (decision log)

1. The first idea was a copy of a past project, "LabTwin" (a 3D lab-bench physics twin, Gemini Argon hackathon). We dropped it: no dataset (the rubric gives ~75 of 100 points to data, method and validation), too close to someone else's work, and unrealistic for one day.
2. **Runnr v0:** a lab-protocol "spellchecker" trained on BioProt (100 protocols.io protocols). It worked (cross-validated injected-error detection), but it was weak: caught ~24% of 10× typos at 5% false alarms. We pivoted. It's in git history at commit `fa7e4a4`.
3. **Runnr (now):** the SF 911 response simulator. It has real data, real validation, a public-safety impact and an equity angle.
4. Then added, in order:
   - a 3D UI (styled after Bench), then a live replay
   - street routing, 3D buildings and the Haiku Ask box
   - police, hospitals and beacons
   - a UI pass
   - the Google 3D city, load-time fixes and terrain heights
   - AI transparency
   - simulator v2

---

## 10. Open items
- **Google driving-time baseline:** compare Google's ordinary-car ETA with our model on a sample of held-out calls, as another row in the accuracy table. Needs a server-side key with the **Routes API** enabled (`GOOGLE_ROUTES_API_KEY`). Not built yet.
- **"If I'm at this address" what-if:** expected wait at your location by time of day and scenario, from the simulator. The backend endpoint is planned; Arshdeep's address lookup is the UI entry point.
- **Keep-warm ping** (a GitHub Action curling the site every 10 min) to avoid Render cold starts. Not added. Otherwise, open the site a few minutes before presenting.
- **The deck** (https://claude.ai/artifact/KCR3xuBB2VjJakXPcHr3uH) has v1 numbers (10:07, −88 s, r 0.92) and old screenshots. Update to section 2's numbers before presenting.
- **Rubric fit** (Acalanes Data Fest): problem 15 · data 15 · method 25 · validation 20 · originality 10 · feasibility 5 · communication 10. The demo order the rubric asks for is question → data → build → finding → limits.
