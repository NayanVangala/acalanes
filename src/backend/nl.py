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
AGENT_SYSTEM = """You answer open-ended questions about San Francisco 911 fire/EMS response for Medio, a simulator built from 300,081 real 911 incidents (held-out month: October 2026, 13,417 calls).
How you work:
- Get every number from a tool. Never estimate, round differently, or invent a number; copy values exactly as the tool wrote them (7:24, 40%, 1,234).
- Pick the closest measurable version of the question and say what you assumed. A parade, a game or a storm: more calls (simulate_city volume) or units tied up (incident). A blocked road or bridge: say the simulator has no road closures, then measure the nearest proxy (for example the affected station's units busy). Weather, traffic or hospitals you can't see: say so plainly, then give what the records do show.
- Always run the proxy instead of only describing it: a game, concert or parade = simulate_city with volume 1.3 to 1.5 (and an incident at the venue if one is mentioned); a storm or disaster = volume 2 or more.
- Use the user's address only when the question is about them ("near me", "my place", "here", "me").
- Combine tools when the question has parts (for example a neighborhood profile and a scenario). Two or three tool calls is usually enough; never more than five.
- If the question isn't about emergencies, response times, stations, ambulances or this city, call reply and say what you can answer.
- Finish by calling reply exactly once: headline = one or two plain sentences that answer the question directly; facts = 2 to 6 rows, each a short label and a value copied from a tool; assumptions = what you assumed, in plain words.
Stations and neighborhoods must come from these lists."""

KIND_ANY = KINDS + ["any"]
STR, BOOL = {"type": "string"}, {"type": "boolean"}
TOOLS = [
    {"name": "records", "description": "Statistics over the 13,417 real October 2026 calls (wait = call received to first unit on scene). Filter by kind, neighborhood, first-due station, near the user's address, hours and weekday/weekend; optionally group the result.",
     "input_schema": {"type": "object", "required": ["kind"], "properties": {
         "kind": {"type": "string", "enum": KIND_ANY}, "hood": STR, "station": STR, "near_me": BOOL,
         "hour_from": INT, "hour_to": INT, "days": {"type": "string", "enum": ["any", "weekday", "weekend"]},
         "group_by": {"type": "string", "enum": ["none", "hour", "neighborhood", "kind", "weekday", "priority"]}}}},
    {"name": "simulate_city", "description": "Re-run all of October with lasting changes: close stations, add ambulances (MEDIC) or engines (ENGINE) at stations, or scale 911 call volume (0.5 to 3.0). Returns citywide and neighborhood waits before and after. The map will show this scenario.",
     "input_schema": {"type": "object", "required": ["closed", "added", "volume"], "properties": {
         "closed": {"type": "array", "items": STR},
         "added": {"type": "array", "items": {"type": "object", "required": ["station", "type"], "properties": {
             "station": STR, "type": {"type": "string", "enum": ["MEDIC", "ENGINE"]}}}},
         "volume": {"type": "number"}}}},
    {"name": "incident", "description": "Simulate one event (crash, fire, medical, mass casualty) at a place and hour that ties up the nearest units every day of October: how fast the first engine arrives, and how much longer other nearby calls wait. Use -1 for unknown numbers.",
     "input_schema": {"type": "object", "required": ["kind"], "properties": {
         "kind": {"type": "string", "enum": KINDS}, "hood": STR, "station": STR, "near_me": BOOL, "hour": INT,
         "duration_min": INT, "engines": INT, "ambulances": INT}}},
    {"name": "units_busy", "description": "Take specific units out of service for a window of hours (an engine at a station, the ambulance nearest a station, or ANY) and measure the effect on calls in that station's area.",
     "input_schema": {"type": "object", "required": ["busy"], "properties": {"busy": {"type": "array", "items": {
         "type": "object", "required": ["station", "type"], "properties": {
             "station": STR, "type": {"type": "string", "enum": ["MEDIC", "ENGINE", "ANY"]}, "from_hour": INT, "to_hour": INT}}}}}},
    {"name": "place_profile", "description": "Profile one place: a neighborhood, a station, or the user's looked-up address. Typical wait, rank, time-of-day pattern, likely first responders.",
     "input_schema": {"type": "object", "properties": {"hood": STR, "station": STR, "near_me": BOOL}}},
    {"name": "reply", "description": "Give the final answer. Call exactly once, last.",
     "input_schema": {"type": "object", "required": ["headline", "facts", "assumptions"], "properties": {
         "headline": STR,
         "facts": {"type": "array", "items": {"type": "object", "required": ["k", "v"], "properties": {"k": STR, "v": STR, "note": STR}}},
         "assumptions": {"type": "array", "items": STR}}}},
]
NUM = re.compile(r"\d+:\d\d|\d[\d,]*(?:\.\d+)?%|\b\d{1,3}(?:,\d{3})+\b|\b\d{3,}\b")


