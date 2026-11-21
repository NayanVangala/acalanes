"""Medio simulator: replay real SF 911 incidents with a learned travel-time model.

python3 sim.py   -> fits on training months, replays the held-out month, prints validation vs real arrivals.

Travel-time model (learned from real trips, only trips where the unit had been idle >= 15 min,
so it was most likely at its home station):
  1. median of real trips from that home to that ~500 m grid cell in that hour band (>= 5 trips)
  2. else median for that home -> cell over all hours (>= 5 trips)
  3. else median travel for that straight-line distance (250 m bins) in that hour band
Simulation: incidents arrive in real order with their real dispatch delay. Each real unit dispatched is
replaced by the unit of the same type that can arrive soonest (busy units wait until free).
Each simulated unit then takes the real turnout and on-scene time of the unit it replaces.
"""
import csv
import gzip
import json
import math
import random
import re
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from paths import BUILD, SF as SF_DATA
SF = ZoneInfo("America/Los_Angeles")
SIM_TYPES = {"ENGINE", "TRUCK", "MEDIC", "PRIVATE", "RESCUE SQUAD"}  # units that deliver help on scene
CELL_LAT, CELL_LNG = 0.0045, 0.0057  # ~500 m
BIN_KM = 0.25
IDLE_S = 15 * 60
MIN_TRIPS = 5


def num(x):
    return int(x) if x else None


def band(t):
    h = datetime.fromtimestamp(t, SF).hour
    return 0 if h < 6 else 1 if h < 10 else 2 if h < 16 else 3 if h < 20 else 4


def day(t):
    return datetime.fromtimestamp(t, SF).toordinal()


def cell(lat, lng):
    return int(lat / CELL_LAT), int(lng / CELL_LNG)


def km(a_lat, a_lng, b_lat, b_lng):
    x = math.radians(b_lng - a_lng) * math.cos(math.radians((a_lat + b_lat) / 2))
    return 6371 * math.hypot(x, math.radians(b_lat - a_lat))


def load_stations():
    return {s["station"].split()[0]: (s["lat"], s["lng"]) for s in json.load(open(SF_DATA / "stations.json"))}


def load_transports(path=SF_DATA / "transports.csv.gz"):
    """(incident, unit) pairs that took a patient to hospital. Covers the calibration + held-out months."""
    if not path.exists():
        return set()
    with gzip.open(path, "rt") as f:
        return {(r["incident"], r["unit"]) for r in csv.DictReader(f)}


def load_hospitals():
    p = SF_DATA / "hospitals.json"
    return [(h["lat"], h["lng"]) for h in json.load(open(p))] if p.exists() else []


def load_incidents(path=SF_DATA / "calls.csv.gz"):
    """Incidents with >= 1 on-scene unit of a SIM_TYPE, in time order."""
    inc, transports = {}, load_transports()
    with gzip.open(path, "rt") as f:
        for r in csv.DictReader(f):
            i = inc.get(r["incident"])
            if i is None:
                i = inc[r["incident"]] = {"id": r["incident"], "t": int(r["received"]), "lat": float(r["lat"]),
                                          "lng": float(r["lng"]), "hood": r["hood"], "group": r["group"],
                                          "type": r["call_type"], "priority": r["priority"], "units": [],
                                          "area": r["station"].lstrip("0") or "0"}
            if r["unit_type"] in SIM_TYPES and r["dispatch"]:
                i["units"].append({"unit": r["unit"], "type": r["unit_type"], "dispatch": int(r["dispatch"]),
                                   "enroute": num(r["enroute"]), "onscene": num(r["onscene"]),
                                   "available": num(r["available"]),
                                   "transport": (r["incident"], r["unit"]) in transports})
    out = []
    for i in inc.values():
        i["units"].sort(key=lambda u: u["dispatch"])
        arrivals = [u["onscene"] for u in i["units"] if u["onscene"]]
        if not arrivals:
            continue
        i["real_response"] = min(arrivals) - i["t"]
        i["delay"] = i["units"][0]["dispatch"] - i["t"]
        if 0 <= i["delay"] < 3600 and 0 < i["real_response"] < 3 * 3600:
            i["cell"] = cell(i["lat"], i["lng"])
            i["day"] = day(i["t"])
            out.append(i)
    return sorted(out, key=lambda i: i["t"])


