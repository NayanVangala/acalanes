"""Turn a typed what-if question into a Medio scenario, using Claude Haiku.

parse_scenario("close the two stations nearest Bayview", stations_meta)
  -> {"closed": ["17", "25"], "added": [], "volume": 1.0, "explanation": "..."}  or  {"error": "..."}

Stdlib only (no SDK) so the Render deploy needs no installs. Key: ANTHROPIC_API_KEY env var, else .env next to this file.
"""
import json
import math
import re
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
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
INT, STR, BOOL, NUMBER = {"type": "integer"}, {"type": "string"}, {"type": "boolean"}, {"type": "number"}


def _obj(**props):
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


PLACE = _obj(hood=STR, station=STR, near_me=BOOL, text=STR)
PLAN_SCHEMA = _obj(
    mode={"type": "string", "enum": ["simulate", "lookup", "off_topic"]},
    title=STR,
    close_stations={"type": "array", "items": STR},
    add_units={"type": "array", "items": _obj(station=STR, type={"type": "string", "enum": ["MEDIC", "ENGINE"]}, count=INT)},
    new_stations={"type": "array", "items": _obj(place=PLACE, kind={"type": "string", "enum": ["fire", "ambulance"]})},
    call_volume=NUMBER,
    volume_place=PLACE,
    slowdown=NUMBER,
    slow_from_hour=INT,
    slow_to_hour=INT,
    dispatch_delay_s=INT,
    units_busy={"type": "array", "items": _obj(station=STR, type={"type": "string", "enum": ["MEDIC", "ENGINE", "ANY"]},
                                               from_hour=INT, to_hour=INT)},
    incidents={"type": "array", "items": _obj(kind={"type": "string", "enum": KINDS}, place=PLACE, hour=INT,
                                              duration_min=INT, engines=INT, ambulances=INT)},
    focus=_obj(place=PLACE, from_hour=INT, to_hour=INT),
    lookup=_obj(kind={"type": "string", "enum": KINDS + ["any"]}, place=PLACE, hour_from=INT, hour_to=INT,
                days={"type": "string", "enum": ["any", "weekday", "weekend"]},
                group_by={"type": "string", "enum": ["none", "hour", "neighborhood", "kind", "weekday", "priority"]}),
    assumptions={"type": "array", "items": STR},
    not_modeled={"type": "array", "items": STR},
)

PLAN_SYSTEM = """You turn anything a person types about emergencies in San Francisco into a plan for Medio's simulator. The simulator replays all 13,417 real fire/EMS 911 calls from October 2026 with your changes and measures how long help takes. You never produce result numbers; the simulator does.

Levers (leave a lever at its neutral value when unused):
- close_stations: station numbers taken out of service (budget cuts, a station flooded, staffing shortage).
- add_units: extra ambulances (MEDIC) or engines (ENGINE) based at an existing station, with a count.
- new_stations: a brand-new station at a place ("fire" = an engine plus an ambulance, "ambulance" = an ambulance post).
- call_volume (1.0 = normal, 0.5 to 3.0) and volume_place (empty place = citywide): more 911 calls. Game, concert, parade, festival, protest = 1.3 to 1.6 at that place. Heat wave, flu season = 1.4 to 1.8 citywide. Earthquake or big disaster = 2.5 citywide.
- slowdown (1.0 = normal, up to 2.0) for the hours slow_from_hour to slow_to_hour (-1 and -1 = all day): slower driving from rain, fog, traffic, rush hour, construction, a closed bridge or road nearby. Rain about 1.15, heavy traffic about 1.25, earthquake about 1.5.
- dispatch_delay_s: extra seconds before units are sent (911 center outage, phone system down, fewer dispatchers). Usually 30 to 120.
- units_busy: specific units out of service for hours (station number, MEDIC/ENGINE/ANY, from_hour, to_hour; -1 if not given).
- incidents: one event at a place that ties up units (crash, fire, medical, mass_casualty) with hour, duration_min, engines, ambulances; -1 for anything not given.
- focus: where and when the person cares. A named place, or the incident's place, or near_me if they talk about themselves; empty place = citywide. Hours -1 = all day; if they give a time, use a window around it.

Places: hood = an exact neighborhood name from the list, station = a station number, near_me = the person's looked-up address ("near me", "my house", "here"), text = anything else to look up on a map (a street, intersection, landmark, address), for example "6th St and Market St" or "Oracle Park". Fill at most one; leave the others empty/false.

Be generous: every situation maps to some lever. Vague is fine; pick sensible amounts and say so in assumptions (plain words, one short sentence each). Put parts that the simulator truly can't represent (police, hospitals' capacity, a specific road closure's exact detour, injuries) in not_modeled, still simulating the closest lever.
mode: simulate for any situation or change; lookup only for a plain factual question with nothing changing ("which hours are slowest for fires") and then fill lookup; off_topic if it isn't about emergencies, safety, response or the city.
title: 3 to 7 words naming the scenario, like "Giants game at Oracle Park" or "Rainy evening rush"."""


