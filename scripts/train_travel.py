"""Machine-learned travel times + rolling-month cross-validation.

    python3 scripts/train_travel.py            # cross-validate (trips + full simulator), then export the table
    python3 scripts/train_travel.py --cv-only  # just the cross-validation report

Model: gradient-boosted trees (scikit-learn HistGradientBoostingRegressor, absolute-error loss on log seconds)
trained on real trips that started at a unit's home (idle >= 15 min), the same trips the lookup model uses.
Features: straight-line + street-network distance (DataSF centerline shortest paths), east/north offsets,
origin/destination coordinates, climb (elevation grid), time of day, unit type, home.

Cross-validation is rolling-origin by month: for each test month (Jul-Oct 2026) everything is fit on data
strictly before it. Two levels:
  1. trips: predicted vs real travel seconds, ML vs the median-lookup model
  2. full simulator: replay the month, compare neighborhood median response with reality (as in sim.py)

Training needs scikit-learn (local only). The output is a plain lookup table (home x 500 m cell x hour band)
in data/build/travel_ml.pkl.gz, so the server stays standard-library only.
"""
import array
import gzip
import json
import math
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/backend"))
import routing  # noqa: E402
import sim  # noqa: E402
from paths import BUILD, SF  # noqa: E402

OUT = BUILD / "travel_ml.pkl.gz"
CV_OUT = BUILD / "cv.json"
TEST_MONTHS = ["2026-07", "2026-08", "2026-09", "2026-10"]
BAND_HOUR = {0: 3, 1: 8, 2: 13, 3: 18, 4: 22}
TYPES = ["ENGINE", "TRUCK", "MEDIC", "PRIVATE", "RESCUE SQUAD"]


# ---------- street-network distance from each home (tree in routes.pkl.gz) ----------
class NetDist:
    def __init__(self):
        g, src, pred = routing._load()
        self.g, self.src, self.dist = g, src, {}
        for key, (s, _, _) in src.items():
            p = pred[key]
            d = array.array("d", [math.nan]) * len(p)
            d[s] = 0.0
            # pred is a tree: resolve each node's distance by walking up to a known ancestor (memoized)
            for v in range(len(p)):
                if not math.isnan(d[v]) or p[v] == -1:
                    continue
                chain, u = [], v
                while u != -1 and math.isnan(d[u]):
                    chain.append(u)
                    u = p[u]
                base = d[u] if u != -1 else math.nan
                for w in reversed(chain):
                    pw = p[w]
                    base = base + routing.meters(g.lat[w], g.lng[w], g.lat[pw], g.lng[pw]) if pw != -1 else math.nan
                    d[w] = base
            self.dist[key] = d
        self.cache = {}

    def __call__(self, key, lat, lng):
        """meters along streets from home `key` to (lat, lng); NaN if unknown."""
        ck = (key, round(lat, 4), round(lng, 4))
        if ck in self.cache:
            return self.cache[ck]
        out = math.nan
        if key in self.dist:
            t = self.g.snap(lat, lng)
            if t is not None:
                s, slat, slng = self.src[key]
                out = self.dist[key][t] + routing.meters(lat, lng, self.g.lat[t], self.g.lng[t]) \
                    + routing.meters(slat, slng, self.g.lat[s], self.g.lng[s])
        self.cache[ck] = out
        return out


# ---------- elevation ----------
E = json.load(open(SF / "elevation.json"))


def elev(lat, lng):
    fx, fy = (lng - E["bbox"][0]) / E["step"][0], (lat - E["bbox"][1]) / E["step"][1]
    i, j = min(max(int(fx), 0), E["nx"] - 1), min(max(int(fy), 0), E["ny"] - 1)
    return E["rows"][j][i]


# ---------- dataset ----------
def month_of(t):
    return datetime.fromtimestamp(t, sim.SF).strftime("%Y-%m")


def trips(incidents, homes):
    """Real trips from home (idle >= 15 min), as in sim.TravelModel: (home, unit type, dest lat/lng, cell, t, seconds)."""
    last_free, out = {}, []
    for i in incidents:
        for u in i["units"]:
            h = homes.get(u["unit"])
            idle = u["dispatch"] - last_free.get(u["unit"], -10**12)
            if u["available"]:
                last_free[u["unit"]] = u["available"]
            if not h or not (u["enroute"] and u["onscene"]) or idle < sim.IDLE_S:
                continue
            tt = u["onscene"] - u["enroute"]
            if 0 < tt < 1800:
                out.append((h, u["type"], i["lat"], i["lng"], i["cell"], u["enroute"], tt))
    return out


