"""Runnr web server. python3 app.py -> http://localhost:8000 (Render sets $PORT).

GET  /                 the app (web/index.html)
GET  /data/<file>      stations.json, hoods.geojson, people.json, validation.json
GET  /api/meta         fleet, period, replay days, baseline results + replay of the default day
POST /api/simulate     {"closed": [stations], "added": [[station, "MEDIC"|"ENGINE"]], "volume": 1.0, "day": <ordinal>}
                       -> neighborhood results for the whole held-out month + a replay of one day
"""
import gzip
import json
import os
import pickle
from collections import Counter, OrderedDict
from datetime import date, datetime, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

import sim
from sim import TravelModel  # noqa: F401  (needed to unpickle the model)

ROOT = Path(__file__).parent
with gzip.open(ROOT / "data/runtime.pkl.gz", "rb") as f:
    RT = pickle.load(f)
STATIONS = set(sim.load_stations())
TYPES = {"MEDIC", "ENGINE", "TRUCK"}
LOCK = Lock()  # the travel memo is shared; one simulation at a time is plenty for a demo
CACHE = OrderedDict()
DAYS = Counter(i["day"] for i in RT["test"])
DEFAULT_DAY = max(DAYS, key=DAYS.get)  # the busiest day makes the liveliest replay
OFF = round(RT["offset"])


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
                      d["call_type"].capitalize()])
        c = calls.setdefault(d["incident"], [d["to"][0], d["to"][1], d["call"] - t0, arr, d["hood"], d["call_type"].capitalize()])
        c[3] = min(c[3], arr)
    trips.sort(key=lambda r: r[6])
    return {"day": ordinal, "trips": trips,
            "calls": sorted(([*c[:3], round(c[3]), *c[4:]] for c in calls.values()), key=lambda c: c[2])}


def meta():
    fleet = Counter((h[0][1:], h[3]) for h in RT["homes"].values() if h[0][0] == "S")
    by_station = {}
    for (st, typ), n in fleet.items():
        by_station.setdefault(st, {})[typ] = n
    summary, log = run()
    days = [{"day": d, "label": date.fromordinal(d).strftime("%a %b %-d"), "calls": n} for d, n in sorted(DAYS.items())]
    return {"period": RT["period"], "calls": len(RT["test"]), "units": len(RT["homes"]),
            "incidents_total": RT["n_incidents"], "fleet": by_station, "baseline": summary,
            "days": days, "replay": replay(log, DEFAULT_DAY)}


class Handler(BaseHTTPRequestHandler):
    def reply(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self.reply((ROOT / "web/index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/meta":
            self.reply(json.dumps(meta()))
        elif path.startswith("/data/") and Path(path).name in {"stations.json", "hoods.geojson", "people.json",
                                                               "validation.json"}:
            self.reply((ROOT / "data" / Path(path).name).read_bytes())
        else:
            self.reply("not found", "text/plain", 404)

    def do_POST(self):
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

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    run()  # warm the baseline before taking traffic
    print(f"Runnr on http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