def _place_ok(p, valid, hoods):
    p = dict(p or {})
    return {"hood": p.get("hood") if p.get("hood") in hoods else "",
            "station": str(p.get("station") or "") if str(p.get("station") or "") in valid else "",
            "near_me": bool(p.get("near_me")), "text": str(p.get("text") or "")[:120]}


def plan_scenario(text, stations_meta, me_label=None):
    """Free text -> a validated simulation plan, or {error}."""
    text = (text or "").strip()[:500]
    if not text:
        return {"error": "Describe a situation first, for example: a Giants game with a crash outside the ballpark."}
    key = _key()
    if not key:
        return {"error": "The question box isn't set up: the server has no ANTHROPIC_API_KEY."}
    ctx, valid = _context(stations_meta)
    hoods = {f["properties"]["nhood"] for f in json.load(open(SF / "hoods.geojson"))["features"]}
    where = f"The person has looked up their address: {me_label}." if me_label else "The person has not looked up an address."
    body = {"model": MODEL, "max_tokens": 2048,
            "system": [{"type": "text", "text": PLAN_SYSTEM + "\n\n" + ctx, "cache_control": {"type": "ephemeral"}}],
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
            "messages": [{"role": "user", "content": f"{where}\n\n{text}"}]}
    try:
        resp = _post(body, key, timeout=30)
    except Exception as e:
        return {"error": f"Couldn't reach the AI service ({type(e).__name__}). Try again."}
    if resp.get("type") == "error":
        return {"error": f"The AI service returned an error: {resp.get('error', {}).get('message', 'unknown')[:200]}"}
    if resp.get("stop_reason") == "refusal":
        return {"error": "That one was declined. Describe a situation in San Francisco."}
    raw = next((b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"), "")
    try:
        p = json.loads(raw)
        hour = lambda v, lo=0, hi=23: int(v) if isinstance(v, (int, float)) and lo <= v <= hi else -1
        num = lambda v, lo, hi, d: min(max(float(v), lo), hi) if isinstance(v, (int, float)) and math.isfinite(v) else d
        plan = {
            "mode": p["mode"], "title": str(p["title"])[:80],
            "close_stations": list(dict.fromkeys(str(s) for s in p["close_stations"] if str(s) in valid))[:20],
            "add_units": [{"station": str(a["station"]), "type": a["type"], "count": max(1, min(int(a.get("count") or 1), 5))}
                          for a in p["add_units"] if str(a["station"]) in valid and a["type"] in TYPES][:10],
            "new_stations": [{"place": _place_ok(n["place"], valid, hoods), "kind": n["kind"]} for n in p["new_stations"]][:3],
            "call_volume": num(p["call_volume"], 0.5, 3.0, 1.0), "volume_place": _place_ok(p["volume_place"], valid, hoods),
            "slowdown": num(p["slowdown"], 1.0, 2.0, 1.0),
            "slow_from_hour": hour(p["slow_from_hour"]), "slow_to_hour": hour(p["slow_to_hour"], 0, 24),
            "dispatch_delay_s": int(num(p["dispatch_delay_s"], 0, 600, 0)),
            "units_busy": [{"station": str(b["station"]), "type": b["type"], "from_hour": hour(b["from_hour"]),
                            "to_hour": hour(b["to_hour"], 0, 24)} for b in p["units_busy"] if str(b["station"]) in valid][:6],
            "incidents": [{"kind": i["kind"], "place": _place_ok(i["place"], valid, hoods), "hour": hour(i["hour"]),
                           "duration_min": hour(i["duration_min"], 5, 600), "engines": hour(i["engines"], 0, 8),
                           "ambulances": hour(i["ambulances"], 0, 8)} for i in p["incidents"]][:3],
            "focus": {"place": _place_ok(p["focus"]["place"], valid, hoods), "from_hour": hour(p["focus"]["from_hour"]),
                      "to_hour": hour(p["focus"]["to_hour"], 0, 24)},
            "lookup": {**p["lookup"], "place": _place_ok(p["lookup"]["place"], valid, hoods)},
            "assumptions": [str(a)[:240] for a in p["assumptions"]][:6],
            "not_modeled": [str(a)[:240] for a in p["not_modeled"]][:4],
            "usage": resp.get("usage", {}),
        }
    except (ValueError, KeyError, TypeError):
        return {"error": "Couldn't turn that into a scenario. Try describing what happens and where."}
    return plan


NUM = re.compile(r"\d+:\d\d|\d[\d,]*(?:\.\d+)?%|\b\d{1,3}(?:,\d{3})+\b|\b\d{3,}\b")
NARRATE = """Write the answer to the person's what-if from the simulator's measured outcome below.
headline: one or two plain sentences that directly answer their question, using only numbers that appear in the outcome, copied exactly (7:24, +0:18, 40%). Lead with what matters to them: for an incident, how fast the first engine gets there (from facts); for a change, how their place or time is affected; then the citywide picture if it differs. Waits are medians: say "typical", never "average". If nothing changes, say why in plain words (nearby stations cover it). No hype."""


def narrate(question, outcome):
    """One or two sentences from the measured outcome; every number checked against it. None if unusable."""
    key = _key()
    if not key:
        return None
    data = json.dumps(outcome, ensure_ascii=False)
    body = {"model": MODEL, "max_tokens": 400,
            "system": NARRATE,
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": _obj(headline=STR)}},
            "messages": [{"role": "user", "content": f"Question: {question}\n\nOutcome: {data}"}]}
    try:
        resp = _post(body, key, timeout=20)
        raw = next((b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"), "")
        head = str(json.loads(raw)["headline"])[:500]
    except Exception:
        return None
    bad = [n for n in NUM.findall(head) if n not in data]
    return None if bad else head


def geocode(text):
    """A street, intersection or landmark in San Francisco -> (lat, lng, label), via Photon (OpenStreetMap). None if not found."""
    url = ("https://photon.komoot.io/api/?limit=8&bbox=-122.52,37.70,-122.35,37.83&q="
           + urllib.parse.quote(f"{text}, San Francisco"))
    try:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "medio-sim"}), timeout=8) as r:
                d = json.loads(r.read())
        except urllib.error.URLError as e:
            if not isinstance(e.reason, ssl.SSLError):
                raise
            out = subprocess.run(["curl", "-sS", "--max-time", "8", "-A", "medio-sim", url], capture_output=True, text=True, timeout=12)
            d = json.loads(out.stdout)
        norm = lambda t: re.sub(r"\b(street)\b", "st", re.sub(r"\b(avenue)\b", "ave", t.lower()))
        want = set(re.findall(r"[a-z0-9]+", norm(text)))
        def score(f):  # "6th street" must not land on 6th Avenue: prefer results sharing the query's words
            p = f["properties"]
            have = set(re.findall(r"[a-z0-9]+", norm(" ".join(str(p.get(k, "")) for k in ("name", "street")))))
            return len(want & have) - 2 * bool({"st", "ave"} & want and ({"st", "ave"} & have) - want)
        f = max(d["features"], key=score)
        lng, lat = f["geometry"]["coordinates"]
        pr = f["properties"]
        label = text[:1].upper() + text[1:]  # the place as they named it; the POI name can be a corner shop
        return (lat, lng, label) if 37.70 < lat < 37.83 and -122.52 < lng < -122.35 else None
    except Exception:
        return None


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