def features(home, utype, lat, lng, hour, net, home_idx):
    km = sim.km(home[1], home[2], lat, lng)
    dx = (lng - home[2]) * 111.32 * math.cos(math.radians(lat))
    dy = (lat - home[1]) * 111.32
    nk = net / 1000 if not math.isnan(net) else km * 1.3
    return [km, nk, nk / max(km, 0.05), dx, dy, lat, lng, home[1], home[2], elev(lat, lng) - elev(home[1], home[2]),
            elev(lat, lng), math.sin(2 * math.pi * hour / 24), math.cos(2 * math.pi * hour / 24),
            TYPES.index(utype) if utype in TYPES else 0, home_idx]


CAT = [13, 14]  # unit type, home index


def matrix(rows, netd, home_idx):
    X, y = [], []
    for h, utype, lat, lng, _, t, tt in rows:
        hour = datetime.fromtimestamp(t, sim.SF).hour + datetime.fromtimestamp(t, sim.SF).minute / 60
        X.append(features(h, utype, lat, lng, hour, netd(h[0], lat, lng), home_idx.get(h[0], 0)))
        y.append(tt)
    return np.array(X, dtype=float), np.array(y, dtype=float)


def fit(X, y):
    m = HistGradientBoostingRegressor(loss="absolute_error", max_iter=500, learning_rate=0.08, max_leaf_nodes=63,
                                      min_samples_leaf=40, l2_regularization=1.0, categorical_features=CAT, random_state=0)
    m.fit(X, np.log(y))
    return m


def table(model, homes, cells, netd, home_idx, stations):
    """Predicted seconds for every (home, cell, band): what the simulator needs. Homes = units + every station."""
    keys = {h[0]: (h[0], h[1], h[2], h[3]) for h in homes.values()}
    for st, (la, ln) in stations.items():
        keys.setdefault("S" + st, ("S" + st, la, ln, "ENGINE"))
    rows, idx = [], []
    for key, h in keys.items():
        for ci, c in enumerate(cells):
            lat, lng = (c[0] + 0.5) * sim.CELL_LAT, (c[1] + 0.5) * sim.CELL_LNG
            net = netd(key, lat, lng)
            for b, hour in BAND_HOUR.items():
                rows.append(features(h, h[3], lat, lng, hour, net, home_idx.get(key, 0)))
                idx.append((key, ci, b))
    pred = np.exp(model.predict(np.array(rows, dtype=float)))
    out = {}
    for (key, ci, b), p in zip(idx, pred):
        out.setdefault(key, array.array("H", [0]) * (len(cells) * 5))[ci * 5 + b] = int(min(65535, max(1, round(p))))
    return {"cells": {c: i for i, c in enumerate(cells)}, "table": out}


# ---------- simulator wrapper using the table ----------
class TableTravel(sim.TravelModel):
    """Same interface as sim.TravelModel; (home, cell, band) answered by the ML table, the rest by the lookup model."""
    def __init__(self, base, tab):  # noqa: super().__init__ intentionally skipped: we wrap a fitted model
        self.__dict__.update(base.__dict__)
        self.ml_cells, self.ml_table, self.memo = tab["cells"], tab["table"], {}

    def __call__(self, home, i_cell, lat, lng, b):
        ci, arr = self.ml_cells.get(i_cell), self.ml_table.get(home[0])
        if ci is not None and arr is not None:
            return arr[ci * 5 + b]
        return super().__call__(home, i_cell, lat, lng, b)


def err_stats(pred, real):
    e = np.abs(np.array(pred) - np.array(real))
    return {"mae_s": round(float(e.mean()), 1), "median_ae_s": round(float(np.median(e)), 1),
            "within_60s": round(float((e <= 60).mean()), 3), "n": int(len(e))}


