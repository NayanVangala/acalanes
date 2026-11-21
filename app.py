"""Runnr web server. python3 app.py -> http://localhost:8000 (Render sets $PORT).

GET  /                 the 3D app (web/index.html)
GET  /data/<file>      stations.json, hoods.geojson, people.json, validation.json
GET  /api/meta         fleet, period, baseline results
POST /api/simulate     {"closed": [stations], "added": [[station, "MEDIC"|"ENGINE"]], "volume": 1.0}
"""
import gzip
import json
import os
import pickle
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

import sim
from sim import TravelModel  # noqa: F401  (needed to unpickle the model)

ROOT = Path(__file__).parent
with gzip.open(ROOT / "data/runtime.pkl.gz", "rb") as f:
    RT = pickle.load(f)
STATIONS = {s for s in sim.load_stations()}
TYPES = {"MEDIC", "ENGINE", "TRUCK"}
LOCK = Lock()  # the travel memo is shared; one simulation at a time is plenty for a demo
CACHE = {}


def run(closed=(), added=(), volume=1.0):
    key = json.dumps([sorted(closed), sorted(added), volume])
    if key not in CACHE:
        with LOCK:
            out = sim.simulate(RT["test"], RT["homes"], RT["model"], closed=closed, added=added, volume=volume,
                               offset=RT["offset"])
            CACHE[key] = sim.summarize(RT["test"], out)
    return CACHE[key]


def meta():
    fleet = Counter((h[0][1:], h[3]) for h in RT["homes"].values() if h[0][0] == "S")
    by_station = {}
    for (st, typ), n in fleet.items():
        by_station.setdefault(st, {})[typ] = n
    return {"period": RT["period"], "calls": len(RT["test"]), "units": len(RT["homes"]),
            "incidents_total": RT["n_incidents"], "fleet": by_station, "baseline": run()}


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
        except (ValueError, TypeError, AttributeError):
            return self.reply('{"error": "bad request"}', code=400)
        self.reply(json.dumps(run(closed, added, volume)))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    run()  # warm the baseline before taking traffic
    print(f"Runnr on http://localhost:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
