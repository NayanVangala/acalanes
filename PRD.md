# Runnr: PRD

Acalanes Data Fest 2026 · Nov 21, 2026 · demo by 5 PM (5-minute presentation)

## 1. Problem

When someone calls 911, the minutes until help arrives decide outcomes for cardiac arrests, fires and overdoses. How fast help arrives depends on where stations are, how many units exist, and how busy they are. Cities make station and staffing decisions, like closing a station, cutting an ambulance or adding a unit, often with little public evidence of who would be affected.

**Question:** Using San Francisco's real 911 records, can we simulate the city's fire and EMS response accurately enough to answer "what if" questions, and which neighborhoods wait longest today?

## 2. Users

| User | Need |
|---|---|
| Residents and journalists | "Does my neighborhood wait longer? Who is hurt if Station X closes?" |
| City council and budget staff | Compare scenarios before a vote |
| Fire and EMS planners | A quick, transparent first pass before a full study |
| Judges (today) | A clear question, real data, a validated method, an honest statement of limits |

## 3. Goals and non-goals

**Goals**
- Replay real SF 911 incidents in a simulation built only from public data.
- Show the simulation matches reality on held-out data.
- Let a user run "what if" scenarios and see the impact by neighborhood on a 3D map.
- Report one clear equity finding: who waits longest, and by how much.

**Non-goals**
- Not a tool for real operational decisions (see Limits).
- No live, real-time feed.
- No cities other than SF.

## 4. Data

**SF Fire Department Calls for Service** (DataSF `nuek-vuh3`): public, about 14k incidents a month, with timestamps (received, dispatched, en route, on scene, available), the unit sent, its unit type and station, the incident location, the neighborhood, the call type and the priority.

- **Training:** 2 years of incidents, all units dispatched (not only the first).
- **Validation:** the most recent full month, held out and never used to fit anything.
- **Station locations:** not published as a dataset. Inferred from the data as the median location of each station's own engine's fastest arrivals. Checked against the real address of a few stations.

**Cleaning rules:** drop rows missing on-scene or available times, travel ≤ 0 s or > 60 min, busy time > 6 h, or a location outside SF.

## 5. Method

1. **Travel-time model (learned from real trips).** Split the city into ~500 m grid cells.
   - Predicted travel time from station *s* to cell *c* in hour band *h* = median of real recorded trips for (*s*, *c*, *h*) when there are at least 5 trips.
   - Otherwise, fall back to a regression on distance, with a speed factor for the hour band fitted on all trips.
   - The real trips already include roads, hills and traffic, so no map router is needed.
2. **Dispatch delay and busy time.** Use each incident's real recorded call-to-dispatch delay and real time on scene.
3. **Simulation (discrete-event).**
   - Incidents arrive in real order.
   - Dispatch the available unit of the right type with the shortest predicted travel time. Medical calls can take a medic or an engine, as in the data.
   - The unit is busy for travel + time on scene, then returns to its station.
   - If no unit is free, the call waits in a queue.
4. **Scenarios.** Close a station, add a unit at a station, scale call volume (×1.5 or ×2), or remove ambulances.

## 6. Validation (rubric: 20 pts)

Replay the held-out month with the real station and unit setup. Compare simulated first-unit arrival with the actual recorded arrival.

| Metric | Target |
|---|---|
| Median absolute error per incident | < 90 s |
| Neighborhood median arrival, simulated vs real | correlation > 0.8; error < 60 s for most neighborhoods |
| Share of calls over 4 min travel, by neighborhood | within 5 percentage points |

**Baselines to beat:**
- (a) Every call gets the city-wide median.
- (b) Straight-line distance at a single average speed.

This shows the learned travel model is what makes the simulation accurate.

**Sanity checks:** closing a station should never make response faster, and doubling call volume should never reduce queueing.

## 7. Product (MVP)

**A 3D map of SF, pitched like a game view:**
- Neighborhood or grid columns, raised and colored by median arrival time.
- Stations shown as markers. Units animate along their trips during the replay.
- **Controls:** pick a station to close, add a unit, set call volume, toggle a medical-only view.
- **Results panel:**
  - city median arrival time
  - % of calls over the 4-minute travel target
  - top 5 neighborhoods that get worse vs. baseline
  - number of residents and calls affected
- **"How accurate is this?" panel:** the validation chart (simulated vs real by neighborhood).

**MVP scope (must ship today)**
1. Data download and cleaning, committed as a compact file.
2. Travel-time model, simulator, and validation script that prints the metrics table.
3. Server endpoint `POST /simulate {closed_stations, added_units, volume}` that returns per-neighborhood results.
4. The 3D map with columns, stations, the close-station / add-unit / volume controls, and the results panel.
5. Deployed on Render.

**Later (only if time is left)**
- Animated unit trips.
- Medical vs fire split.
- Equity overlay with census income by neighborhood.
- Optimal placement: "where would one new ambulance help most?"

## 8. Tech

- **Python standard library only:** data, model, simulation, HTTP server. No installs, which keeps Render deploys simple.
- **Front end:** one `index.html` with deck.gl and a MapLibre basemap from the CDN (free CARTO dark style, no API key).
- **Deploy:** Render web service with start command `python3 app.py`, binding to `0.0.0.0:$PORT`. Data ships with the repo.

## 9. Limits (said out loud in the demo)

- Areas with few recorded trips use the distance fallback, which is less accurate.
- It doesn't model crew staffing, mutual aid from neighboring cities, hospital wait times or unit cross-staffing.
- It assumes future calls look like past calls in where and when they happen.
- Inferred station locations are approximate.
- It's a transparency and screening tool, not grounds alone to close or open a station.

## 10. Demo script (5 min)

1. **Question (30 s):** "Who waits longest for an ambulance in SF, and what happens if a station closes?"
2. **Data (45 s):** real 911 records: how many incidents, what's in them, what we cleaned out.
3. **Build (90 s):** the travel times learned from real trips, the simulator, then the live 3D map.
4. **Finding (90 s):** the validation chart ("it matches reality within X s"), then the equity finding, then a live "close Station N" scenario.
5. **Limits (45 s):** section 9.

## 11. Timeline (today)

| By | Done |
|---|---|
| +1 h | Data downloaded; travel model working |
| +2 h | Simulator and validation numbers |
| +3.5 h | Server and 3D map with controls |
| +4 h | Deployed on Render |
| 4:00 PM | Freeze code; slides and rehearsal |

## 12. Risks

| Risk | Mitigation |
|---|---|
| Download slow or blocked | Start with 6 months, which is enough for the MVP; add more if time allows |
| Validation misses its targets | Report the honest numbers and the comparison with the baselines; still valid for the rubric |
| 3D front end takes too long | Fall back to a flat map with colored columns; the controls still work |
| Render cold start / build issues | No dependencies; test the deploy early (by +3.5 h) |