def main():
    t0 = time.time()
    incidents = sim.load_incidents()
    stations = sim.load_stations()
    netd = NetDist()
    print(f"loaded {len(incidents)} incidents, street distances for {len(netd.dist)} homes ({time.time() - t0:.0f}s)")
    report = {"months": {}, "method": "rolling-origin: each month predicted from data strictly before it"}
    for m in TEST_MONTHS:
        train = [i for i in incidents if month_of(i["t"]) < m]
        test = [i for i in incidents if month_of(i["t"]) == m]
        calm = [i for i in incidents if month_of(i["t"]) == max(month_of(j["t"]) for j in train)]
        fit_cal = [i for i in train if i not in calm] if False else [i for i in train if month_of(i["t"]) < month_of(calm[0]["t"])]
        homes = sim.unit_homes(train, stations)
        home_idx = {k: n for n, k in enumerate(sorted({h[0] for h in homes.values()} | {"S" + s for s in stations}))}
        # --- level 1: trips ---
        lookup = sim.TravelModel(train, homes)
        Xtr, ytr = matrix(trips(train, homes), netd, home_idx)
        te = trips(test, homes)
        Xte, yte = matrix(te, netd, home_idx)
        gbm = fit(Xtr, ytr)
        ml_pred = np.exp(gbm.predict(Xte))
        look_pred = [lookup(h, cell, lat, lng, sim.band(t)) for h, _, lat, lng, cell, t, _ in te]
        # --- level 2: full simulator, calibrated on the month before (models fit strictly before that month) ---
        cells = sorted({i["cell"] for i in test} | {i["cell"] for i in calm})
        tab = table(gbm, homes, cells, netd, home_idx, stations)
        ml_travel = TableTravel(lookup, tab)
        homes_c = sim.unit_homes(fit_cal, stations)
        look_c = sim.TravelModel(fit_cal, homes_c)
        Xc, yc = matrix(trips(fit_cal, homes_c), netd, home_idx)
        gbm_c = fit(Xc, yc)
        ml_c = TableTravel(look_c, table(gbm_c, homes_c, sorted({i["cell"] for i in calm}), netd, home_idx, stations))

        def off_for(model, hs):
            s = sim.simulate(calm, hs, model)
            gaps = defaultdict(list)
            for i in calm:
                if i["id"] in s:
                    gaps[sim.band(i["t"])].append(i["real_response"] - s[i["id"]])
                    gaps["all"].append(i["real_response"] - s[i["id"]])
            return {k: median(v) for k, v in gaps.items() if len(v) >= 50}

        res = {"trips": {"lookup": err_stats(look_pred, yte), "ml": err_stats(ml_pred, yte)}, "sim": {}}
        for name, model, cal_model, cal_homes in [("lookup", lookup, look_c, homes_c), ("ml", ml_travel, ml_c, homes_c)]:
            off = off_for(cal_model, cal_homes)
            res["sim"][name] = sim.score(test, sim.simulate(test, homes, model, offset=off))
            res["sim"][name] = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in res["sim"][name].items()}
        report["months"][m] = res
        tr = res["trips"]
        print(f"{m}: trips MAE lookup {tr['lookup']['mae_s']}s vs ML {tr['ml']['mae_s']}s (n={tr['ml']['n']}) | "
              f"sim hood r lookup {res['sim']['lookup']['hood_corr']} vs ML {res['sim']['ml']['hood_corr']}, "
              f"hood err {res['sim']['lookup']['hood_median_abs_err_s']}s vs {res['sim']['ml']['hood_median_abs_err_s']}s, "
              f"call err {res['sim']['lookup']['median_abs_err_s']}s vs {res['sim']['ml']['median_abs_err_s']}s ({time.time() - t0:.0f}s)")
    # summary: mean +- sd across months
    summ = {}
    for level, keys in [("trips", ["mae_s", "median_ae_s", "within_60s"]), ("sim", ["hood_corr", "hood_median_abs_err_s", "median_abs_err_s"])]:
        for name in ["lookup", "ml"]:
            for k in keys:
                xs = [report["months"][m][level][name][k] for m in TEST_MONTHS]
                summ[f"{level}.{name}.{k}"] = {"mean": round(float(np.mean(xs)), 3), "sd": round(float(np.std(xs)), 3)}
    report["summary"] = summ
    CV_OUT.write_text(json.dumps(report, indent=1))
    print("\nsummary (mean ± sd over", ", ".join(TEST_MONTHS) + "):")
    for k, v in summ.items():
        print(f"  {k:36} {v['mean']} ± {v['sd']}")
    if "--cv-only" in sys.argv:
        return
    # export: model fit on everything before the app's held-out month (Oct 2026), table over every cell
    m = TEST_MONTHS[-1]
    train = [i for i in incidents if month_of(i["t"]) < m]
    homes = sim.unit_homes(train, stations)
    home_idx = {k: n for n, k in enumerate(sorted({h[0] for h in homes.values()} | {"S" + s for s in stations}))}
    X, y = matrix(trips(train, homes), netd, home_idx)
    gbm = fit(X, y)
    cells = sorted({i["cell"] for i in incidents})
    tab = table(gbm, homes, cells, netd, home_idx, stations)
    with gzip.open(OUT, "wb") as f:
        pickle.dump(tab, f, protocol=4)
    print(f"exported {len(tab['table'])} homes x {len(cells)} cells x 5 bands -> {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
