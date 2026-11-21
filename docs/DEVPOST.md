# Medio: Devpost submission

**Project name:** Medio

**Tagline (short description):** A community tool that turns San Francisco's own 911 records into answers: how fast help reaches your block, why some neighborhoods wait longer, and what would fix it.

**Links**
- Live app: https://runnr-cbc8.onrender.com (free hosting: the first load after idle can take 30–50 s)
- Code: https://github.com/NayanVangala/acalanes

**Built with:** Python, deck.gl, Google Photorealistic 3D Tiles, Claude Haiku (Anthropic API), DataSF open data, U.S. Census ACS, OpenStreetMap (Photon), Render

---

## Inspiration

When you call 911, the minutes until help arrives decide outcomes. San Francisco publishes every fire and medical 911 call: who was sent, when they left, when they arrived. But that data sits in a spreadsheet nobody in a neighborhood can use. We wanted residents to be able to ask: *how long would help take to reach me, is my neighborhood treated fairly, and what would actually make it faster?*

## What it does

Medio is a live 3D replay of San Francisco's real 911 response, plus a simulator you can talk to.

- **Check my block.** Enter an address and see the typical wait for help there, by time of day and kind of emergency, with your house pinned on the map.
- **Ask a what-if in your own words.** "A Giants game tonight", "pouring rain at rush hour", "a crash on 6th Street at 5 PM", "close Station 1". Medio replays all 13,417 real October 2026 calls with that change and shows:
  - the before and after where and when it matters
  - how many calls get 30+ seconds slower or faster
  - the neighborhoods that change most

  The whole map switches to the scenario.
- **Propose and vote.** Click a spot to propose a new fire station or ambulance post. Medio shows who it would help before you submit, and neighbors vote and share it.
- **Make the case.** After an address lookup, Medio writes a short, sourced paragraph for your neighborhood (your wait vs. the city, the share of calls over 8 minutes, what a new station would do) to copy, email to a supervisor, or share as a link.
- **Data story and Breakdown.**
  - Data story: the question, the data, the method, the validation and the limits.
  - Breakdown: engines, trucks, ambulances and police, side by side.
- **AI activity page.** It shows, live, what each AI agent did for every question.

## Our key findings

- **An equity gap.** In real October 2026 records joined with Census data, neighborhoods in the quarter with the fewest white residents wait **8:13** for help, against **7:09** in the quarter with the most: a **1:04 gap** (r = −0.59 across 36 neighborhoods).
- **It's distance, not busy units.** We added one ambulance at each of the 43 stations in turn; the five slowest neighborhoods improve by at most 13 seconds. A new *station* at Visitacion Valley's center cuts nearby waits by **0:54** (9:29 → 8:35), the most of any neighborhood we tried.
- **Where help is slowest.** Visitacion Valley (9:54), Lakeshore and Excelsior: 65–80% of calls wait over 8 minutes.

## How we built it

1. **Data (DataSF + Census).**
   - **SF Fire Department Calls for Service:** 300,081 incidents, Nov 2024 – Oct 2026, with every unit's dispatch, en-route, on-scene and available times.
   - **SFPD dispatches** and **hospital diversion records.**
   - **Station locations** and **street centerlines.**
   - **Census ACS** population by neighborhood.
2. **Learned travel times.** From **332,948 real drives** (only units that had been idle 15+ minutes, so they truly left from their station), we learn the typical drive time from each station to each 500 m block at each time of day. Hills, one-way streets and traffic are already inside those times because the drives really happened.
3. **Simulator.** It replays every call in order, the way SF dispatches:
   - the call's own first-due station goes first when free, otherwise the unit that can arrive soonest
   - busy units stay busy
   - ambulances end up at hospitals after transports
4. **Validation.** Everything is fit on data before October 2026; then October is replayed and compared with what really happened:

   | Method | Error per neighborhood | Error per call |
   |---|---|---|
   | **Medio** (r = 0.94) | **16 s** | **91 s** |
   | Straight-line distance | 32 s | 100 s |
   | City median | 28 s | 153 s |

5. **AI that never invents numbers.** Five Claude Haiku agents (Router, Stations, Demand, Events, Records) read the typed question **in parallel**, each filling only its own simulator settings. The deterministic Python simulator then produces every number. A sixth Haiku call writes one sentence from the measured result, and a number checker rejects it if any number doesn't appear in the simulator's output.
6. **Interface.**
   - A full-screen deck.gl map over Google's photorealistic 3D city.
   - Vehicles follow real streets and terrain.
   - A time-of-day wait gradient shows how coverage shifts across the day.
   - A Python standard-library server, deployed on Render.

## Challenges we ran into

- **Making a simulation trustworthy:** we held out a full month and compared with reality, and kept two baselines to beat.
- **Keeping the AI honest:** a mentor asked whether we were "just asking an LLM to predict". We restructured so the AI only reads words and writes one sentence, and every number is checked against the simulator.
- **Small effects looked like "no change":** a single crash barely moves a neighborhood's median. We now measure where and when the change acts and report how many calls are hit and by how much.
- **Free hosting limits:** monthly simulations are memory-heavy; we capped caches to stay under Render's 512 MB.
- **3D performance:** we cut the drawn line geometry 4.5× and added Quality / Balanced / Fast settings.

## Accomplishments that we're proud of

- Neighborhood-level accuracy of r = 0.94 on a month the model never saw.
- A real, data-backed equity finding residents can act on.
- Any situation typed in plain English becomes a real simulation in seconds, with every number traceable.

## What we learned

- **Validation is the product:** a pretty map means nothing without a held-out test.
- **Equity gaps here come from geography:** they're about distance to stations, not how busy units are.
- **Transparency matters:** showing what the AI did (the AI activity page, "What Medio simulated") builds more trust than hiding it.

## Limits (honest)

- The equity result is a correlation; it matches distance from stations, and we didn't test other causes.
- It's accurate for areas, not single calls (about 1.5 minutes off for an individual call).
- It was tested on one month (October 2026).
- Police response and hospital capacity aren't simulated, and slower driving is a scaling factor, not live traffic.
- Ambulance drive times are underestimated, because real ambulances are often already on the road.

## What's next for Medio

- Share proposals with district supervisors with vote counts attached.
- More months of validation, with error bars on every result.
- Police response modeling from SFPD records.
- Bring it to other cities that publish 911 data.