def unit_homes(incidents, stations):
    """unit id -> (home key, lat, lng, type). Station-numbered units live at their station;
    others (most ambulances) at the median location of the calls they reached fastest."""
    seen, quick = {}, defaultdict(list)
    for i in incidents:
        for u in i["units"]:
            seen[u["unit"]] = u["type"]
            if u["enroute"] and u["onscene"] and u["onscene"] - u["enroute"] < 240:
                quick[u["unit"]].append((i["lat"], i["lng"]))
    homes = {}
    for uid, typ in seen.items():
        m = re.match(r"^[A-Z]+(\d+)$", uid)
        if m and str(int(m.group(1))) in stations and typ != "PRIVATE":
            st = str(int(m.group(1)))
            homes[uid] = ("S" + st, *stations[st], typ)
        elif quick[uid]:
            pts = quick[uid]
            homes[uid] = ("U" + uid, median(p[0] for p in pts), median(p[1] for p in pts), typ)
    return homes


class TravelModel:
    def __init__(self, incidents, homes, mode="learned"):
        self.mode = mode
        last_free = {}
        exact, home_cell, by_bin, speeds = defaultdict(list), defaultdict(list), defaultdict(list), []
        for i in incidents:
            for u in i["units"]:
                h = homes.get(u["unit"])
                idle = u["dispatch"] - last_free.get(u["unit"], -10**12)
                if u["available"]:
                    last_free[u["unit"]] = u["available"]
                if not h or not (u["enroute"] and u["onscene"]) or idle < IDLE_S:
                    continue
                tt = u["onscene"] - u["enroute"]
                if not 0 < tt < 1800:
                    continue
                b, d = band(u["enroute"]), km(h[1], h[2], i["lat"], i["lng"])
                exact[(h[0], i["cell"], b)].append(tt)
                home_cell[(h[0], i["cell"])].append(tt)
                by_bin[(b, int(d / BIN_KM))].append(tt)
                if d > 0.3:
                    speeds.append(d / tt)
        self.exact = {k: median(v) for k, v in exact.items() if len(v) >= MIN_TRIPS}
        self.home_cell = {k: median(v) for k, v in home_cell.items() if len(v) >= MIN_TRIPS}
        self.bins = {}
        for b in range(5):  # monotone: travel never shrinks with distance
            prev = 0
            for k in range(int(12 / BIN_KM)):
                xs = by_bin.get((b, k), [])
                prev = max(prev, median(xs)) if len(xs) >= MIN_TRIPS else prev
                self.bins[(b, k)] = prev
        self.speed = median(speeds)  # km/s, for the straight-line baseline
        self.n_trips = sum(len(v) for v in home_cell.values())
        self.memo = {}

    def from_point(self, lat, lng, to_lat, to_lng, b):
        """Travel time from an arbitrary point (a unit still at a scene or a hospital), by distance and hour band."""
        d = km(lat, lng, to_lat, to_lng)
        if d < 0.05:
            return 30
        return self.bins.get((b, int(d / BIN_KM))) or 60 + d / self.speed

    def __call__(self, home, i_cell, lat, lng, b):
        key = (home[0], i_cell, b)
        if key in self.memo:
            return self.memo[key]
        d = km(home[1], home[2], lat, lng)
        if self.mode == "straight":
            t = 60 + d / self.speed
        else:
            t = self.exact.get(key) or self.home_cell.get(key[:2]) or self.bins.get((b, int(d / BIN_KM))) or 60 + d / self.speed
        self.memo[key] = t
        return t


def duty_days(incidents):
    """unit -> set of days it was dispatched at least once. Proxy for 'staffed that day'."""
    out = defaultdict(set)
    for i in incidents:
        for u in i["units"]:
            out[u["unit"]].add(i["day"])
    return out


def offset_for(offset, t):
    """Calibration seconds for a call at time t: a number, or {band: seconds} learned per time of day."""
    return offset.get(band(t), offset.get("all", 0.0)) if isinstance(offset, dict) else offset


FIRST_DUE = {"ENGINE", "TRUCK"}  # SF sends the call's own station first: 86.5% of first engines, 63% of trucks (2 years of data)


