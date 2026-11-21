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


def run(closed=(), added=(), volume=1.0):
    key = json.dumps([sorted(closed), sorted(added), volume])
    if key not in CACHE:
        with LOCK:
            log = []
            out = sim.simulate(RT["test"], RT["homes"], RT["model"], closed=closed, added=added, volume=volume,
                               offset=RT["offset"], log=log)
            CACHE[key] = (sim.summarize(RT["test"], out), log)
            RESP[key] = out
            if len(CACHE) > 24:  # ponytail: tiny LRU, raise if demo traffic grows
                CACHE.popitem(last=False)
    CACHE.move_to_end(key)
    for k in list(RESP):
        if k not in CACHE:
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
    return out


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


def meta():
    by_station = fleet()
    summary, log = run()
    days = [{"day": d, "label": date.fromordinal(d).strftime("%a %b %-d"), "calls": n} for d, n in sorted(DAYS.items())]
    return {"period": RT["period"], "calls": len(RT["test"]), "units": len(RT["homes"]),
            "incidents_total": RT["n_incidents"], "fleet": by_station, "baseline": summary,
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


BLOCK_CACHE = OrderedDict()


def blocked_run(block):
    key = json.dumps(sorted(block.items()))
    if key not in BLOCK_CACHE:
        with LOCK:
            BLOCK_CACHE[key] = sim.simulate(RT["test"], RT["homes"], RT["model"], offset=RT["offset"], blocked=block)
        if len(BLOCK_CACHE) > 16:
            BLOCK_CACHE.popitem(last=False)
    return BLOCK_CACHE[key]


def window_effect(block, lat, lng, h0, h1, radius_km=2.5, area=None):
    """Calls near (lat, lng) (or in a station's first-due area) during [h0, h1) local: wait today vs with the block."""
    run()
    base, alt = RESP[json.dumps([[], [], 1.0])], blocked_run(block)
    near = []
    for i in RT["test"]:
        hr = datetime.fromtimestamp(i["t"], sim.SF).hour
        if not (h0 <= hr < h1) or i["id"] not in base:
            continue
        if (area and i.get("area") == area) or sim.km(lat, lng, i["lat"], i["lng"]) <= radius_km:
            near.append(i["id"])
    if not near:
        return None
    b, a = sorted(base[x] for x in near), sorted(alt[x] for x in near)
    deltas = sorted(alt[x] - base[x] for x in near)
    hit = sum(d > 15 for d in deltas)
    return {"n": len(near), "base": b[len(b) // 2], "alt": a[len(a) // 2], "worst": deltas[-1],
            "hit": hit, "over8_base": sum(x > 480 for x in b) / len(b), "over8_alt": sum(x > 480 for x in a) / len(a)}


def answer(q, me=None):
    """Turn a parsed question into an answer made of simulator/record numbers. The AI only wrote q."""
    intent, facts, assume = q["intent"], [], []
    out = {"intent": intent, "read_as": q["explanation"]}
    if intent == "scenario":
        out.update({k: q[k] for k in ("closed", "added", "volume")})
        return out
    if intent == "incident":
        inc = q["incident"]
        ks = kind_stats(inc["kind"]) or kind_stats("other")
        if inc["near_me"] and me:
            lat, lng, place = me["lat"], me["lng"], me.get("label") or "your address"
        elif inc["hood"]:
            (lat, lng), place = hood_centroid(inc["hood"]), inc["hood"]
        elif inc["station"]:
            lat, lng = sim.load_stations()[inc["station"]]
            place = f"Station {inc['station']}"
        elif me:
            lat, lng, place = me["lat"], me["lng"], me.get("label") or "your address"
            assume.append("No place was named, so I used the address you looked up.")
        else:
            out["headline"] = (f"{ks['label'].capitalize()} calls: help typically arrives in {mmss(ks['median'])} "
                               f"(1 in 10 waits over {mmss(ks['p90'])}). Tell me where, or look up an address, and I'll simulate it.")
            out["facts"] = [{"k": f"{ks['label']} calls in October", "v": f"{ks['n']:,}"},
                            {"k": "Typical wait for help", "v": mmss(ks["median"])},
                            {"k": "Units usually sent", "v": f"{ks['engines']} engine, {ks['ambulances']} ambulance"},
                            {"k": "Typical time on scene", "v": f"{ks['scene_min']} min"}]
            return out
        hour = inc["hour"] if inc["hour"] >= 0 else 17
        if inc["hour"] < 0:
            assume.append("No time was given, so I used 5 PM, the evening rush.")
        dur = inc["duration_min"] if inc["duration_min"] > 0 else max(20, ks["scene_min"] + (30 if inc["kind"] == "mass_casualty" else 0))
        if inc["duration_min"] < 0:
            assume.append(f"Scene time {dur} min: the typical time units spend at {ks['label'].lower()} calls in the data.")
        ne = inc["engines"] if inc["engines"] >= 0 else max(1, ks["engines"] + (2 if inc["kind"] in ("fire", "mass_casualty") else 0))
        na = inc["ambulances"] if inc["ambulances"] >= 0 else max(1, ks["ambulances"] + (2 if inc["kind"] == "mass_casualty" else 0))
        if inc["engines"] < 0 or inc["ambulances"] < 0:
            assume.append(f"{ne} engine{'s' * (ne != 1)} and {na} ambulance{'s' * (na != 1)} tied up, as SF usually sends to this kind of call.")
        eng, amb, first, first_amb = nearest_units(lat, lng, hour, ne, na)
        delay, turnout = typical_delays()
        block = {u: (hour * 3600, hour * 3600 + dur * 60) for _, u, _ in eng + amb}
        eff = window_effect(block, lat, lng, hour, min(24, hour + (dur + 59) // 60))
        arrive = delay + turnout + (first or 0) + sim.offset_for(RT["offset"], sf_ts(hour))
        out["focus"] = {"lat": lat, "lng": lng, "label": place}
        facts.append({"k": "First engine reaches the scene", "v": f"~{mmss(arrive)}", "note": "dispatch + turnout + learned drive time"})
        facts.append({"k": "Units tied up", "v": ", ".join(u for _, u, _ in eng + amb)})
        if eff:
            d = eff["alt"] - eff["base"]
            tone = "bad" if d >= 60 else "warn" if d >= 15 else "ok"
            facts.append({"k": f"Other calls within 2.5 km, {hour_label(hour)}–{hour_label(hour + (dur + 59) // 60)}",
                          "v": f"{mmss(eff['base'])} → {mmss(eff['alt'])}", "tone": tone,
                          "note": f"{eff['n']} real calls over October; {eff['hit']} wait 15+ s longer, worst +{mmss(max(0, eff['worst']))}"})
            facts.append({"k": "Those calls over 8 min", "v": f"{eff['over8_base']:.0%} → {eff['over8_alt']:.0%}"})
            out["headline"] = (f"A {inc['kind'].replace('_', '-')} near {place} at {hour_label(hour)}: the first engine arrives in about "
                               f"{mmss(arrive)}. While {len(block)} units are tied up for {dur} min, other calls nearby wait "
                               f"{'about the same' if abs(d) < 15 else f'{mmss(abs(d))} longer' if d > 0 else f'{mmss(-d)} less'} "
                               f"(typical {mmss(eff['base'])} → {mmss(eff['alt'])}).")
        else:
            out["headline"] = f"A {inc['kind']} near {place} at {hour_label(hour)}: the first engine arrives in about {mmss(arrive)}."
    elif intent == "units_busy":
        block, places = {}, []
        h0 = next((b["from_hour"] for b in q["busy"] if b["from_hour"] >= 0), 17)
        h1 = next((b["to_hour"] for b in q["busy"] if b["to_hour"] > h0), h0 + 2)
        if all(b["from_hour"] < 0 for b in q["busy"]):
            assume.append(f"No time was given, so I used {hour_label(h0)}–{hour_label(h1)}.")
        stations = sim.load_stations()
        for b in q["busy"] or []:
            st = b["station"]
            slat, slng = stations[st]
            for uid, h in RT["homes"].items():
                if h[0] == "S" + st and b["type"] in ("ENGINE", "ANY") and h[3] in ("ENGINE", "TRUCK"):
                    block[uid] = (h0 * 3600, h1 * 3600)
            if b["type"] in ("MEDIC", "ANY"):  # ambulances rove: take the one based nearest this station
                med = min(((sim.km(slat, slng, h[1], h[2]), uid) for uid, h in RT["homes"].items() if h[3] == "MEDIC"))
                block[med[1]] = (h0 * 3600, h1 * 3600)
                assume.append(f"Ambulances aren't tied to stations, so I took {med[1]}, the one based nearest Station {st}.")
            places.append((st, slat, slng))
        if not block:
            out["headline"] = "I couldn't tell which units you mean. Name a station, like: the engine at Station 43 is busy from 5 to 7 PM."
            return out
        st, lat, lng = places[0]
        eff = window_effect(block, lat, lng, h0, h1, radius_km=2.0, area=st)
        out["focus"] = {"lat": lat, "lng": lng, "label": f"Station {st}"}
        facts.append({"k": "Out of service", "v": ", ".join(block), "note": f"{hour_label(h0)}–{hour_label(h1)}, every day of October"})
        if eff:
            d = eff["alt"] - eff["base"]
            tone = "bad" if d >= 60 else "warn" if d >= 15 else "ok"
            facts.append({"k": f"Calls in Station {st}'s area, {hour_label(h0)}–{hour_label(h1)}", "v": f"{mmss(eff['base'])} → {mmss(eff['alt'])}",
                          "tone": tone, "note": f"{eff['n']} real calls; {eff['hit']} wait 15+ s longer, worst +{mmss(max(0, eff['worst']))}"})
            facts.append({"k": "Those calls over 8 min", "v": f"{eff['over8_base']:.0%} → {eff['over8_alt']:.0%}"})
            out["headline"] = (f"With {', '.join(block)} out from {hour_label(h0)} to {hour_label(h1)}, calls in Station {st}'s area wait "
                               f"{'about the same' if abs(d) < 15 else f'{mmss(abs(d))} longer' if d > 0 else f'{mmss(-d)} less'} "
                               f"({mmss(eff['base'])} → {mmss(eff['alt'])}); neighbors cover the rest.")
        else:
            out["headline"] = f"No calls in that window near Station {st} in October, so there's nothing to measure."
    elif intent == "info":
        info, (summary, _) = q["info"], run()
        if info["topic"] == "call_kind":
            ks = kind_stats(info["kind"]) or kind_stats("other")
            out["headline"] = f"{ks['label'].capitalize()} calls: help typically arrives in {mmss(ks['median'])}; 1 in 10 waits over {mmss(ks['p90'])}."
            facts += [{"k": "Calls in October", "v": f"{ks['n']:,}"}, {"k": "Units usually sent", "v": f"{ks['engines']} engine, {ks['ambulances']} ambulance"},
                      {"k": "Typical time on scene", "v": f"{ks['scene_min']} min"}]
        elif info["topic"] == "neighborhood" and info["hood"] in summary["hoods"]:
            h = summary["hoods"][info["hood"]]
            rank = sorted(summary["hoods"], key=lambda n: -summary["hoods"][n]["median"]).index(info["hood"]) + 1
            lat, lng = hood_centroid(info["hood"])
            hr = here(lng, lat)
            slow = max((b for b in hr["by_time"] if b["today"]), key=lambda b: b["today"]["median"])
            out["focus"] = {"lat": lat, "lng": lng, "label": info["hood"]}
            out["headline"] = (f"{info['hood']}: help typically arrives in {mmss(h['median'])} ({'slower' if h['median'] > summary['city']['median'] else 'faster'} "
                               f"than the city's {mmss(summary['city']['median'])}); #{rank} slowest of {len(summary['hoods'])}.")
            facts += [{"k": "Calls over 8 min", "v": f"{h['over8']:.0%}"}, {"k": "Slowest time of day", "v": f"{slow['label']}: {mmss(slow['today']['median'])}"},
                      {"k": "Likely first engine", "v": f"Station {hr['responders'][0]['station']}"}]
        elif info["topic"] == "station" and info["station"]:
            st = info["station"]
            rows = [i for i in RT["test"] if i.get("area") == st]
            units = [u for u, h in RT["homes"].items() if h[0] == "S" + st]
            if rows:
                r = sorted(i["real_response"] for i in rows)
                busiest = max(range(24), key=lambda hh: sum(datetime.fromtimestamp(i["t"], sim.SF).hour == hh for i in rows))
                out["headline"] = f"Station {st} is first-due for {len(rows):,} calls in October; help there typically arrives in {mmss(r[len(r) // 2])}."
                facts += [{"k": "Units based here", "v": ", ".join(units) or "none"}, {"k": "Busiest hour", "v": hour_label(busiest)}]
                lat, lng = sim.load_stations()[st]
                out["focus"] = {"lat": lat, "lng": lng, "label": f"Station {st}"}
            else:
                out["headline"] = f"Station {st} has no first-due calls in October's records."
        else:
            c = summary["city"]
            out["headline"] = f"Citywide, help typically arrives in {mmss(c['median'])}; {c['over8']:.0%} of calls wait over 8 minutes."
    else:
        out["headline"] = ("I can only answer questions about 911 response in San Francisco. Try: what if there's a crash near me at 5 PM, "
                           "what if Station 43's engine is busy, or how fast do fires get help.")
    out["facts"], out["assumptions"] = facts, assume
    return out


# ---------- tools the question agent can call (nl.agent): every number it says comes from one of these ----------
RECORD_KIND = {"crash": "Traffic collision", "fire": "Fire", "medical": "Medical", "other": "Other"}
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _hour(v, lo=0, hi=23):
    return int(v) if isinstance(v, (int, float)) and lo <= v <= hi else -1


def _pct(x):
    return f"{x:.0%}"


def _stats(xs):
    s = spread(xs)
    return s and {"calls": f"{s['n']:,}", "typical wait": mmss(s["median"]), "1 in 10 waits over": mmss(s["p90"]),
                  "over 8 min": _pct(s["over8"])}


def records_tool(a, me, state):
    kind, hood, st = a.get("kind", "any"), a.get("hood") or "", str(a.get("station") or "")
    h0, h1 = _hour(a.get("hour_from")), _hour(a.get("hour_to"), 0, 24)
    if h0 >= 0 and h1 in (-1, h0):
        h1 = h0 + 1
    days, group = a.get("days", "any"), a.get("group_by", "none")
    near = me if a.get("near_me") and me else None
    if hood in hood_names():
        lat, lng = hood_centroid(hood)
        state.setdefault("focus", {"lat": lat, "lng": lng, "label": hood})
    want, rows = RECORD_KIND.get(kind), []
    for i in RT["test"]:
        dt = datetime.fromtimestamp(i["t"], sim.SF)
        if want and kind_of(i) != want or hood and i["hood"] != hood or st and i.get("area") != st:
            continue
        if h0 >= 0 and not (h0 <= dt.hour < h1 if h1 > h0 else dt.hour >= h0 or dt.hour < h1):
            continue
        if days != "any" and (dt.weekday() >= 5) != (days == "weekend"):
            continue
        if near and sim.km(near["lat"], near["lng"], i["lat"], i["lng"]) > 1.5:
            continue
        rows.append((i, dt))
    label = " · ".join(x for x in [want or ("all calls" if kind != "mass_casualty" else "all calls (no mass-casualty tag in records)"),
                                   hood, st and f"Station {st}'s area", near and "within 1.5 km of your address",
                                   h0 >= 0 and f"{hour_label(h0)}–{hour_label(h1)}", days != "any" and days + "s"] if x)
    out = {"_label": f"Real October records: {label}", "source": "real recorded arrival times, not the simulator", "filter": label,
           "result": _stats([i["real_response"] for i, _ in rows]) or "no matching calls",
           "citywide, all calls": _stats([i["real_response"] for i in RT["test"]])}
    key = {"hour": lambda i, dt: dt.hour, "neighborhood": lambda i, dt: i["hood"], "kind": lambda i, dt: kind_of(i),
           "weekday": lambda i, dt: dt.weekday(), "priority": lambda i, dt: i.get("group") or "unknown"}.get(group)
    if key and rows:
        g = {}
        for i, dt in rows:
            g.setdefault(key(i, dt), []).append(i["real_response"])
        items = [(k, v) for k, v in g.items() if len(v) >= 15]
        items.sort(key=(lambda kv: kv[0]) if group in ("hour", "weekday") else (lambda kv: -spread(kv[1])["median"]))
        name = {"hour": hour_label, "weekday": lambda d: WEEKDAY_NAMES[d]}.get(group, str)
        out["by " + group] = [{"name": name(k), **_stats(v)} for k, v in items[:24]]
    return out


def simulate_tool(a, state):
    valid = set(sim.load_stations())
    closed = sorted({str(x) for x in a.get("closed", []) if str(x) in valid})[:20]
    added = [[str(x.get("station")), x.get("type")] for x in a.get("added", [])
             if str(x.get("station")) in valid and x.get("type") in ("MEDIC", "ENGINE")][:20]
    volume = min(max(float(a.get("volume") or 1.0), 0.5), 3.0)
    (b, _), (r, _) = run(), run(closed, added, volume)
    state["scenario"] = {"closed": closed, "added": added, "volume": volume}
    moved = sorted(((n, b["hoods"][n]["median"], r["hoods"][n]["median"]) for n in r["hoods"] if n in b["hoods"]),
                   key=lambda x: -abs(x[2] - x[1]))
    desc = ", ".join(x for x in [closed and "close " + ", ".join(closed), added and "add " + ", ".join(f"{t.lower()} at {s}" for s, t in added),
                                 volume != 1 and f"{volume:g}× calls"] if x) or "no change"
    return {"_label": f"Simulated all of October: {desc}", "applied": desc,
            "citywide typical wait": f"{mmss(b['city']['median'])} → {mmss(r['city']['median'])}",
            "citywide change": signed_s(r["city"]["median"] - b["city"]["median"]),
            "calls over 8 min": f"{_pct(b['city']['over8'])} → {_pct(r['city']['over8'])}",
            "neighborhoods that change most": [{"name": n, "wait": f"{mmss(x)} → {mmss(y)}", "change": signed_s(y - x)} for n, x, y in moved[:6]]}


def signed_s(d):
    d = round(d)
    return "no change" if d == 0 else f"{'+' if d > 0 else '−'}{mmss(abs(d))}"


def engine_tool(intent, a, me, state):
    """Wrap the existing answer() branches as tools."""
    valid, hoods = set(sim.load_stations()), set(hood_names())
    if intent == "incident":
        q = {"incident": {"kind": a.get("kind", "other"), "hood": a.get("hood") if a.get("hood") in hoods else "",
                          "station": str(a.get("station") or "") if str(a.get("station") or "") in valid else "",
                          "near_me": bool(a.get("near_me")), "hour": _hour(a.get("hour")),
                          "duration_min": _hour(a.get("duration_min"), 5, 600), "engines": _hour(a.get("engines"), 0, 6),
                          "ambulances": _hour(a.get("ambulances"), 0, 6)}}
    elif intent == "units_busy":
        q = {"busy": [{"station": str(b.get("station")), "type": b.get("type", "ANY"), "from_hour": _hour(b.get("from_hour")),
                       "to_hour": _hour(b.get("to_hour"), 0, 24)} for b in a.get("busy", []) if str(b.get("station")) in valid][:6]}
    else:
        if a.get("near_me") and me:
            hr = here(me["lng"], me["lat"])
            state.setdefault("focus", {"lat": me["lat"], "lng": me["lng"], "label": me.get("label") or "your address"})
            return {"_label": "Profile of your address", "calls within " + f"{hr['radius_m']} m": hr["calls"],
                    "typical wait": mmss(hr["overall"]["today"]["median"]), "over 8 min": _pct(hr["overall"]["today"]["over8"]),
                    "by time of day": {t["label"]: mmss(t["today"]["median"]) for t in hr["by_time"] if t["today"]},
                    "by kind": {k["kind"]: mmss(k["today"]["median"]) for k in hr["by_kind"]},
                    "likely first engines": [f"Station {r['station']} ({r['km']} km)" for r in hr["responders"]]}
        st = str(a.get("station") or "")
        topic = "neighborhood" if a.get("hood") in hoods else "station" if st in valid else "citywide"
        q = {"info": {"topic": topic, "kind": "", "hood": a.get("hood") if topic == "neighborhood" else "", "station": st if topic == "station" else ""}}
    q.update(intent="info" if intent == "place" else intent, explanation="")
    r = answer(q, me)
    if r.get("focus"):
        state.setdefault("focus", r["focus"])  # the first place the answer is about
    return {"_label": {"incident": "Simulated the incident on every October day", "units_busy": "Simulated the units out of service",
                       "place": "Profiled the place"}[intent],
            "summary": r.get("headline", ""), "facts": r.get("facts", []), "assumptions": r.get("assumptions", [])}


@lru_cache(1)
def hood_names():
    return tuple(f["properties"]["nhood"] for f in json.load(open(SF / "hoods.geojson"))["features"])


def tool_handlers(me, state):
    return {"records": lambda a: records_tool(a, me, state), "simulate_city": lambda a: simulate_tool(a, state),
            "incident": lambda a: engine_tool("incident", a, me, state), "units_busy": lambda a: engine_tool("units_busy", a, me, state),
            "place_profile": lambda a: engine_tool("place", a, me, state)}


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
        if self.path not in ("/api/simulate", "/api/here"):
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
        if self.path == "/api/here":
            try:
                lng, lat = float(req["lng"]), float(req["lat"])
                if not (-122.56 < lng < -122.33 and 37.69 < lat < 37.85):
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                return self.reply('{"error": "lng/lat must be inside San Francisco"}', code=400)
            return self.reply(json.dumps(here(lng, lat, closed, added, volume)))
        summary, log = run(closed, added, volume)
        self.reply(json.dumps({**summary, "replay": replay(log, day)}))

    def ask(self):
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 4_000)
            body = json.loads(self.rfile.read(n) or b"{}")
            text = str(body.get("text", ""))[:500].strip()
            me = body.get("me") if isinstance(body.get("me"), dict) else None
            if me and not (-122.56 < float(me.get("lng", 0)) < -122.33 and 37.69 < float(me.get("lat", 0)) < 37.85):
                me = None
        except (ValueError, TypeError, AttributeError):
            return self.reply('{"error": "bad request"}', code=400)
        if not text:
            return self.reply('{"error": "Type a question first."}', code=400)
        try:
            import nl
        except ImportError:
            return self.reply('{"error": "Typed questions are not set up on this server."}', code=503)
        try:
            state = {}
            # nl._context expects {station: {"units": ...}} (Arshdeep's audit fix: bare fleet() listed units as "unknown")
            r = nl.agent(text, {st: {"units": u} for st, u in fleet().items()}, tool_handlers(me, state), (me or {}).get("label"))
            usage = r.pop("usage", None)
            if usage:
                print(f"ask: {usage}")
            if "error" in r:
                return self.reply(json.dumps(r), code=400)
            r.update(state)  # focus to fly to, scenario for the map
            self.reply(json.dumps(r))
        except Exception as e:  # model or network failure: tell the page, don't crash the server
            self.reply(json.dumps({"error": f"Couldn't read that question: {e}"}), code=400)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    warm()  # build the model run, page payload and gzipped data files before taking traffic
    print(f"Medio on http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
