"""Medio web server. python3 app.py -> http://localhost:8000 (Render sets $PORT).

GET  /                 the app (src/frontend/index.html)
GET  /data/<file>      stations, hoods, people, validation, buildings, police_stations, hospitals, elevation (.json)
GET  /api/meta         fleet, period, replay days, baseline results + replay of the default day
GET  /api/config       {"google_maps_key": ...} for the Google 3D city (browser key, domain-restricted)
POST /api/simulate     {"closed": [stations], "added": [[station, "MEDIC"|"ENGINE"]], "volume": 1.0, "day": <ordinal>}
                       -> neighborhood results for the whole held-out month + a replay of one day
POST /api/here         {"lng", "lat", + optional scenario} -> typical wait at that spot by time of day and kind of call
POST /api/ask          {"text": "...", "me": {lng, lat, label}} -> answer built from simulator numbers (needs nl.py + ANTHROPIC_API_KEY)
JSON and /data files are gzip-compressed when the browser accepts it.
"""
import csv
import gzip
import json
import math
import os
import pickle
from collections import Counter, OrderedDict
from functools import lru_cache
from datetime import date, datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

import sim
from sim import TravelModel  # noqa: F401  (needed to unpickle the model)

from paths import BUILD, FRONTEND, SF
from paths import ROOT as ROOT_DIR


def env_key(name):
    """Read NAME=value from the repo's .env (local dev only; Render uses real environment variables)."""
    env = ROOT_DIR / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == name and v.strip():
                return v.strip().strip('"').strip("'")
    return None


def data_file(name):
    return (BUILD if name == "validation.json" else SF) / name


with gzip.open(BUILD / "runtime.pkl.gz", "rb") as f:
    RT = pickle.load(f)
STATIONS = set(sim.load_stations())
TYPES = {"MEDIC", "ENGINE", "TRUCK"}
LOCK = Lock()  # the travel memo is shared; one simulation at a time is plenty for a demo
CACHE = OrderedDict()
DAYS = Counter(i["day"] for i in RT["test"])
DEFAULT_DAY = max(DAYS, key=DAYS.get)  # the busiest day makes the liveliest replay
def off_for(t):
    return round(sim.offset_for(RT["offset"], t))  # calibration seconds for a call at time t (per time of day)
DATA_FILES = {"stations.json", "hoods.geojson", "people.json", "validation.json", "buildings.json", "police_stations.json", "hospitals.json", "elevation.json"}
# added units (NEW*) start at a station; find its key from the start coordinates in the sim log
STATION_AT = {(round(lng, 5), round(lat, 5)): "S" + k for k, (lat, lng) in sim.load_stations().items()}
try:
    import routing
    routing._load()
except Exception as e:  # no routes file: vehicles fall back to straight lines
    print(f"street routing off: {e}")
    routing = None


def street_path(d):
    # street routes are precomputed from each unit's home; a unit leaving a scene or hospital is drawn straight
    if routing is None or not d.get("from_home", True):
        return None
    h = RT["homes"].get(d["unit"])
    key = h[0] if h else STATION_AT.get((round(d["from"][0], 5), round(d["from"][1], 5)))
    return routing.route(key, d["to"][0], d["to"][1]) if key else None


def load_police():
    """Real SFPD dispatches (scripts/fetch_police.py), grouped by local day. Empty if the file is missing."""
    by_day = {}
    try:
        stations = {s["district"]: s for s in json.load(open(SF / "police_stations.json"))}
        with gzip.open(SF / "police_calls.csv.gz", "rt") as f:
            for r in csv.DictReader(f):
                if r["district"] in stations:
                    by_day.setdefault(sim.day(int(r["dispatch"])), []).append(r)
    except FileNotFoundError:
        stations = {}
    return stations, by_day


POLICE_STATIONS, POLICE = load_police()


@lru_cache(maxsize=40)
def police_day(ordinal):
    """[district, fx, fy, tx, ty, dispatch, onscene, clear, call_type, priority, path] for one day, times in
    seconds after local midnight. Real records: scenarios don't change them. clear = close, capped at 45 min on scene."""
    t0, out = day_start(ordinal), []
    for r in POLICE.get(ordinal, []):
        st = POLICE_STATIONS[r["district"]]
        tx, ty, d, o = float(r["lng"]), float(r["lat"]), int(r["dispatch"]), int(r["onscene"])
        c = min(int(r["close"]) if r["close"] else o + 2700, o + 2700)
        path = routing.route("P" + r["district"], tx, ty) if routing else None
        out.append([r["district"], st["lng"], st["lat"], tx, ty, d - t0, o - t0, max(c, o) - t0,
                    r["call_type"], r["priority"], path])
    return out


def day_start(ordinal):
    return int(datetime.combine(date.fromordinal(ordinal), time(0), sim.SF).timestamp())


RESP = {}  # scenario key -> {incident id: simulated response seconds}, for the location what-if

# The scenarios people click most, simulated once offline and shipped in data/build/ so they answer instantly:
# on Render's free CPU a fresh run takes 12-28 s (1.3 s on a laptop). Rebuild after any model change with
#   python3 src/backend/app.py --precompute
# The file carries a fingerprint of runtime.pkl.gz; if the model changes it is ignored instead of serving stale numbers.
PRECOMPUTED = BUILD / "scenarios.pkl.gz"
PIN_SCENARIOS = [
    ((), (), 1.0),                                      # today
    (("1", "8", "36"), (), 1.0),                        # preset: close 3 downtown stations
    ((), (), 2.0),                                      # preset: heat wave, 2x calls
    ((), (("43", "MEDIC"), ("44", "MEDIC")), 1.0),      # preset: 2 ambulances for the south
    (("44",), (), 1.0),                                 # demo: close Station 44
    ((), (("44", "MEDIC"),), 1.0),                      # demo: +1 ambulance at Station 44 (Location card button)
]


def scenario_key(closed=(), added=(), volume=1.0):
    return json.dumps([sorted(closed), sorted(added), volume])


def runtime_fingerprint():
    import hashlib
    return hashlib.sha256((BUILD / "runtime.pkl.gz").read_bytes()).hexdigest()


def load_pinned():
    try:
        with gzip.open(PRECOMPUTED, "rb") as f:
            saved = pickle.load(f)
    except FileNotFoundError:
        return {}
    if saved.get("runtime_sha") != runtime_fingerprint():
        print("precomputed scenarios are for an older model: ignoring them (rebuild with --precompute)")
        return {}
    runs, base = saved["runs"], scenario_key()
    # memory: Render's free plan has 512 MB. Only the baseline keeps its whole month of dispatches; the other presets keep
    # just the default replay day's (~1/31 of it). Asking a preset for another day re-simulates it like any scenario.
    t0 = day_start(DEFAULT_DAY)
    for k, (summary, log, resp) in list(runs.items()):
        if k != base:
            runs[k] = (summary, [d for d in log if t0 <= d["call"] < t0 + 86400], resp)
    return runs


PINNED = load_pinned()  # key -> (summary, log, responses); never evicted


def run(closed=(), added=(), volume=1.0, day=None):
    key = scenario_key(closed, added, volume)
    if key in PINNED and (day in (None, DEFAULT_DAY) or key == scenario_key()):
        summary, log, resp = PINNED[key]
        RESP[key] = resp
        return summary, log
    if key not in CACHE:
        with LOCK:
            log = []
            out = sim.simulate(RT["test"], RT["homes"], RT["model"], closed=closed, added=added, volume=volume,
                               offset=RT["offset"], log=log)
            CACHE[key] = (sim.summarize(RT["test"], out), log)
            RESP[key] = out
            if len(CACHE) > 3:  # ponytail: each run holds a month of dispatches (~20-70 MB); Render's free plan has 512 MB and the server starts at ~320
                CACHE.popitem(last=False)
    CACHE.move_to_end(key)
    for k in list(RESP):
        if k not in CACHE and k not in PINNED:
            RESP.pop(k, None)  # two request threads can reach the same stale key: del would raise KeyError
    return CACHE[key]


# ---------- "if I'm here": typical wait at a spot, by time of day and type of emergency ----------
BAND_LABEL = {0: "Night (0–6)", 1: "Morning (6–10)", 2: "Midday (10–16)", 3: "Evening (16–20)", 4: "Late (20–24)"}
SPOTS = [(i["lat"], i["lng"], i["id"], sim.band(i["t"]), i["type"]) for i in RT["test"]]


