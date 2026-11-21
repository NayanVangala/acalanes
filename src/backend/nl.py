"""Turn a typed what-if question into a Medio scenario, using Claude Haiku.

parse_scenario("close the two stations nearest Bayview", stations_meta)
  -> {"closed": ["17", "25"], "added": [], "volume": 1.0, "explanation": "..."}  or  {"error": "..."}

Stdlib only (no SDK) so the Render deploy needs no installs. Key: ANTHROPIC_API_KEY env var, else .env next to this file.
"""
import json
import math
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from paths import ROOT, SF
MODEL = "claude-haiku-5-5"
URL = "https://api.anthropic.com/v1/messages"
TYPES = {"MEDIC", "ENGINE"}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["closed", "added", "volume", "explanation"],
    "properties": {
        "closed": {"type": "array", "items": {"type": "string"}},
        "added": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["station", "type"],
            "properties": {"station": {"type": "string"}, "type": {"type": "string", "enum": ["MEDIC", "ENGINE"]}}}},
        "volume": {"type": "number"},
        "explanation": {"type": "string"},
    },
}


KINDS = ["crash", "fire", "medical", "mass_casualty", "other"]
INT = {"type": "integer"}
Q_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["intent", "closed", "added", "volume", "incident", "busy", "info", "explanation"],
    "properties": {
        "intent": {"type": "string", "enum": ["scenario", "incident", "units_busy", "info", "other"]},
        "closed": {"type": "array", "items": {"type": "string"}},
        "added": SCHEMA["properties"]["added"],
        "volume": {"type": "number"},
        "incident": {"type": "object", "additionalProperties": False,
                     "required": ["kind", "hood", "station", "near_me", "hour", "duration_min", "engines", "ambulances"],
                     "properties": {"kind": {"type": "string", "enum": KINDS}, "hood": {"type": "string"},
                                    "station": {"type": "string"}, "near_me": {"type": "boolean"}, "hour": INT,
                                    "duration_min": INT, "engines": INT, "ambulances": INT}},
        "busy": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                 "required": ["station", "type", "from_hour", "to_hour"],
                 "properties": {"station": {"type": "string"}, "type": {"type": "string", "enum": ["MEDIC", "ENGINE", "ANY"]},
                                "from_hour": INT, "to_hour": INT}}},
        "info": {"type": "object", "additionalProperties": False, "required": ["topic", "kind", "hood", "station"],
                 "properties": {"topic": {"type": "string", "enum": ["call_kind", "neighborhood", "station", "citywide", "none"]},
                                "kind": {"type": "string", "enum": KINDS + [""]}, "hood": {"type": "string"},
                                "station": {"type": "string"}}},
        "explanation": {"type": "string"},
    },
}

Q_SYSTEM = """You read a question about San Francisco 911 fire/EMS response and turn it into a structured request for a simulator built from real 911 records. You never answer with numbers yourself; the simulator does.
Pick one intent:
- scenario: a lasting change to the city: close stations (closed), add units at a station (added: MEDIC = ambulance, ENGINE = fire engine), or change 911 call volume (volume 0.5-3.0; heat wave/disaster day ~1.5-2).
- incident: something happens at a place and time that ties up responders (a crash, a fire, a big medical emergency, a mass-casualty event). Fill incident: kind; hood = a neighborhood name from the list if a place is named or implied ("" if none); station = a station number if the place is given as a station ("" if none); near_me = true if they say "near me", "my address", "here", or give no place at all; hour 0-23 if a time is given (5 pm = 17, rush hour = 17, night = 2, morning = 8), else -1; duration_min if given, else -1; engines/ambulances if a count is given, else -1.
- units_busy: specific units are out of service ("the ambulance at Station 43 is busy", "Station 7's engine is at a fire from 5 to 7"). Fill busy with station number, type (MEDIC, ENGINE, or ANY), from_hour/to_hour (-1 if not given).
- info: a factual question ("how fast do crashes get help", "how is the Mission doing", "how busy is Station 1"). topic = call_kind (with kind), neighborhood (with hood), station (with station), or citywide.
- other: not about 911 response.
Unused fields: empty lists, volume 1.0, incident {kind "other", hood "", station "", near_me false, hour -1, duration_min -1, engines -1, ambulances -1}, info {topic "none", kind "", hood "", station ""}.
Use only station numbers and neighborhood names from the lists. "Near X" for stations means X's nearest stations.
explanation: one plain sentence saying how you read the question, naming the place, time and units you assumed."""