def agent(text, stations_meta, handlers, me_label=None, max_rounds=6):
    """Open-ended question -> tool calls on real data -> {headline, facts, assumptions, steps, checked, unmatched}.
    handlers: {tool name: fn(input dict) -> dict}. Every number in the answer is checked against tool outputs."""
    text = (text or "").strip()[:500]
    if not text:
        return {"error": "Type a question first, for example: what if there's a crash near me at 5 pm?"}
    key = _key()
    if not key:
        return {"error": "The question box isn't set up: the server has no ANTHROPIC_API_KEY."}
    ctx, _ = _context(stations_meta)
    hoods = sorted({f["properties"]["nhood"] for f in json.load(open(SF / "hoods.geojson"))["features"]})
    system = [{"type": "text", "text": AGENT_SYSTEM + "\n\n" + ctx + "\n\nNeighborhood names: " + "; ".join(hoods),
               "cache_control": {"type": "ephemeral"}}]
    where = f"The user has looked up this address: {me_label}." if me_label else "The user has not looked up an address."
    msgs = [{"role": "user", "content": f"{where}\n\nQuestion: {text}"}]
    steps, seen, usage = [], [], {}
    for _ in range(max_rounds):
        try:
            resp = _post({"model": MODEL, "max_tokens": 1500, "system": system, "tools": TOOLS, "messages": msgs}, key, timeout=30)
        except Exception as e:
            return {"error": f"Couldn't reach the AI service ({type(e).__name__}). Try again or use the controls."}
        if resp.get("type") == "error":
            return {"error": f"The AI service returned an error: {resp.get('error', {}).get('message', 'unknown')[:200]}"}
        for k, v in (resp.get("usage") or {}).items():
            if isinstance(v, int):
                usage[k] = usage.get(k, 0) + v
        content = resp.get("content", [])
        calls = [b for b in content if b.get("type") == "tool_use"]
        final = next((b for b in calls if b["name"] == "reply"), None)
        if final or not calls:
            if final:
                fin = final["input"]
                def listify(v):  # the model sometimes sends a list as a JSON string
                    if isinstance(v, str) and v.strip().startswith("["):
                        try:
                            v = json.loads(v)
                        except ValueError:
                            pass
                    return v if isinstance(v, list) else [v] if v else []
                out = {"headline": str(fin.get("headline", ""))[:600],
                       "facts": [{k: str(f.get(k, ""))[:200] for k in ("k", "v", "note") if f.get(k)} if isinstance(f, dict) else {"k": str(f)[:200]}
                                 for f in listify(fin.get("facts"))][:8],
                       "assumptions": [str(a)[:300] for a in listify(fin.get("assumptions"))][:5]}
            else:
                out = {"headline": " ".join(b.get("text", "") for b in content if b.get("type") == "text").strip()[:600], "facts": [], "assumptions": []}
            data = "\n".join(seen)
            nums = NUM.findall(" ".join([out["headline"]] + [f.get("v", "") + " " + f.get("note", "") for f in out["facts"]]))
            out.update({"steps": steps, "checked": len(nums), "unmatched": sorted({n for n in nums if n not in data}), "usage": usage})
            return out
        msgs.append({"role": "assistant", "content": content})
        results = []
        for c in calls:
            fn = handlers.get(c["name"])
            try:
                res = fn(c["input"]) if fn else {"error": "unknown tool"}
            except Exception as e:  # a bad station number etc.: let the model see it and recover
                res = {"error": f"{type(e).__name__}: {e}"[:200]}
            label = res.pop("_label", c["name"]) if isinstance(res, dict) else c["name"]
            body = json.dumps(res, default=str)
            seen.append(body)
            steps.append({"tool": c["name"], "label": label})
            results.append({"type": "tool_result", "tool_use_id": c["id"], "content": body[:6000]})
        msgs.append({"role": "user", "content": results})
    return {"error": "That question needed too many steps. Try asking one thing at a time."}


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