def call_kind(t):
    t = (t or "").lower()
    return "Medical" if "medical" in t else "Fire" if "fire" in t else "Alarm" if "alarm" in t else \
        "Traffic collision" if "traffic" in t else "Other"


def spread(xs):
    xs = sorted(xs)
    if not xs:
        return None
    return {"median": round(xs[len(xs) // 2]), "p90": round(xs[min(len(xs) - 1, int(len(xs) * 0.9))]),
            "over8": round(sum(x > 480 for x in xs) / len(xs), 3), "n": len(xs)}


def here(lng, lat, closed=(), added=(), volume=1.0):
    """Wait for help around (lng, lat): every held-out call within a radius (grown until >= 40 calls), simulated
    today and under the scenario, split by time of day and kind of emergency; plus the likely first responders."""
    run()
    run(closed, added, volume)
    base = RESP[json.dumps([[], [], 1.0])]
    scen = RESP[json.dumps([sorted(closed), sorted(added), volume])]
    for radius in (400, 600, 900, 1300, 1800, 2500):
        near = [s for s in SPOTS if sim.km(lat, lng, s[0], s[1]) * 1000 <= radius and s[2] in base]
        if len(near) >= 80:  # enough to split by time of day
            break
    pick = lambda resp, rows: [resp[s[2]] for s in rows if s[2] in resp]
    out = {"radius_m": radius, "calls": len(near), "is_scenario": scen is not base,
           "overall": {"today": spread(pick(base, near)), "scenario": spread(pick(scen, near))},
           "by_time": [{"band": b, "label": BAND_LABEL[b], "today": spread(pick(base, [s for s in near if s[3] == b])),
                        "scenario": spread(pick(scen, [s for s in near if s[3] == b]))} for b in range(5)],
           "by_kind": []}
    for kind in ["Medical", "Fire", "Alarm", "Traffic collision", "Other"]:
        rows = [s for s in near if call_kind(s[4]) == kind]
        if len(rows) >= 5:
            out["by_kind"].append({"kind": kind, "today": spread(pick(base, rows)), "scenario": spread(pick(scen, rows))})
    # likely first engine: the open stations whose learned drive time to this block is shortest (midday)
    c, closed_set = sim.cell(lat, lng), set(closed)
    stations = sim.load_stations()
    resp = []
    for st, (slat, slng) in stations.items():
        if st in closed_set or not st.isdigit():
            continue
        home = ("S" + st, slat, slng, "ENGINE")
        resp.append({"station": st, "drive_s": {BAND_LABEL[b]: round(RT["model"](home, c, lat, lng, b)) for b in range(5)},
                     "km": round(sim.km(lat, lng, slat, slng), 2)})
    out["responders"] = sorted(resp, key=lambda r: r["drive_s"][BAND_LABEL[2]])[:3]
    # the street route the first engine would drive (DataSF centerlines, shortest path from its station)
    if out["responders"] and routing is not None:
        path = routing.route("S" + out["responders"][0]["station"], lng, lat)
        if path and len(path) >= 2:
            out["responders"][0]["path"] = path
    return out


def first_engine_route(lng, lat, closed=(), kind="fire"):
    """Who reaches (lng, lat) first, with the street path it drives. Milliseconds: no simulation.
    fire: the open fire station whose engine the learned model says arrives first (midday).
    ambulance: the ambulance the learned model says arrives first, from its usual base (ambulances rove).
    police: the SFPD district station with the shortest estimated drive (fire/EMS distance-by-hour travel times,
    since there is no police-specific model)."""
    c, closed_set, best = sim.cell(lat, lng), set(closed), None
    if kind == "ambulance":
        for uid, h in RT["homes"].items():
            if h[3] != "MEDIC" or (h[0][0] == "S" and h[0][1:] in closed_set):
                continue
            t = RT["model"](h, c, lat, lng, 2)
            if best is None or t < best[0]:
                best = (t, h[0], uid, f"Ambulance {uid}")
    elif kind == "police":
        for d, st in POLICE_STATIONS.items():
            t = RT["model"].from_point(st["lat"], st["lng"], lat, lng, 2)
            if best is None or t < best[0]:
                best = (t, "P" + d, d, f"Police, {d.title()} station")
    else:
        kind = "fire"
        for st, (slat, slng) in sim.load_stations().items():
            if st in closed_set or not st.isdigit():
                continue
            t = RT["model"](("S" + st, slat, slng, "ENGINE"), c, lat, lng, 2)
            if best is None or t < best[0]:
                best = (t, "S" + st, st, f"Engine from Station {st}")
    if best is None:
        return {"kind": kind, "station": None, "path": None}
    path = routing.route(best[1], lng, lat) if routing is not None else None
    return {"kind": kind, "station": best[2], "label": best[3], "drive_s": round(best[0]),
            "estimate": kind == "police", "path": path if path and len(path) >= 2 else None}


def replay(log, ordinal):
    """One day of dispatches, times in seconds after local midnight. Unit timings include the
    calibration offset, so the live median matches the verdict."""
    t0 = day_start(ordinal)
    trips, calls = [], {}
    for d in log:
        if not t0 <= d["call"] < t0 + 86400:
            continue
        off = off_for(d["call"])
        dep, arr, free = d["depart"] + off - t0, d["arrive"] + off - t0, d["free"] + off - t0
        travel = arr - dep
        trips.append([d["unit"], d["type"], round(d["from"][0], 5), round(d["from"][1], 5), d["to"][0], d["to"][1],
                      round(dep), round(arr), round(free), round(free + travel), d["hood"], d["call"] - t0,
                      d["call_type"].capitalize(), street_path(d)])
        c = calls.setdefault(d["incident"], [d["to"][0], d["to"][1], d["call"] - t0, arr, d["hood"], d["call_type"].capitalize()])
        c[3] = min(c[3], arr)
    trips.sort(key=lambda r: r[6])
    return {"day": ordinal, "trips": trips, "police": police_day(ordinal),
            "calls": sorted(([*c[:3], round(c[3]), *c[4:]] for c in calls.values()), key=lambda c: c[2])}


def fleet():
    by_station = {}
    for (st, typ), n in Counter((h[0][1:], h[3]) for h in RT["homes"].values() if h[0][0] == "S").items():
        by_station.setdefault(st, {})[typ] = n
    return by_station


# Time of day: the same month of calls split into the five travel-model bands, so the page can show how waits change
# with the hour (all of October, not one day). Computed from a scenario's per-call responses and cached per scenario.
TEST_BAND = {i["id"]: sim.band(i["t"]) for i in RT["test"]}
BAND_CACHE = {}


def by_band(key):
    if key in BAND_CACHE:
        return BAND_CACHE[key]
    resp, city, hoods = RESP[key], {b: [] for b in range(5)}, {b: {} for b in range(5)}
    for i in RT["test"]:
        v = resp.get(i["id"])
        if v is not None:
            b = TEST_BAND[i["id"]]
            city[b].append(v)
            hoods[b].setdefault(i["hood"], []).append(v)
    def stats(xs):
        xs = sorted(xs)
        return {"median": round(xs[len(xs) // 2]), "over8": round(sum(x > 480 for x in xs) / len(xs), 3), "calls": len(xs)}
    out = {str(b): {"label": BAND_LABEL[b], "city": stats(city[b]), "hoods": {h: stats(v) for h, v in hoods[b].items()}}
           for b in range(5) if city[b]}
    if len(BAND_CACHE) > 48:
        BAND_CACHE.clear()
    BAND_CACHE[key] = out
    return out


def meta():
    by_station = fleet()
    summary, log = run()
    days = [{"day": d, "label": date.fromordinal(d).strftime("%a %b %-d"), "calls": n} for d, n in sorted(DAYS.items())]
    return {"period": RT["period"], "calls": len(RT["test"]), "units": len(RT["homes"]),
            "incidents_total": RT["n_incidents"], "fleet": by_station, "baseline": {**summary, "by_band": by_band(scenario_key())},
            "days": days, "replay": replay(log, DEFAULT_DAY)}


@lru_cache(maxsize=16)
def data_file_gz(name):
    return gzip.compress(data_file(name).read_bytes(), 6)


@lru_cache(maxsize=1)
def meta_payload():
    """The first-load payload never changes between deploys: build it (and its gzip) once."""
    body = json.dumps(meta()).encode()
    return body, gzip.compress(body, 6)


def warm():
    run()
    meta_payload()
    for name in DATA_FILES:
        if data_file(name).exists():
            data_file_gz(name)


# ---------- answering typed what-ifs with data ----------
KIND_MATCH = {"crash": "Traffic collision", "fire": "Fire", "medical": "Medical"}
_HOOD_C = None


def hood_centroid(name):
    global _HOOD_C
    if _HOOD_C is None:
        _HOOD_C = {}
        for f in json.load(open(SF / "hoods.geojson"))["features"]:
            g = f["geometry"]
            ring = max([g["coordinates"][0]] if g["type"] == "Polygon" else [p[0] for p in g["coordinates"]], key=len)
            _HOOD_C[f["properties"]["nhood"]] = (sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring))
    return _HOOD_C.get(name)


def mmss(sec):
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def hour_label(h):
    h = int(h) % 24
    return "midnight" if h == 0 else "noon" if h == 12 else f"{h % 12} {'AM' if h < 12 else 'PM'}"


def kind_of(i):
    return call_kind(i["type"])


def kind_stats(kind):
    """What the real October records say about this kind of call."""
    want = KIND_MATCH.get(kind)
    rows = [i for i in RT["test"] if want is None or kind_of(i) == want]
    if not rows:
        return None
    resp = sorted(i["real_response"] for i in rows)
    eng = sorted(sum(u["type"] in ("ENGINE", "TRUCK") for u in i["units"]) for i in rows)
    amb = sorted(sum(u["type"] in ("MEDIC", "PRIVATE") for u in i["units"]) for i in rows)
    scene = sorted(u["available"] - u["onscene"] for i in rows for u in i["units"][:1]
                   if u["available"] and u["onscene"] and 0 < u["available"] - u["onscene"] < 6 * 3600)
    mid = lambda xs: xs[len(xs) // 2] if xs else 0
    return {"n": len(rows), "median": mid(resp), "p90": resp[int(len(resp) * 0.9)], "engines": mid(eng),
            "ambulances": mid(amb), "scene_min": round(mid(scene) / 60) if scene else 30,
            "label": want or "all calls"}


def sf_ts(hour):
    """Timestamp for today at `hour` San Francisco time. A naive datetime would use the server's zone:
    Render runs UTC, so '5 PM' became 9-10 AM in SF and picked the wrong travel-time and calibration band."""
    return int(datetime.combine(datetime.now(sim.SF).date(), time(hour), sim.SF).timestamp())


def nearest_units(lat, lng, hour, engines, ambulances, closed=()):
    """The units most likely sent: open station engines and ambulances ranked by learned drive time at that hour."""
    c, b = sim.cell(lat, lng), sim.band(sf_ts(hour))
    ranked = sorted(((RT["model"](h, c, lat, lng, b), uid, h) for uid, h in RT["homes"].items()
                     if not (h[0][0] == "S" and h[0][1:] in set(closed))), key=lambda x: x[0])
    eng = [(t, u, h) for t, u, h in ranked if h[3] in ("ENGINE", "TRUCK")][:max(engines, 0)]
    amb = [(t, u, h) for t, u, h in ranked if h[3] in ("MEDIC", "PRIVATE")][:max(ambulances, 0)]
    first = next((t for t, u, h in ranked if h[3] in ("ENGINE", "TRUCK")), None)
    first_amb = next((t for t, u, h in ranked if h[3] in ("MEDIC", "PRIVATE")), None)
    return eng, amb, first, first_amb


TYPICAL = None


def typical_delays():
    """Median call-to-dispatch delay and turnout, from the held-out month."""
    global TYPICAL
    if TYPICAL is None:
        d = sorted(i["delay"] for i in RT["test"])
        t = sorted(u["enroute"] - u["dispatch"] for i in RT["test"] for u in i["units"][:1]
                   if u["enroute"] and 0 <= u["enroute"] - u["dispatch"] < 600)
        TYPICAL = (d[len(d) // 2], t[len(t) // 2])
    return TYPICAL



# ---------- live AI activity: every typed what-if is traced step by step for the AI activity page ----------
import threading
from collections import deque
from time import perf_counter

AI_LOG = deque(maxlen=25)
AI_TOTALS = Counter()
_TL = threading.local()


def trace_start(kind, text):
    t = {"id": f"t{int(perf_counter() * 1e6)}", "kind": kind, "text": text[:200], "started": datetime.now(sim.SF).strftime("%-I:%M:%S %p"),
         "t0": perf_counter(), "steps": [], "status": "running", "ms": None, "summary": ""}
    AI_LOG.appendleft(t)
    AI_TOTALS["requests"] += 1
    _TL.t = t
    return t


def step(agent, what, ms=None, **extra):
    t = getattr(_TL, "t", None)
    if t is not None:
        t["steps"].append({"agent": agent, "what": what, "ms": None if ms is None else round(ms),
                           "at": round((perf_counter() - t["t0"]) * 1000), **extra})


def trace_end(status, summary=""):
    t = getattr(_TL, "t", None)
    if t is not None:
        t.update(status=status, summary=summary, ms=round((perf_counter() - t["t0"]) * 1000))
        _TL.t = None


def usage_tokens(u):
    u = u or {}
    i = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    o = u.get("output_tokens", 0)
    AI_TOTALS["haiku_calls"] += 1
    AI_TOTALS["tokens_in"] += i
    AI_TOTALS["tokens_out"] += o
    return {"tokens_in": i, "tokens_out": o, "cached": u.get("cache_read_input_tokens", 0)}


def ai_activity():
    return {"totals": dict(AI_TOTALS), "traces": [{k: v for k, v in t.items() if k != "t0"} for t in AI_LOG]}

# ---------- the what-if engine: plan (typed by a person, or a proposed station) -> October re-simulated -> outcome ----------
try:
    import nl
except ImportError:  # no nl.py: typed questions are off, proposals still work
    nl = None

FOCUS_KM = 1.5


def band_of_hour(h):
    return 0 if h < 6 else 1 if h < 10 else 2 if h < 16 else 3 if h < 20 else 4


def signed_s(d):
    d = round(d)
    return "no change" if d == 0 else f"{'+' if d > 0 else '−'}{mmss(abs(d))}"


def pct(x):
    return f"{x:.0%}"


class Slowed:
    """Travel model with drive times scaled by `factor` in some hour bands (rain, traffic, an earthquake)."""
    def __init__(self, model, factor, bands):
        self.m, self.f, self.bands = model, factor, bands

    def __call__(self, home, c, lat, lng, b):
        t = self.m(home, c, lat, lng, b)
        return t * self.f if b in self.bands else t

    def from_point(self, la, ln, tla, tln, b):
        t = self.m.from_point(la, ln, tla, tln, b)
        return t * self.f if b in self.bands else t


@lru_cache(1)
def hood_names():
    return tuple(f["properties"]["nhood"] for f in json.load(open(SF / "hoods.geojson"))["features"])


def resolve_place(p, me):
    """A plan place -> (lat, lng, label, hood or None), or None."""
    p = p or {}
    if p.get("near_me") and me:
        return me["lat"], me["lng"], me.get("label") or "your address", None
    if p.get("station") and p["station"] in STATIONS:
        lat, lng = sim.load_stations()[p["station"]]
        return lat, lng, f"Station {p['station']}", None
    if p.get("hood") in hood_names():
        lat, lng = hood_centroid(p["hood"])
        return lat, lng, p["hood"], p["hood"]
    if p.get("text") and nl:
        t0 = perf_counter()
        g = nl.geocode(p["text"])
        step("Geocoder (OpenStreetMap)", f"Found “{p['text']}” at {g[0]:.4f}, {g[1]:.4f}" if g else f"Couldn’t find “{p['text']}” in San Francisco",
             (perf_counter() - t0) * 1000)
        if g:
            return g[0], g[1], g[2], None
    if p.get("lat") is not None and p.get("lng") is not None:  # a clicked point (proposals)
        return float(p["lat"]), float(p["lng"]), p.get("label") or "the chosen spot", None
    return None


def window(h0, h1):
    if h0 < 0:
        return None
    return h0, h1 if h1 > h0 else min(24, h0 + 2)


def build_run(plan, me):
    """Turn a plan into simulator arguments plus a plain list of what was simulated and map markers."""
    did, marks, notes, extra, where_when = [], [], [], [], []  # where_when: (place, hours) each lever acts on
    stations = sim.load_stations()
    closed = list(plan.get("close_stations", []))
    if closed:
        did.append(f"Closed Station{'s' * (len(closed) > 1)} {', '.join(closed)}")
        marks += [{"kind": "closed", "lat": stations[s][0], "lng": stations[s][1], "label": f"Station {s} closed"} for s in closed]
    added = [(a["station"], a["type"]) for a in plan.get("add_units", []) for _ in range(a["count"])]
    for a in plan.get("add_units", []):
        did.append(f"Added {a['count']} {'ambulance' if a['type'] == 'MEDIC' else 'engine'}{'s' * (a['count'] > 1)} at Station {a['station']}")
    new_homes = []
    for n in plan.get("new_stations", []):
        r = resolve_place(n["place"], me)
        if not r:
            notes.append("Couldn't find the new station's location on the map, so it was left out.")
            continue
        types = ["ENGINE", "MEDIC"] if n["kind"] == "fire" else ["MEDIC"]
        new_homes += [(r[0], r[1], t) for t in types]
        did.append(f"New {'fire station (an engine and an ambulance)' if n['kind'] == 'fire' else 'ambulance post'} at {r[2]}")
        where_when.append((r, None))
        marks.append({"kind": "new", "lat": r[0], "lng": r[1], "label": f"Proposed {'fire station' if n['kind'] == 'fire' else 'ambulance post'}"})
    volume, area, vhours = plan.get("call_volume", 1.0), None, None
    if abs(volume - 1) > 0.01:
        r = resolve_place(plan.get("volume_place"), me)
        vhours = window(plan.get("volume_from_hour", -1), plan.get("volume_to_hour", -1))
        if r:
            area = (r[0], r[1], FOCUS_KM)
            marks.append({"kind": "crowd", "lat": r[0], "lng": r[1], "label": f"{volume:g}x calls"})
        did.append(f"{volume:g}× the usual 911 calls" + (f" within {FOCUS_KM:g} km of {r[2]}" if r else " citywide")
                   + (f", {hour_label(vhours[0])}–{hour_label(vhours[1])}" if vhours else ", all day"))
        where_when.append((r, vhours))
    slow, bands = plan.get("slowdown", 1.0), set(range(5))
    if slow > 1.01:
        w = window(plan.get("slow_from_hour", -1), plan.get("slow_to_hour", -1))
        if w:
            bands = {band_of_hour(h) for h in range(w[0], w[1])}
        did.append(f"Driving {round((slow - 1) * 100)}% slower" + (f", {hour_label(w[0])}–{hour_label(w[1])}" if w else ", all day"))
        where_when.append((None, w))
    delay = plan.get("dispatch_delay_s", 0)
    if delay:
        did.append(f"Units sent {delay} s later than usual")
    blocked = {}
    for b in plan.get("units_busy", []):
        w = window(b["from_hour"], b["to_hour"]) or (17, 19)
        st, (slat, slng) = b["station"], stations[b["station"]]
        units = []
        if b["type"] in ("ENGINE", "ANY"):
            units += [uid for uid, h in RT["homes"].items() if h[0] == "S" + st and h[3] in ("ENGINE", "TRUCK")]
        if b["type"] in ("MEDIC", "ANY"):  # ambulances rove: take the one based nearest the station
            units.append(min((sim.km(slat, slng, h[1], h[2]), uid) for uid, h in RT["homes"].items() if h[3] == "MEDIC")[1])
        for u in units:
            blocked[u] = (w[0] * 3600, w[1] * 3600)
        if units:
            did.append(f"{', '.join(units)} out of service, {hour_label(w[0])}–{hour_label(w[1])}")
            where_when.append(((slat, slng, f"Station {st}'s area", None), w))
    first_focus = None
    for inc in plan.get("incidents", []):
        r = resolve_place(inc["place"], me) or resolve_place(plan.get("focus", {}).get("place"), me)
        if not r:
            notes.append("Couldn't place the incident on the map, so it was left out. Name a street, landmark or neighborhood.")
            continue
        ks = kind_stats(inc["kind"]) or kind_stats("other")
        hour = inc["hour"] if inc["hour"] >= 0 else 17
        dur = inc["duration_min"] if inc["duration_min"] > 0 else max(20, ks["scene_min"] + (30 if inc["kind"] == "mass_casualty" else 0))
        ne = inc["engines"] if inc["engines"] >= 0 else max(1, ks["engines"] + (2 if inc["kind"] in ("fire", "mass_casualty") else 0))
        na = inc["ambulances"] if inc["ambulances"] >= 0 else max(1, ks["ambulances"] + (2 if inc["kind"] == "mass_casualty" else 0))
        eng, amb, first, _ = nearest_units(r[0], r[1], hour, ne, na, closed)
        for _, u, _ in eng + amb:
            blocked[u] = (hour * 3600, min(24 * 3600, hour * 3600 + dur * 60))
        d0, turn = typical_delays()
        arrive = d0 + turn + (first or 0) + sim.offset_for(RT["offset"], sf_ts(hour))
        label = inc["kind"].replace("_", "-")
        did.append(f"A {label} at {r[2]}, {hour_label(hour)}, holding {', '.join(u for _, u, _ in eng + amb)} for {dur} min")
        extra.append({"k": f"First engine reaches the {label}", "v": f"~{mmss(arrive)}", "note": "typical dispatch + turnout + learned drive time"})
        marks.append({"kind": "incident", "lat": r[0], "lng": r[1], "label": f"{label.capitalize()}, {hour_label(hour)}"})
        first_focus = first_focus or (r, (hour, min(24, hour + max(1, (dur + 59) // 60))))
        where_when.append(first_focus)
    kw = {"closed": closed, "added": added, "volume": volume, "new_homes": new_homes, "volume_area": area, "volume_hours": vhours,
          "slow": slow, "bands": sorted(bands), "delay": delay, "blocked": blocked}
    # where and when the change acts: the first lever that names a place, and the first that names hours
    auto = (next((p for p, _ in where_when if p), None), next((h for _, h in where_when if h), None))
    return kw, did, marks, notes, extra, auto


PLAN_CACHE = OrderedDict()


def run_plan(kw):
    key = json.dumps(kw, sort_keys=True)
    t0, hit = perf_counter(), key in PLAN_CACHE
    if key not in PLAN_CACHE:
        travel = Slowed(RT["model"], kw["slow"], set(kw["bands"])) if kw["slow"] > 1.01 else RT["model"]
        log = []
        with LOCK:
            out = sim.simulate(RT["test"], RT["homes"], travel, closed=kw["closed"], added=kw["added"], volume=kw["volume"],
                               offset=RT["offset"], log=log, blocked=kw["blocked"] or None, new_homes=kw["new_homes"],
                               volume_area=kw["volume_area"], extra_delay=kw["delay"], volume_hours=kw["volume_hours"])
        PLAN_CACHE[key] = (out, log)
        if len(PLAN_CACHE) > 1:  # ponytail: one typed scenario at a time keeps Render's 512 MB plan from restarting
            PLAN_CACHE.popitem(last=False)
    PLAN_CACHE.move_to_end(key)
    step("Simulator", f"Replayed all {len(RT['test']):,} October calls with the plan" + (" (already cached)" if hit else ""), (perf_counter() - t0) * 1000)
    AI_TOTALS["simulations"] += 1
    return PLAN_CACHE[key]


def measure(alt, focus, hours):
    """Today vs the scenario, for the real October calls: at the focus place/time, citywide, and by neighborhood."""
    run()
    base = RESP[json.dumps([[], [], 1.0])]
    rows = [i for i in RT["test"] if i["id"] in base and i["id"] in alt]
    pair = lambda rs: ([base[i["id"]] for i in rs], [alt[i["id"]] for i in rs])
    city_b, city_a = pair(rows)
    sel = rows
    if focus:
        lat, lng, _, hood = focus
        sel = [i for i in sel if (hood and i["hood"] == hood) or (not hood and sim.km(lat, lng, i["lat"], i["lng"]) <= FOCUS_KM)]
    if hours:
        sel = [i for i in sel if hours[0] <= datetime.fromtimestamp(i["t"], sim.SF).hour < hours[1]]
    fb, fa = pair(sel)
    by = {}
    for i in rows:
        by.setdefault(i["hood"], []).append(i)
    hoods = []
    for h, rs in by.items():
        if len(rs) >= 30:
            b, a = pair(rs)
            hoods.append((spread(a)["median"] - spread(b)["median"], h, spread(b)["median"], spread(a)["median"]))
    movers = sorted((x for x in hoods if abs(x[0]) >= 3), key=lambda x: -abs(x[0]))[:5]
    slow5 = sorted(hoods, key=lambda x: -x[2])[:5]
    deltas = [a - b for b, a in zip(fb, fa)]
    slower = [d for d in deltas if d >= 30]
    faster = [d for d in deltas if d <= -30]
    sb, sa, cb, ca = spread(fb), spread(fa), spread(city_b), spread(city_a)
    return {
        "where": focus[2] if focus else "Citywide",
        "when": f"{hour_label(hours[0])}–{hour_label(hours[1])}" if hours else "all day",
        "calls": f"{len(sel):,}",
        "before": mmss(sb["median"]) if sb else "—", "after": mmss(sa["median"]) if sa else "—",
        "change": signed_s(sa["median"] - sb["median"]) if sb else "no change",
        "change_s": (sa["median"] - sb["median"]) if sb else 0,
        "over8_before": pct(sb["over8"]) if sb else "—", "over8_after": pct(sa["over8"]) if sa else "—",
        "slower_30s": f"{len(slower):,}", "faster_30s": f"{len(faster):,}",
        "slower_by": mmss(sum(slower) / len(slower)) if slower else "—", "faster_by": mmss(-sum(faster) / len(faster)) if faster else "—",
        "worst": signed_s(max(deltas)) if deltas else "no change",
        "affected_share": pct((len(slower) + len(faster)) / len(deltas)) if deltas else "0%",
        "affected_n": len(slower) - len(faster),
        "citywide": {"before": mmss(cb["median"]), "after": mmss(ca["median"]), "change": signed_s(ca["median"] - cb["median"]),
                     "over8_before": pct(cb["over8"]), "over8_after": pct(ca["over8"])},
        "neighborhoods": [{"name": h, "before": mmss(b), "after": mmss(a), "change": signed_s(d), "change_s": round(d)} for d, h, b, a in movers],
        "slowest_five": {"before": mmss(sum(x[2] for x in slow5) / 5), "after": mmss(sum(x[3] for x in slow5) / 5),
                         "names": [x[1] for x in slow5]},
    }


def template_headline(o, extra=()):
    lead = "".join(f"{f['k']}: {f['v']}. " for f in extra)
    where = o["where"][:1].upper() + o["where"][1:] + ("" if o["when"] == "all day" else f", {o['when']}")
    if o["change"] == "no change" and o["slower_30s"] != "0":
        s = f"{where}: the typical wait stays {o['after']}, but {o['slower_30s']} of {o['calls']} real calls wait 30+ s longer, by {o['slower_by']} on average."
    elif o["change"] == "no change":
        s = f"{where}: help still arrives in about {o['after']}; nearby stations absorb the change."
    else:
        n, how = (o["faster_30s"], "get help 30+ s sooner") if o["change_s"] < 0 else (o["slower_30s"], "wait 30+ s longer")
        s = f"{where}: help arrives in {o['after']} instead of {o['before']} ({o['change']}); {n} of {o['calls']} real calls {how}."
    if o["where"] != "Citywide" and o["citywide"]["change"] != "no change":
        s += f" Citywide: {o['citywide']['before']} → {o['citywide']['after']}."
    return lead + s


def simulate_plan(plan, me, question=None, day=None):
    """The full what-if: plan -> simulation -> outcome card + everything the map needs."""
    kw, did, marks, notes, extra, auto = build_run(plan, me)
    if not did:
        return {"error": "Nothing in that changes how help gets around. Try naming what happens: a crash, a closed station, more calls, slower traffic."}
    out, log = run_plan(kw)
    focus = resolve_place(plan.get("focus", {}).get("place"), me)
    hours = window(plan.get("focus", {}).get("from_hour", -1), plan.get("focus", {}).get("to_hour", -1))
    focus, hours = focus or auto[0], hours or auto[1]  # measure where and when the change actually happens
    o = measure(out, focus, hours)
    told = {**{k: v for k, v in o.items() if not k.endswith("_s") and k != "affected_n"}, "simulated": did, "facts": extra}
    head = None
    if question and nl:
        info, t0 = {}, perf_counter()
        head = nl.narrate(question, told, info)
        step("Writer (Claude Haiku)", "Wrote the answer’s first sentence from the measured outcome", (perf_counter() - t0) * 1000, **usage_tokens(info.get("usage")))
        if head:
            AI_TOTALS["verified"] += 1
            step("Number checker", f"All {info.get('checked', 0)} numbers in that sentence match the simulator’s output")
        else:
            AI_TOTALS["rejected"] += 1
            step("Number checker", f"Rejected the sentence ({', '.join(info.get('bad', [])) or 'unusable'} not in the output); used a fixed template instead")
    head = head or template_headline(o, extra)
    n = max(1, int(o["calls"].replace(",", "")))
    hit, helped = int(o["slower_30s"].replace(",", "")) / n, int(o["faster_30s"].replace(",", "")) / n
    tone = ("bad" if o["change_s"] >= 30 or hit >= 0.15 else "warn" if o["change_s"] >= 10 or hit >= 0.03
            else "good" if o["change_s"] <= -10 or helped >= 0.03 else "flat")
    summary = sim.summarize(RT["test"], out)
    return {"mode": "simulate", "title": plan.get("title") or "Your scenario", "headline": head, "tone": tone,
            "simulated": did, "assumptions": plan.get("assumptions", []) + notes, "not_modeled": plan.get("not_modeled", []),
            "outcome": o, "facts": extra, "markers": marks,
            "focus": {"lat": focus[0], "lng": focus[1], "label": focus[2]} if focus else None,
            "result": {**summary, "replay": replay(log, day if day in DAYS else DEFAULT_DAY)},
            "hours": list(hours) if hours else None,
            "basis": f"Re-ran all {len(RT['test']):,} real 911 calls from October 2026 through the simulator (validated against reality: neighborhood r = {VALIDATION_R})."}


RECORD_KIND = {"crash": "Traffic collision", "fire": "Fire", "medical": "Medical", "other": "Other"}
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def lookup(q, me, title):
    """A plain factual question: statistics straight from the real October records (no simulation)."""
    kind, place = q.get("kind", "any"), q.get("place") or {}
    r = resolve_place(place, me)
    h0, h1 = q.get("hour_from", -1), q.get("hour_to", -1)
    if h0 >= 0 and h1 in (-1, h0):
        h1 = h0 + 1
    want, rows = RECORD_KIND.get(kind), []
    for i in RT["test"]:
        dt = datetime.fromtimestamp(i["t"], sim.SF)
        if want and kind_of(i) != want:
            continue
        if r and ((r[3] and i["hood"] != r[3]) or (not r[3] and sim.km(r[0], r[1], i["lat"], i["lng"]) > FOCUS_KM)):
            continue
        if h0 >= 0 and not (h0 <= dt.hour < h1 if h1 > h0 else dt.hour >= h0 or dt.hour < h1):
            continue
        if q.get("days", "any") != "any" and (dt.weekday() >= 5) != (q["days"] == "weekend"):
            continue
        rows.append((i, dt))
    s, c = spread([i["real_response"] for i, _ in rows]), spread([i["real_response"] for i in RT["test"]])
    what = " ".join(x for x in [(want or "All").lower() + " calls" if want else "All calls", r and f"near {r[2]}",
                                h0 >= 0 and f"{hour_label(h0)}–{hour_label(h1)}", q.get("days") != "any" and f"on {q['days']}s"] if x)
    if not s:
        return {"mode": "lookup", "title": title, "headline": f"No real October calls match: {what}.", "facts": [], "groups": []}
    facts = [{"k": "Real calls", "v": f"{s['n']:,}"}, {"k": "Typical wait for help", "v": mmss(s["median"])},
             {"k": "1 in 10 waits over", "v": mmss(s["p90"])}, {"k": "Over 8 minutes", "v": pct(s["over8"])},
             {"k": "Citywide, all calls", "v": mmss(c["median"])}]
    key = {"hour": lambda i, dt: dt.hour, "neighborhood": lambda i, dt: i["hood"], "kind": lambda i, dt: kind_of(i),
           "weekday": lambda i, dt: dt.weekday(), "priority": lambda i, dt: i.get("group") or "unknown"}.get(q.get("group_by"))
    groups = []
    if key:
        g = {}
        for i, dt in rows:
            g.setdefault(key(i, dt), []).append(i["real_response"])
        items = [(k, spread(v)) for k, v in g.items() if len(v) >= 15]
        items.sort(key=(lambda kv: kv[0]) if q["group_by"] in ("hour", "weekday") else (lambda kv: -kv[1]["median"]))
        name = {"hour": hour_label, "weekday": lambda d: WEEKDAY_NAMES[d]}.get(q["group_by"], str)
        groups = [{"name": name(k), "wait": mmss(v["median"]), "median_s": v["median"], "calls": v["n"]} for k, v in items[:24]]
    head = f"{what[0].upper() + what[1:]}: help typically arrives in {mmss(s['median'])}, and {pct(s['over8'])} wait over 8 minutes."
    if groups:
        worst = max(groups, key=lambda x: x["median_s"])
        head += f" Slowest: {worst['name']} ({worst['wait']})."
    return {"mode": "lookup", "title": title, "headline": head, "facts": facts, "groups": groups,
            "focus": {"lat": r[0], "lng": r[1], "label": r[2]} if r else None,
            "basis": f"Straight from {s['n']:,} real recorded October 2026 calls (DataSF Fire Department Calls for Service); no simulation."}



VALIDATION_R = round(json.load(open(BUILD / "validation.json")).get("runnr: learned + calibrated", {}).get("hood_corr", 0.94), 2)


# ---------- residents' proposals: a new station somewhere, simulated, voted on ----------
PROPOSALS = ROOT_DIR / "data" / "proposals.json"  # ponytail: one JSON file; Render's disk resets on deploy, use a DB for real
PROP_LOCK = Lock()
KIND_LABEL = {"fire": "Fire station", "ambulance": "Ambulance post", "police": "Police station", "hospital": "Hospital"}


def load_proposals():
    try:
        return json.load(open(PROPOSALS))
    except (OSError, ValueError):
        return []


def station_plan(lat, lng, kind):
    return {"title": f"New {KIND_LABEL[kind].lower()} here", "new_stations": [{"place": {"lat": lat, "lng": lng, "label": "this spot"}, "kind": kind}],
            "focus": {"place": {"lat": lat, "lng": lng, "label": "within 1.5 km of this spot"}, "from_hour": -1, "to_hour": -1}}


def station_effect(lat, lng, kind, day=None):
    if kind not in ("fire", "ambulance"):
        return {"mode": "collect", "title": f"New {KIND_LABEL[kind].lower()} here",
                "headline": f"Medio can't simulate a {KIND_LABEL[kind].lower()} yet: its data covers fire and EMS response only. You can still submit it, and the city sees how many residents want one here."}
    return simulate_plan(station_plan(lat, lng, kind), None, None, day)


# ---------- findings: what the datasets say, computed from them ----------
BEST_SPOTS = BUILD / "best_spots.json"
DATASETS = [
    {"name": "Fire Department Calls for Service", "source": "DataSF nuek-vuh3", "use": "300,081 real 911 incidents (Nov 2024–Oct 2026): every unit's dispatch, drive and arrival time. Trains the travel model; October 2026 is held out to test it."},
    {"name": "Fire stations and police stations", "source": "DataSF wt27-4jim", "use": "Where every unit is based."},
    {"name": "Neighborhood boundaries", "source": "DataSF j2bu-swwd", "use": "Groups calls into 41 neighborhoods."},
    {"name": "Census ACS population by neighborhood", "source": "DataSF 4qbq-hvtt", "use": "Who lives where: joined to response times for the equity finding."},
    {"name": "Street centerlines", "source": "DataSF 3psu-pn9h", "use": "Draws each trip along real streets."},
    {"name": "Hospital locations and diversions", "source": "CA HHS + DataSF t4sf-777q", "use": "Where ambulances end up after transporting a patient."},
]


def corr(xs, ys):
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sx, sy = sum((x - mx) ** 2 for x in xs) ** 0.5, sum((y - my) ** 2 for y in ys) ** 0.5
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


@lru_cache(1)
def equity():
    """Real October waits by neighborhood vs who lives there (Census ACS)."""
    people = json.load(open(SF / "people.json"))
    real = {}
    for i in RT["test"]:
        real.setdefault(i["hood"], []).append(i["real_response"])
    W = "White alone, Not Hispanic or Latino"
    pts = [{"hood": h, "white": round(people[h][W] / people[h]["total"], 3), "senior": round(people[h]["age_65_plus"] / people[h]["total"], 3),
            "wait_s": spread(v)["median"], "calls": len(v)}
           for h, v in real.items() if h in people and people[h]["total"] > 2000 and len(v) >= 40]
    r_white = corr([p["white"] for p in pts], [p["wait_s"] for p in pts])
    q = sorted(pts, key=lambda p: p["white"])
    k = len(q) // 4
    low, high = q[:k], q[-k:]
    w = lambda ps: sum(p["wait_s"] * p["calls"] for p in ps) / sum(p["calls"] for p in ps)
    return {"r_white": round(r_white, 2), "n": len(pts), "points": pts,
            "least_white_quarter": {"wait": mmss(w(low)), "white": pct(sum(p["white"] for p in low) / k)},
            "most_white_quarter": {"wait": mmss(w(high)), "white": pct(sum(p["white"] for p in high) / k)},
            "gap": mmss(w(low) - w(high)),
            "slowest": [{"hood": p["hood"], "wait": mmss(p["wait_s"]), "white": pct(p["white"])} for p in sorted(pts, key=lambda p: -p["wait_s"])[:5]]}


def best_spots():
    """Try a new fire station (an engine + an ambulance) at every neighborhood's center; rank by calls helped."""
    rows = []
    for h in hood_names():
        lat, lng = hood_centroid(h)
        if not Handler.in_sf(lat, lng):
            continue
        out, _ = run_plan(build_run(station_plan(lat, lng, "fire"), None)[0])
        o = measure(out, (lat, lng, h, None), None)
        rows.append({"hood": h, "lat": round(lat, 5), "lng": round(lng, 5), "before": o["before"], "after": o["after"],
                     "change": o["change"], "change_s": o["change_s"], "helped": o["faster_30s"], "calls": o["calls"],
                     "citywide": o["citywide"]["change"], "slowest_five": o["slowest_five"]})
        PLAN_CACHE.clear()
    rows.sort(key=lambda r: (int(r["calls"].replace(",", "")) < 150, r["change_s"]))  # biggest cut where calls are common
    json.dump(rows, open(BEST_SPOTS, "w"), indent=1)
    return rows


@lru_cache(1)
def story():
    """The numbers behind the Data story tab, all computed from the datasets and the model."""
    run()
    base, test, m = RESP[json.dumps([[], [], 1.0])], RT["test"], RT["model"]
    hours = {}
    for i in test:
        hours.setdefault(datetime.fromtimestamp(i["t"], sim.SF).hour, []).append(i["real_response"])
    hist = Counter(min(int(i["real_response"] // 60), 20) for i in test)
    hoods = {}
    for i in test:
        if i["id"] in base:
            hoods.setdefault(i["hood"], ([], []))
            hoods[i["hood"]][0].append(i["real_response"])
            hoods[i["hood"]][1].append(base[i["id"]])
    val = [{"hood": h, "real": spread(r)["median"], "sim": spread(p)["median"], "calls": len(r)} for h, (r, p) in hoods.items() if len(r) >= 40]
    rows = json.load(open(BUILD / "validation.json"))
    names = {"baseline: city median": "Guess the city median", "baseline: straight-line, one speed": "Straight-line distance",
             "runnr: learned + calibrated": "Medio"}
    return {
        "data": {"incidents": f"{RT['n_incidents']:,}", "test_calls": f"{len(test):,}",
                 "trips": f"{m.n_trips:,}", "units": len(RT["homes"]), "stations": sum(st.isdigit() for st in STATIONS),
                 "cells": f"{len(m.exact):,}", "idle_min": sim.IDLE_S // 60},
        "by_hour": [{"hour": h, "calls": len(hours.get(h, [])), "wait_s": spread(hours[h])["median"] if hours.get(h) else None} for h in range(24)],
        "wait_hist": [{"min": k, "calls": hist.get(k, 0)} for k in range(21)],
        "drive": {str(b): [[round(k * sim.BIN_KM, 2), round(m.bins[(b, k)])] for k in range(0, 33) if m.bins.get((b, k))] for b in (0, 2, 3)},
        "validation": {"points": val, "r": round(corr([v["real"] for v in val], [v["sim"] for v in val]), 2),
                       "baselines": [{"name": names[k], "call_err": round(rows[k]["median_abs_err_s"]), "hood_err": round(rows[k]["hood_median_abs_err_s"])}
                                     for k in names if k in rows]},
    }


@lru_cache(1)
def breakdown():
    """By responder: fire engines, trucks, ambulances (real October records + the simulator), police (real SFPD), and by call kind."""
    _, log = run()
    med = lambda xs: mmss(sorted(xs)[len(xs) // 2]) if xs else "—"
    test, units = RT["test"], []
    for name, types in (("Fire engines", ("ENGINE",)), ("Ladder trucks", ("TRUCK",)), ("Ambulances", ("MEDIC", "PRIVATE"))):
        drive, turn, scene, n, first, trans = [], [], [], 0, 0, 0
        for i in test:
            us = [u for u in i["units"] if u["type"] in types]
            n += len(us)
            for u in us:
                if u["enroute"] and u["onscene"] and 0 < u["onscene"] - u["enroute"] < 1800:
                    drive.append(u["onscene"] - u["enroute"])
                if u["enroute"] and 0 <= u["enroute"] - u["dispatch"] < 600:
                    turn.append(u["enroute"] - u["dispatch"])
                if u["available"] and u["onscene"] and 0 < u["available"] - u["onscene"] < 6 * 3600:
                    scene.append(u["available"] - u["onscene"])
                trans += bool(u.get("transport"))
            arr = [u for u in i["units"] if u["onscene"]]
            first += bool(arr) and min(arr, key=lambda u: u["onscene"])["type"] in types
        sim_drive = [d["arrive"] - d["depart"] for d in log if d["type"] in types]
        units.append({"name": name, "fleet": sum(h[3] in types for h in RT["homes"].values()), "dispatches": f"{n:,}",
                      "turnout": med(turn), "drive_real": med(drive), "drive_sim": med(sim_drive), "on_scene": med(scene),
                      "first": pct(first / len(test)), "transport": pct(trans / n) if "MEDIC" in types and n else None})
    pol = []
    for rows in POLICE.values():
        for r in rows:
            d = int(r["onscene"]) - int(r["dispatch"])
            if 0 < d < 3 * 3600:
                pol.append((r["priority"], r["district"], r["call_type"], d))
    by = lambda k: {key: [x[3] for x in pol if x[k] == key] for key in {x[k] for x in pol}}
    pri = by(0)
    police = {"calls": f"{len(pol):,}", "median": med([x[3] for x in pol]),
              "priority": [{"name": {"A": "A: emergency", "B": "B: urgent", "C": "C: routine"}.get(k, k), "wait": med(v), "calls": f"{len(v):,}"}
                           for k, v in sorted(pri.items()) if len(v) >= 30],
              "districts": sorted(({"name": k.title(), "wait": med(v), "wait_s": sorted(v)[len(v) // 2], "calls": len(v)} for k, v in by(1).items() if len(v) >= 30),
                                  key=lambda x: -x["wait_s"]),
              "types": [{"name": k, "calls": n} for k, n in Counter(x[2] for x in pol).most_common(6)]}
    kinds = []
    for k in ["Medical", "Fire", "Alarm", "Traffic collision", "Other"]:
        xs = [i["real_response"] for i in test if kind_of(i) == k]
        if xs:
            sp = spread(xs)
            kinds.append({"name": k, "calls": f"{sp['n']:,}", "wait": mmss(sp["median"]), "over8": pct(sp["over8"])})
    return {"units": units, "police": police, "kinds": kinds}


def findings():
    v = json.load(open(BUILD / "validation.json")).get("runnr: learned + calibrated", {})
    return {"equity": equity(), "story": story(), "best_spots": json.load(open(BEST_SPOTS))[:8] if BEST_SPOTS.exists() else [],
            "datasets": DATASETS, "accuracy": {"r": VALIDATION_R, "hood_err": mmss(v.get("hood_median_abs_err_s", 16)),
                                               "call_err": mmss(v.get("median_abs_err_s", 91)), "calls": f"{v.get('incidents', 13417):,}"}}


class Handler(BaseHTTPRequestHandler):
    def reply(self, body, ctype="application/json", code=200, gz=None):
        data = body if isinstance(body, bytes) else body.encode()
        zipped = "gzip" in self.headers.get("Accept-Encoding", "") and (gz is not None or ctype == "application/json")
        if zipped:
            data = gz if gz is not None else gzip.compress(data, 5)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if zipped:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self.reply((FRONTEND / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path in ("/favicon.svg", "/favicon-32.png", "/favicon.ico", "/apple-touch-icon.png", "/icon-512.png"):
            name = "favicon-32.png" if path == "/favicon.ico" else path[1:]
            self.reply((FRONTEND / "public" / name).read_bytes(), "image/svg+xml" if name.endswith(".svg") else "image/png")
        elif path == "/api/proposals":
            ps = sorted(load_proposals(), key=lambda p: (-p["votes"], p["id"]))
            self.reply(json.dumps([{k: v for k, v in p.items() if k != "voters"} for p in ps]))
        elif path == "/api/breakdown":
            self.reply(json.dumps(breakdown()))
        elif path == "/api/ai":
            self.reply(json.dumps(ai_activity()))
        elif path == "/api/findings":
            self.reply(json.dumps(findings()))
        elif path == "/api/meta":
            body, gz = meta_payload()
            self.reply(body, gz=gz if "gzip" in self.headers.get("Accept-Encoding", "") else None)
        elif path == "/api/config":
            # Browser map key (Google Map Tiles API). It's public by design; restrict it to this site's domain in Google Cloud.
            key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip().strip('"').strip("'") or env_key("GOOGLE_MAPS_API_KEY")
            self.reply(json.dumps({"google_maps_key": key or None}))
        elif path.startswith("/data/") and Path(path).name in DATA_FILES and data_file(Path(path).name).exists():
            name = Path(path).name
            gz = data_file_gz(name) if "gzip" in self.headers.get("Accept-Encoding", "") else None
            self.reply(b"" if gz else data_file(name).read_bytes(), "application/json", gz=gz)
        else:
            self.reply("not found", "text/plain", 404)

    def do_POST(self):
        if self.path == "/api/ask":
            return self.ask()
        if self.path == "/api/station":
            return self.station()
        if self.path in ("/api/proposals", "/api/vote"):
            return self.proposals()
        if self.path not in ("/api/simulate", "/api/here", "/api/route"):
            return self.reply("not found", "text/plain", 404)
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 10_000)
            req = json.loads(self.rfile.read(n) or b"{}")
            closed = [str(s) for s in req.get("closed", []) if str(s) in STATIONS][:44]
            added = [(str(s), t) for s, t in req.get("added", []) if str(s) in STATIONS and t in TYPES][:20]
            volume = float(req.get("volume", 1.0))
            if not math.isfinite(volume):  # json.loads accepts NaN/Infinity; NaN slips through min/max and crashes sim
                raise ValueError("volume")
            volume = min(max(volume, 0.5), 3.0)
            day = int(req.get("day", DEFAULT_DAY))
            day = day if day in DAYS else DEFAULT_DAY
        except (ValueError, TypeError, AttributeError, OverflowError):  # OverflowError: {"day": 1e999}
            return self.reply('{"error": "bad request"}', code=400)
        if self.path in ("/api/here", "/api/route"):
            try:
                lng, lat = float(req["lng"]), float(req["lat"])
                if not (-122.56 < lng < -122.33 and 37.69 < lat < 37.85):
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                return self.reply('{"error": "lng/lat must be inside San Francisco"}', code=400)
            if self.path == "/api/route":  # fast: no simulation, just the first engine and its street path
                kind = req.get("kind") if req.get("kind") in ("fire", "ambulance", "police") else "fire"
                return self.reply(json.dumps(first_engine_route(lng, lat, closed, kind)))
            return self.reply(json.dumps(here(lng, lat, closed, added, volume)))
        summary, log = run(closed, added, volume, day)
        self.reply(json.dumps({**summary, "by_band": by_band(scenario_key(closed, added, volume)), "replay": replay(log, day)}))

    def body(self, limit=4_000):
        n = min(int(self.headers.get("Content-Length", 0)), limit)
        return json.loads(self.rfile.read(n) or b"{}")

    @staticmethod
    def in_sf(lat, lng):
        return 37.69 < lat < 37.85 and -122.56 < lng < -122.33

    def ask(self):
        try:
            body = self.body()
            text = str(body.get("text", ""))[:500].strip()
            me = body.get("me") if isinstance(body.get("me"), dict) else None
            if me and not self.in_sf(float(me.get("lat", 0)), float(me.get("lng", 0))):
                me = None
            day = int(body.get("day") or DEFAULT_DAY)
        except (ValueError, TypeError, AttributeError, OverflowError):
            return self.reply('{"error": "bad request"}', code=400)
        if not text:
            return self.reply('{"error": "Describe a situation first."}', code=400)
        if nl is None:
            return self.reply('{"error": "Typed what-ifs are not set up on this server."}', code=503)
        try:
            # nl._context expects {station: {"units": ...}}
            trace_start("What if", text)
            step("Planner", "Five Claude Haiku agents read your words in parallel: Router, Stations, Demand, Events, Records…")
            t0 = perf_counter()
            plan = nl.plan_scenario(text, {st: {"units": u} for st, u in fleet().items()}, (me or {}).get("label"))
            usage = plan.pop("usage", None)
            for ag in plan.pop("agents", []):
                o = ag.get("out") or {}
                what = ag["error"] and f"Failed: {ag['error']}" or {
                    "Router": lambda: f"Mode {o.get('mode')}, title “{o.get('title')}”",
                    "Stations": lambda: ", ".join(x for x in [o.get("close_stations") and f"close {', '.join(o['close_stations'])}",
                                                               o.get("add_units") and f"{len(o['add_units'])} unit change(s)",
                                                               o.get("new_stations") and f"{len(o['new_stations'])} new station(s)"] if x) or "no station changes",
                    "Demand": lambda: ", ".join(x for x in [o.get("call_volume", 1) != 1 and f"{o['call_volume']:g}x calls",
                                                             o.get("slowdown", 1) != 1 and f"driving x{o['slowdown']:g}",
                                                             o.get("dispatch_delay_s") and f"+{o['dispatch_delay_s']} s dispatch"] if x) or "no demand changes",
                    "Events": lambda: f"{len(o.get('incidents', []))} incident(s), {len(o.get('units_busy', []))} busy unit group(s)",
                    "Records": lambda: f"filters: {(o.get('lookup') or {}).get('kind', 'any')}, group by {(o.get('lookup') or {}).get('group_by', 'none')}",
                }[ag["name"]]()
                step(f"{ag['name']} agent (Claude Haiku)", what, ag["ms"], **usage_tokens(ag.get("usage")))
            if "error" in plan:
                step("Planner (Claude Haiku)", plan["error"], (perf_counter() - t0) * 1000)
                trace_end("error", plan["error"])
                return self.reply(json.dumps(plan), code=400)
            levers = [k for k in ("close_stations", "add_units", "new_stations", "units_busy", "incidents") if plan.get(k)]
            levers += [k for k, neutral in (("call_volume", 1.0), ("slowdown", 1.0), ("dispatch_delay_s", 0)) if plan.get(k) != neutral]
            step("Planner", f"Merged plan “{plan['title']}” ({plan['mode']}): " + (", ".join(x.replace("_", " ") for x in levers) or "no levers"),
                 (perf_counter() - t0) * 1000)
            if plan["mode"] == "off_topic":
                trace_end("done", "Off topic")
                return self.reply(json.dumps({"error": "Medio answers questions about 911 fire and ambulance response in San Francisco. Try: what if there's a big crash on 6th Street at rush hour?"}), code=400)
            if plan["mode"] == "lookup":
                t0 = perf_counter()
                out = lookup(plan["lookup"], me, plan["title"])
                step("Records", "Queried the real October records directly (no simulation)", (perf_counter() - t0) * 1000)
            else:
                out = simulate_plan(plan, me, text, day)
            trace_end("error" if "error" in out else "done", out.get("error") or out.get("headline", ""))
            self.reply(json.dumps(out), code=400 if "error" in out else 200)
        except Exception as e:  # model or network failure: tell the page, don't crash the server
            trace_end("error", type(e).__name__)
            self.reply(json.dumps({"error": f"That one didn't work ({type(e).__name__}). Try rephrasing."}), code=400)

    def station(self):
        try:
            b = self.body()
            lat, lng, kind = float(b["lat"]), float(b["lng"]), str(b.get("kind", "fire"))
            if not self.in_sf(lat, lng) or kind not in KIND_LABEL:
                raise ValueError
            day = int(b.get("day") or DEFAULT_DAY)
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            return self.reply('{"error": "Pick a spot inside San Francisco."}', code=400)
        trace_start("Proposal preview", f"New {KIND_LABEL[kind].lower()} at {lat:.4f}, {lng:.4f}")
        out = station_effect(lat, lng, kind, day)
        trace_end("done", out.get("headline", ""))
        self.reply(json.dumps(out))

    def proposals(self):
        try:
            b = self.body()
            if self.path == "/api/vote":
                pid, client = str(b["id"]), str(b.get("client", ""))[:40]
                with PROP_LOCK:
                    ps = load_proposals()
                    p = next((p for p in ps if p["id"] == pid), None)
                    if not p:
                        return self.reply('{"error": "That proposal is gone."}', code=404)
                    if client and client not in p["voters"]:
                        p["voters"].append(client)
                        p["votes"] = len(p["voters"])
                    json.dump(ps, open(PROPOSALS, "w"))
                return self.reply(json.dumps({"id": pid, "votes": p["votes"]}))
            lat, lng, kind = float(b["lat"]), float(b["lng"]), str(b.get("kind", "fire"))
            if not self.in_sf(lat, lng) or kind not in KIND_LABEL:
                raise ValueError
            note, name = str(b.get("note", "")).strip()[:280], str(b.get("name", "")).strip()[:40]
            client = str(b.get("client", ""))[:40]
        except (KeyError, ValueError, TypeError, AttributeError):
            return self.reply('{"error": "Pick a spot inside San Francisco first."}', code=400)
        eff = station_effect(lat, lng, kind)
        o = eff.get("outcome")
        hood = min(hood_names(), key=lambda h: sim.km(lat, lng, *hood_centroid(h)))
        rec = {"id": f"p{int(datetime.now().timestamp() * 1000)}", "lat": round(lat, 5), "lng": round(lng, 5), "kind": kind,
               "kind_label": KIND_LABEL[kind], "hood": hood, "note": note, "name": name, "voters": [client] if client else [],
               "votes": 1 if client else 0, "created": datetime.now(sim.SF).strftime("%b %-d, %-I:%M %p"),
               "effect": {"before": o["before"], "after": o["after"], "change": o["change"], "calls": o["calls"],
                          "citywide": o["citywide"]["change"]} if o else None}
        with PROP_LOCK:
            ps = load_proposals()
            if len(ps) >= 500:
                return self.reply('{"error": "The proposal board is full for this demo."}', code=429)
            ps.append(rec)
            PROPOSALS.parent.mkdir(exist_ok=True)
            json.dump(ps, open(PROPOSALS, "w"))
        self.reply(json.dumps({k: v for k, v in rec.items() if k != "voters"}))

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    import sys
    if "--precompute" in sys.argv:  # offline: ~10 s on a laptop, writes data/build/scenarios.pkl.gz
        runs = {}
        for closed, added, volume in PIN_SCENARIOS:
            log = []
            out = sim.simulate(RT["test"], RT["homes"], RT["model"], closed=closed, added=added, volume=volume,
                               offset=RT["offset"], log=log)
            runs[scenario_key(closed, added, volume)] = (sim.summarize(RT["test"], out), log, out)
            print(f"precomputed closed={list(closed)} added={list(added)} volume={volume}")
        with gzip.open(PRECOMPUTED, "wb", 6) as f:
            pickle.dump({"runtime_sha": runtime_fingerprint(), "runs": runs}, f, protocol=4)
        print(f"-> {PRECOMPUTED} ({PRECOMPUTED.stat().st_size / 1e6:.1f} MB)")
        sys.exit()
    if "--best-spots" in sys.argv:  # offline: ~1 min, writes data/build/best_spots.json
        for r in best_spots()[:8]:
            print(f"{r['hood']:32s} {r['before']} -> {r['after']} ({r['change']}), {r['helped']} of {r['calls']} calls 30+ s faster, city {r['citywide']}")
        sys.exit()
    port = int(os.environ.get("PORT", 8000))
    warm()  # build the model run, page payload and gzipped data files before taking traffic
    print(f"Medio on http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
