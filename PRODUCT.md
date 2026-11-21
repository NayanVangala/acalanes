# Medio: product context

**What it is:** a simulator of San Francisco's fire and EMS response, built from 300,081 real 911 incidents and validated against a held-out month (neighborhood r = 0.94). It's shown as a live 3D replay of a real day, with what-if scenarios.

**Positioning:** a community tool. It gives residents the city's own 911 data in a form they can act on: check their block, make the case to officials, propose and vote on fixes.

**Who uses it:**
- Today: hackathon judges and mentors at Acalanes Data Fest, during a 5-minute demo, on a laptop and projector.
- Next: residents ("how fast would help reach me?"), journalists, and city budget staff comparing station and ambulance decisions.

**Primary jobs on the app surface (mode: Operate):**
1. See how long help takes across the city, and where it's slowest.
2. Change the city (close a station, add an ambulance, change call volume) and see who gets slower or faster.
3. Look up your own address and see your typical wait, by time of day and kind of emergency.
4. Trust the numbers: see how they're made and how accurate they are. AI only reads typed questions and never produces a number.

**Tone:** a calm instrument, not a video game. It's emergency data about real people, so the voice is factual, plain and specific, and never alarmist.

**Visual reference the team chose:** "eagle." (Gemini 4 Argon SF hackathon), a flight-planning app:
- docked side columns
- a conversational left column with lettered options
- a right-hand check-list of uppercase monospace labels with colored status and right-aligned values
- one orange primary action
- a profile strip along the bottom
- small boxed layer chips over the map

**Constraints:**
- One file: `src/frontend/index.html` (deck.gl 8.9, no build step).
- The map (layers, camera, Google 3D) is out of scope for UI passes unless asked.
- Every element ID used by the script must keep working.
- A teammate (Arshdeep) edits the same file, so pull before editing and commit small.