def parse_question(text, stations_meta):
    """Read any what-if question into {intent, closed, added, volume, incident, busy, info, explanation} or {error}."""
    text = (text or "").strip()[:500]
    if not text:
        return {"error": "Type a question first, for example: what if there's a crash near me at 5 pm?"}
    key = _key()
    if not key:
        return {"error": "The question box isn't set up: the server has no ANTHROPIC_API_KEY."}
    ctx, valid = _context(stations_meta)
    hoods = {f["properties"]["nhood"] for f in json.load(open(SF / "hoods.geojson"))["features"]}
    body = {"model": MODEL, "max_tokens": 4096,
            "system": [{"type": "text", "text": Q_SYSTEM + "\n\n" + ctx, "cache_control": {"type": "ephemeral"}}],
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": Q_SCHEMA}},
            "messages": [{"role": "user", "content": text}]}
    try:
        resp = _post(body, key)
    except Exception as e:
        return {"error": f"Couldn't reach the AI service ({type(e).__name__}). Try again or use the controls."}
    if resp.get("type") == "error":
        return {"error": f"The AI service returned an error: {resp.get('error', {}).get('message', 'unknown')[:200]}"}
    if resp.get("stop_reason") == "refusal":
        return {"error": "That question was declined. Ask about 911 response in San Francisco."}
    raw = next((b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"), "")
    try:
        out = json.loads(raw)
        inc, info = out["incident"], out["info"]
        clean_int = lambda v, lo, hi: int(v) if isinstance(v, (int, float)) and lo <= v <= hi else -1
        q = {
            "intent": out["intent"],
            "closed": list(dict.fromkeys(str(s) for s in out["closed"] if str(s) in valid))[:20],
            "added": [[str(a["station"]), a["type"]] for a in out["added"] if str(a["station"]) in valid and a["type"] in TYPES][:20],
            "volume": min(max(float(out["volume"]), 0.5), 3.0),
            "incident": {"kind": inc["kind"] if inc["kind"] in KINDS else "other",
                         "hood": inc["hood"] if inc["hood"] in hoods else "",
                         "station": str(inc["station"]) if str(inc["station"]) in valid else "",
                         "near_me": bool(inc["near_me"]), "hour": clean_int(inc["hour"], 0, 23),
                         "duration_min": clean_int(inc["duration_min"], 5, 600),
                         "engines": clean_int(inc["engines"], 0, 6), "ambulances": clean_int(inc["ambulances"], 0, 6)},
            "busy": [{"station": str(b["station"]), "type": b["type"], "from_hour": clean_int(b["from_hour"], 0, 23),
                      "to_hour": clean_int(b["to_hour"], 0, 24)} for b in out["busy"] if str(b["station"]) in valid][:6],
            "info": {"topic": info["topic"], "kind": info["kind"] if info["kind"] in KINDS else "",
                     "hood": info["hood"] if info["hood"] in hoods else "",
                     "station": str(info["station"]) if str(info["station"]) in valid else ""},
            "explanation": str(out["explanation"])[:400],
            "usage": resp.get("usage", {}),
        }
    except (ValueError, KeyError, TypeError):
        return {"error": "Couldn't read that question. Try: what if there's a crash near me at 5 pm?"}
    return q


def _key():
    # strip: a key pasted into a dashboard often carries a trailing newline, which breaks the HTTP header
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return os.environ["ANTHROPIC_API_KEY"].strip().strip('"').strip("'")
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "ANTHROPIC_API_KEY" and v.strip():
                return v.strip().strip('"').strip("'")
    return None


def _post(body, key, timeout=20):
    data = json.dumps(body).encode()
    headers = {"content-type": "application/json", "x-api-key": key, "anthropic-version": "2026-10-21"}
    try:
        req = urllib.request.Request(URL, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        if not isinstance(e.reason, ssl.SSLError):
            raise
    # python.org builds on macOS can't verify certs; curl uses the system keychain. Key goes via stdin config, not argv.
    cfg = "".join(f'header = "{k}: {v}"\n' for k, v in headers.items())
    out = subprocess.run(["curl", "-sS", "--max-time", str(timeout), "-K", "-", "--data-binary", data.decode(), URL],
                         input=cfg, capture_output=True, text=True, timeout=timeout + 5)
    if out.returncode:
        raise RuntimeError(f"network error: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def _km(a, b):
    x = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    return 6371 * math.hypot(x, math.radians(b[0] - a[0]))


def _context(stations_meta):
    """Station list plus, for each neighborhood, its 4 nearest stations (so 'near Bayview' works)."""
    known = {s["station"].split()[0]: s for s in json.load(open(SF / "stations.json"))}
    st = {}
    for num, m in stations_meta.items():
        k = known.get(str(num), {})
        st[str(num)] = {"address": (m or {}).get("address") or k.get("address", ""),
                        "lat": (m or {}).get("lat") or k.get("lat"), "lng": (m or {}).get("lng") or k.get("lng"),
                        "units": (m or {}).get("units") or {}}
    lines = ["Stations (number | address | units):"]
    for num in sorted(st, key=lambda n: int(n) if n.isdigit() else 999):
        u = ", ".join(f"{c} {t.lower()}" for t, c in st[num]["units"].items()) or "unknown"
        lines.append(f"{num} | {st[num]['address'].title()} | {u}")
    lines.append("\nNeighborhoods and their nearest stations (closest first):")
    for f in json.load(open(SF / "hoods.geojson"))["features"]:
        g = f["geometry"]
        ring = max([g["coordinates"][0]] if g["type"] == "Polygon" else [p[0] for p in g["coordinates"]], key=len)
        c = (sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring))
        near = sorted((n for n in st if st[n]["lat"]), key=lambda n: _km(c, (st[n]["lat"], st[n]["lng"])))[:4]
        lines.append(f"{f['properties']['nhood']}: " + ", ".join(f"{n} ({_km(c, (st[n]['lat'], st[n]['lng'])):.1f} km)" for n in near))
    return "\n".join(lines), set(st)


SYSTEM = """You turn a question about San Francisco 911 fire/EMS coverage into a simulation scenario.
Scenario fields: closed = station numbers to close; added = extra units (MEDIC = ambulance, ENGINE = fire engine) placed at a station; volume = 911 call volume multiplier (1.0 normal, 2.0 double, allowed 0.5 to 3.0); explanation = one or two plain sentences saying what you set up and why (name the stations).
Use only station numbers from the list. "Near X" means the nearest stations listed for neighborhood X. "In X" for adding units means X's nearest station(s). If the question names no change, return empty lists and volume 1.0, and say so in the explanation. If it isn't about 911 coverage, return empty lists, volume 1.0, and explain that you can only set up station closures, added units and call volume."""


def parse_scenario(text, stations_meta):
    text = (text or "").strip()[:500]
    if not text:
        return {"error": "Type a question first, for example: close the two stations nearest Bayview."}
    key = _key()
    if not key:
        return {"error": "The question box isn't set up: the server has no ANTHROPIC_API_KEY."}
    ctx, valid = _context(stations_meta)
    body = {
        "model": MODEL, "max_tokens": 4096,
        "system": [{"type": "text", "text": SYSTEM + "\n\n" + ctx, "cache_control": {"type": "ephemeral"}}],
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        "messages": [{"role": "user", "content": text}],
    }
    try:
        resp = _post(body, key)
    except Exception as e:  # network, timeout, HTTP error: plain words, never the key
        return {"error": f"Couldn't reach the AI service ({type(e).__name__}). Try again or use the controls."}
    if resp.get("type") == "error":
        return {"error": f"The AI service returned an error: {resp.get('error', {}).get('message', 'unknown')[:200]}"}
    if resp.get("stop_reason") == "refusal":
        return {"error": "That question was declined. Ask about closing stations, adding units or call volume."}
    raw = next((b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"), "")
    try:
        out = json.loads(raw)
        closed = list(dict.fromkeys(str(s) for s in out["closed"] if str(s) in valid))[:20]
        added = [[str(a["station"]), a["type"]] for a in out["added"]
                 if str(a["station"]) in valid and a["type"] in TYPES][:20]
        volume = min(max(float(out["volume"]), 0.5), 3.0)
        explanation = str(out["explanation"])[:400]
    except (ValueError, KeyError, TypeError):
        return {"error": "Couldn't turn that into a scenario. Try: close Station 17, or add 2 ambulances in the Excelsior."}
    return {"closed": closed, "added": added, "volume": volume, "explanation": explanation, "usage": resp.get("usage", {})}


if __name__ == "__main__":
    meta = {s["station"]: {} for s in json.load(open(SF / "stations.json")) if s["station"].isdigit()}
    assert parse_scenario("", meta)["error"]
    for q in ["close the two stations nearest Bayview", "add 3 ambulances in the Excelsior",
              "what happens in a heat wave with double the calls"]:
        t = time.time()
        r = parse_scenario(q, meta)
        u = r.pop("usage", {})
        cost = (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) * 1.25
                + u.get("cache_read_input_tokens", 0) * 0.1) * 0.10e-6 + u.get("output_tokens", 0) * 0.50e-6
        print(f"{q!r} ({time.time() - t:.1f}s, ~${cost:.5f}):\n  {json.dumps(r)}")
        assert "error" not in r, r