def simulate(incidents, homes, travel, closed=(), added=(), volume=1.0, seed=0, offset=0.0, log=None, positions=True,
             first_due=True, blocked=None, new_homes=(), volume_area=None, extra_delay=0, volume_hours=None):
    """Returns {incident id: simulated response seconds}. closed: station numbers; added: [(station, type)].
    Units are only available on days they really worked (added units: every day).
    offset: calibration seconds added to every simulated response (number, or {band: seconds}).
    positions: units are dispatched from where they are (still at a scene, or at the hospital after a transport)
    until they've had time to drive home; False = the original model where every unit leaves from home.
    blocked: {unit id: (from_sec, to_sec)} local time of day when that unit can't be dispatched (an incident or a
    unit tied up), applied on every day of the month so the effect is averaged over real days.
    first_due: engines and trucks go to the call's own first-due station's unit when it's free (how SF dispatches),
    else the unit that can arrive soonest. Ambulances always go to the soonest (they rove, no home area).
    log: optional list; each dispatch appends a dict (unit, type, from, to, call, depart, arrive, free, travel, incident).
    new_homes: [(lat, lng, type)] units based anywhere (a proposed station); drive times come from the distance model.
    volume_area: (lat, lng, km): extra calls (volume > 1) are copies of real calls from inside that circle only.
    extra_delay: seconds added between the 911 call and dispatch (a slower dispatch center).
    volume_hours: (h0, h1) local hours: extra calls come from, and land in, that window of each day (a game, rush hour)."""
    on_duty = duty_days(incidents)
    stations = load_stations()
    fleet = {uid: h for uid, h in homes.items() if h[0][1:] not in set(map(str, closed)) or h[0][0] != "S"}
    for n, (st, typ) in enumerate(added):
        fleet[f"NEW{n}"] = ("S" + str(st), *stations[str(st)], typ)
        on_duty[f"NEW{n}"] = None  # always on duty
    for n, (la, ln, typ) in enumerate(new_homes):
        fleet[f"PROP{n}"] = (f"X{la:.4f},{ln:.4f}", la, ln, typ)  # unknown home key: travel falls back to distance
        on_duty[f"PROP{n}"] = None
    by_type = defaultdict(list)
    for uid, h in fleet.items():
        by_type[h[3]].append(uid)
    incs = list(incidents)
    if volume != 1.0:  # resample: extra calls are copies of real calls shifted to a random time in the window
        rng = random.Random(seed)
        lo, hi = incs[0]["t"], incs[-1]["t"]
        hour = lambda i: datetime.fromtimestamp(i["t"], SF).hour
        pool = [i for i in incs if (not volume_area or km(volume_area[0], volume_area[1], i["lat"], i["lng"]) <= volume_area[2])
                and (not volume_hours or volume_hours[0] <= hour(i) < volume_hours[1])]
        extra = int(len(pool) * (volume - 1))
        if extra > 0 and pool:
            for k in range(extra):
                c = dict(rng.choice(pool))
                if volume_hours:  # same day, a random moment inside the window
                    tod = (datetime.fromtimestamp(c["t"], SF) - datetime.fromtimestamp(c["t"], SF).replace(hour=0, minute=0, second=0)).seconds
                    shift = rng.randint(volume_hours[0] * 3600, volume_hours[1] * 3600 - 1) - tod
                else:
                    shift = rng.randint(lo, hi) - c["t"]
                c["id"], c["t"] = f"{c['id']}+{k}", c["t"] + shift
                c["day"] = day(c["t"])  # the copy may land on another day: duty rosters and blocked windows use it
                c["units"] = [{**u, **{f: u[f] + shift if u[f] else None for f in ("dispatch", "enroute", "onscene", "available")}}
                              for u in c["units"]]
                incs.append(c)
        elif extra < 0:
            incs = rng.sample(incs, int(len(incs) * volume))
        incs.sort(key=lambda i: i["t"])
    free = defaultdict(lambda: -10**12)
    loc, home_at = {}, defaultdict(lambda: -10**12)  # where a unit is after its last call, and when it's back home
    hospitals = load_hospitals() if positions else []
    out = {}
    blocked = blocked or {}
    day0 = {}
    for i in incs:
        t0 = i["t"] + i["delay"] + extra_delay
        b = band(t0)
        if blocked:
            d = i["day"]
            if d not in day0:
                day0[d] = int(datetime.combine(datetime.fromordinal(d).date(), datetime.min.time(), SF).timestamp())
            tod = t0 - day0[d]
        used, arrivals = set(), []
        for u in i["units"]:
            best = None
            due = first_due and u["type"] in FIRST_DUE
            for uid in by_type[u["type"]]:
                if uid in used or (on_duty[uid] is not None and i["day"] not in on_duty[uid]):
                    continue
                if uid in blocked and blocked[uid][0] <= tod < blocked[uid][1]:
                    continue
                start = max(free[uid], t0)
                if positions and start < home_at[uid] and uid in loc:
                    origin = loc[uid]
                    eta = start - t0 + travel.from_point(origin[0], origin[1], i["lat"], i["lng"], b)
                else:
                    origin = None
                    eta = start - t0 + travel(fleet[uid], i["cell"], i["lat"], i["lng"], b)
                # first-due units win while they're free at dispatch; otherwise fastest wins
                rank = (0 if due and start <= t0 and fleet[uid][0] == "S" + i.get("area", "") else 1, eta)
                if best is None or rank < best[4]:
                    best = (eta, uid, origin, start, rank)
            if best is None:
                continue
            eta, uid, origin, start, _ = best
            used.add(uid)
            turnout = (u["enroute"] - u["dispatch"]) if u["enroute"] and 0 <= u["enroute"] - u["dispatch"] < 600 else 60
            arrive = t0 + turnout + eta
            on_scene = (u["available"] - u["onscene"]) if u["available"] and u["onscene"] and u["available"] > u["onscene"] else 1200
            free[uid] = arrive + on_scene
            arrivals.append(arrive)
            if positions:  # after the call the unit is at the hospital (if it transported) or still at the scene
                end = (min(hospitals, key=lambda h: (h[0] - i["lat"]) ** 2 + (h[1] - i["lng"]) ** 2)
                       if u.get("transport") and hospitals else (i["lat"], i["lng"]))
                h = fleet[uid]
                loc[uid] = end
                home_at[uid] = free[uid] + travel.from_point(end[0], end[1], h[1], h[2], band(free[uid]))
            if log is not None:
                h = fleet[uid]
                frm = (origin[1], origin[0]) if origin else (h[2], h[1])
                log.append({"unit": uid, "type": u["type"], "from": frm, "from_home": origin is None, "to": (i["lng"], i["lat"]),
                            "call": i["t"], "depart": arrive - eta + (start - t0), "arrive": arrive, "free": free[uid],
                            "incident": i["id"], "hood": i["hood"], "call_type": i["type"]})
        if arrivals:
            out[i["id"]] = min(arrivals) - i["t"] + offset_for(offset, i["t"])
    return out


