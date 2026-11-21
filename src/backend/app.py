"""Medio web server. python3 app.py -> http://localhost:8000 (Render sets $PORT).

GET  /                 the app (web/index.html)
GET  /data/<file>      stations.json, hoods.geojson, people.json, validation.json, buildings.json
GET  /api/meta         fleet, period, replay days, baseline results + replay of the default day
POST /api/simulate     {"closed": [stations], "added": [[station, "MEDIC"|"ENGINE"]], "volume": 1.0, "day": <ordinal>}
                       -> neighborhood results for the whole held-out month + a replay of one day
POST /api/ask          {"text": "close the two stations nearest Bayview"} -> scenario (needs nl.py + ANTHROPIC_API_KEY)
JSON and /data files are gzip-compressed when the browser accepts it.
"""
import csv
import gzip
import json
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
            del RESP[k]
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
            volume = min(max(float(req.get("volume", 1.0)), 0.5), 3.0)
            day = int(req.get("day", DEFAULT_DAY))
            day = day if day in DAYS else DEFAULT_DAY
        except (ValueError, TypeError, AttributeError):
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
            text = str(json.loads(self.rfile.read(n) or b"{}").get("text", ""))[:500].strip()
        except (ValueError, TypeError, AttributeError):
            return self.reply('{"error": "bad request"}', code=400)
        if not text:
            return self.reply('{"error": "Type a question first."}', code=400)
        try:
            import nl
        except ImportError:
            return self.reply('{"error": "Typed questions are not set up on this server."}', code=503)
        try:
            out = nl.parse_scenario(text, fleet())  # {station: {type: count}}; nl fills in addresses
            usage = out.pop("usage", None)
            if usage:
                print(f"ask: {usage}")
            self.reply(json.dumps(out), code=400 if "error" in out else 200)
        except Exception as e:  # model or network failure: tell the page, don't crash the server
            self.reply(json.dumps({"error": f"Couldn't read that question: {e}"}), code=400)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    warm()  # build the model run, page payload and gzipped data files before taking traffic
    print(f"Medio on http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
