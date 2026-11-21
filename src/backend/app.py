"""Runnr web server. python3 app.py -> http://localhost:8000 (Render sets $PORT).

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
OFF = round(RT["offset"])
DATA_FILES = {"stations.json", "hoods.geojson", "people.json", "validation.json", "buildings.json", "police_stations.json", "hospitals.json"}
# added units (NEW*) start at a station; find its key from the start coordinates in the sim log
STATION_AT = {(round(lng, 5), round(lat, 5)): "S" + k for k, (lat, lng) in sim.load_stations().items()}
try:
    import routing
    routing._load()
except Exception as e:  # no routes file: vehicles fall back to straight lines
    print(f"street routing off: {e}")
    routing = None


def street_path(d):
    if routing is None:
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


def run(closed=(), added=(), volume=1.0):
    key = json.dumps([sorted(closed), sorted(added), volume])
    if key not in CACHE:
        with LOCK:
            log = []
            out = sim.simulate(RT["test"], RT["homes"], RT["model"], closed=closed, added=added, volume=volume,
                               offset=RT["offset"], log=log)
            CACHE[key] = (sim.summarize(RT["test"], out), log)
            if len(CACHE) > 24:  # ponytail: tiny LRU, raise if demo traffic grows
                CACHE.popitem(last=False)
    CACHE.move_to_end(key)
    return CACHE[key]


def replay(log, ordinal):
    """One day of dispatches, times in seconds after local midnight. Unit timings include the
    calibration offset, so the live median matches the verdict."""
    t0 = day_start(ordinal)
    trips, calls = [], {}
    for d in log:
        if not t0 <= d["call"] < t0 + 86400:
            continue
        dep, arr, free = d["depart"] + OFF - t0, d["arrive"] + OFF - t0, d["free"] + OFF - t0
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


@lru_cache(maxsize=8)
def data_file_gz(name):
    return gzip.compress(data_file(name).read_bytes(), 6)


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
            self.reply(json.dumps(meta()))
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
        if self.path != "/api/simulate":
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
    run()  # warm the baseline before taking traffic
    print(f"Runnr on http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