def split(incidents, holdout_days=31):
    cut = incidents[-1]["t"] - holdout_days * 86400
    return [i for i in incidents if i["t"] < cut], [i for i in incidents if i["t"] >= cut]


def corr(xs, ys):
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return sxy / den if den else float("nan")  # constant predictor has no correlation


def score(test, sim):
    """Per-incident and per-neighborhood agreement between simulated and real response."""
    pairs = [(sim[i["id"]], i["real_response"], i["hood"]) for i in test if i["id"] in sim]
    err = sorted(abs(s - r) for s, r, _ in pairs)
    hood = defaultdict(lambda: ([], []))
    for s, r, h in pairs:
        hood[h][0].append(s)
        hood[h][1].append(r)
    hs = [(median(a), median(b)) for a, b in hood.values() if len(a) >= 30]
    slow = lambda xs: sum(x > 480 for x in xs) / len(xs)
    return {"incidents": len(pairs), "median_abs_err_s": err[len(err) // 2],
            "hood_corr": corr([a for a, _ in hs], [b for _, b in hs]),
            "hood_median_abs_err_s": median(abs(a - b) for a, b in hs),
            "pct_over_8min_sim": slow([s for s, _, _ in pairs]), "pct_over_8min_real": slow([r for _, r, _ in pairs])}


def calibrate(train, stations, positions=True, per_band=True, first_due=True):
    """Median gap (real - simulated) on the last training month, using a model fit on the months before it.
    Covers what the sim leaves out (dispatch reassignments, crews not ready, ...). per_band: one offset per
    time-of-day band instead of one for the whole day."""
    fit, cal = split(train)
    homes = unit_homes(fit, stations)
    sim = simulate(cal, homes, TravelModel(fit, homes), positions=positions, first_due=first_due)
    gaps = defaultdict(list)
    for i in cal:
        if i["id"] in sim:
            gaps["all"].append(i["real_response"] - sim[i["id"]])
            gaps[band(i["t"])].append(i["real_response"] - sim[i["id"]])
    if not per_band:
        return median(gaps["all"])
    return {k: median(v) for k, v in gaps.items() if len(v) >= 50}


def validate():
    incidents = load_incidents()
    train, test = split(incidents)
    stations = load_stations()
    homes = unit_homes(train, stations)
    learned = TravelModel(train, homes)
    straight = TravelModel(train, homes, mode="straight")
    print(f"{len(incidents)} incidents; train {len(train)}, held-out {len(test)}; {len(homes)} units; "
          f"{learned.n_trips} idle-start trips learned; {len(learned.exact)} station-area-hour medians")
    const = median(i["real_response"] for i in train)
    off_v1 = calibrate(train, stations, positions=False, per_band=False, first_due=False)
    off = calibrate(train, stations)
    print(f"calibration (fit on the last training month, before the held-out month): v1 flat {off_v1:+.0f}s; "
          f"v2 by time of day " + ", ".join(f"{k}:{v:+.0f}s" for k, v in sorted(off.items(), key=str)))
    results = {
        "baseline: city median": score(test, {i["id"]: const for i in test}),
        "baseline: straight-line, one speed": score(test, simulate(test, homes, straight, positions=False, first_due=False)),
        "runnr v1: units always leave from home, flat calibration": score(test, simulate(test, homes, learned, offset=off_v1, positions=False, first_due=False)),
        "runnr: learned + calibrated": score(test, simulate(test, homes, learned, offset=off)),
    }
    print(f"\n{'model':38}{'med |err|':>10}{'hood corr':>10}{'hood |err|':>11}{'>8min sim':>10}{'>8min real':>11}")
    for name, r in results.items():
        corr_s = "n/a" if math.isnan(r["hood_corr"]) else f"{r['hood_corr']:.2f}"
        print(f"{name:38}{r['median_abs_err_s']:>9.0f}s{corr_s:>10}{r['hood_median_abs_err_s']:>10.0f}s"
              f"{r['pct_over_8min_sim']:>10.1%}{r['pct_over_8min_real']:>11.1%}")
    # sanity checks: removing capacity must not make the city faster; adding load must not either
    base = simulate(test, homes, learned)
    busiest = Counter(h[0][1:] for h in homes.values() if h[0][0] == "S").most_common(1)[0][0]
    closed = simulate(test, homes, learned, closed=[busiest])
    surge = simulate(test, homes, learned, volume=1.5)
    mean = lambda d: sum(d.values()) / len(d)
    print(f"\nsanity: baseline mean {mean(base):.0f}s | close station {busiest}: {mean(closed):.0f}s | 1.5x calls: {mean(surge):.0f}s")
    assert mean(closed) >= mean(base) - 1 and mean(surge) >= mean(base) - 1, "capacity sanity check failed"
    for r in results.values():  # NaN isn't valid JSON for browsers
        r["hood_corr"] = None if math.isnan(r["hood_corr"]) else round(r["hood_corr"], 3)
    json.dump(results, open(BUILD / "validation.json", "w"), indent=1)


def summarize(test, sim, sample=4000):
    """Per-neighborhood and citywide results for the UI."""
    by_hood = defaultdict(list)
    for i in test:
        if i["id"] in sim:
            by_hood[i["hood"]].append(sim[i["id"]])
    stats = lambda xs: {"median": round(median(xs)), "over8": round(sum(x > 480 for x in xs) / len(xs), 3),
                        "calls": len(xs)}
    allv = [v for i in test for v in [sim.get(i["id"])] if v is not None]
    step = max(1, len(test) // sample)
    pts = [[i["lng"], i["lat"], round(sim[i["id"]])] for i in test[::step] if i["id"] in sim]
    return {"city": stats(allv), "hoods": {h: stats(v) for h, v in by_hood.items() if v}, "points": pts}


def export(path=BUILD / "runtime.pkl.gz"):
    """Everything the web server needs, fit once: model, fleet, calibration, held-out month."""
    import pickle
    incidents = load_incidents()
    train, test = split(incidents)
    stations = load_stations()
    homes = unit_homes(train, stations)
    model = TravelModel(train, homes)
    off = calibrate(train, stations)
    model.memo = {}
    with gzip.open(path, "wb") as f:
        pickle.dump({"model": model, "homes": homes, "offset": off, "test": test,
                     "period": [test[0]["t"], test[-1]["t"]], "n_incidents": len(incidents)}, f)
    print(f"runtime -> {path} ({path.stat().st_size / 1e6:.1f} MB), offset by time of day {off}, {len(test)} held-out incidents")


if __name__ == "__main__":
    import sys
    if "--export" in sys.argv:
        import sim  # re-import as a module so the pickle refers to sim.TravelModel, not __main__
        sim.export()
        sys.exit()
    assert band(int(datetime(2026, 1, 1, 8, tzinfo=SF).timestamp())) == 1
    assert abs(km(37.77, -122.42, 37.78, -122.42) - 1.11) < 0.01
    validate()
