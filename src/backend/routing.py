"""Street routing for Runnr: shortest paths on San Francisco's street centerlines.

Source: DataSF "Streets – Active and Retired" (3psu-pn9h), active segments only:
  curl -L -o data/raw/streets/streets.geojson \
    'https://data.sfgov.org/resource/3psu-pn9h.geojson?$where=active=true&$select=cnn,classcode,streetname,line&$limit=50000'

python3 src/backend/routing.py --build   -> data/build/routes.pkl.gz (one shortest-path tree per unit home and station)
python3 src/backend/routing.py           -> self-check

Graph is undirected (emergency vehicles ignore one-way streets), largest connected component only,
edge weight = meters. Paths only drive the shape; trip timing still comes from the travel-time model.
"""
import array
import gzip
import heapq
import json
import math
import pickle
import sys
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path

import sim

from paths import BUILD, RAW, SF  # noqa: F401
ROADS = RAW / "streets/streets.geojson"
OUT = BUILD / "routes.pkl.gz"
BUCKET = 0.002  # ~200 m snap grid
TOL_M = 8  # Douglas-Peucker tolerance


def meters(lat1, lng1, lat2, lng2):
    return sim.km(lat1, lng1, lat2, lng2) * 1000


class Graph:
    def __init__(self, lat, lng, off, nbr, wt):
        self.lat, self.lng, self.off, self.nbr, self.wt = lat, lng, off, nbr, wt
        self.grid = defaultdict(list)
        for i in range(len(lat)):
            self.grid[(int(lat[i] / BUCKET), int(lng[i] / BUCKET))].append(i)

    def snap(self, lat, lng, max_rings=3):
        ci, cj = int(lat / BUCKET), int(lng / BUCKET)
        for r in range(1, max_rings + 1):
            best = None
            for di in range(-r, r + 1):
                for dj in range(-r, r + 1):
                    for n in self.grid.get((ci + di, cj + dj), ()):
                        d = (self.lat[n] - lat) ** 2 + ((self.lng[n] - lng) * 0.79) ** 2  # cos(37.7°)
                        if best is None or d < best[0]:
                            best = (d, n)
            if best:
                return best[1]
        return None

    def dijkstra(self, src):
        n = len(self.lat)
        dist = array.array("d", [math.inf]) * n
        pred = array.array("i", [-1]) * n
        dist[src] = 0.0
        pq = [(0.0, src)]
        off, nbr, wt = self.off, self.nbr, self.wt
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for k in range(off[u], off[u + 1]):
                v, nd = nbr[k], d + wt[k]
                if nd < dist[v]:
                    dist[v], pred[v] = nd, u
                    heapq.heappush(pq, (nd, v))
        return pred


def load_graph():
    # segments meet at shared vertices: a node is a vertex coordinate rounded to ~0.1 m
    coords, adj = {}, defaultdict(set)
    for f in json.load(open(ROADS))["features"]:
        g = f.get("geometry") or {}
        lines = [g["coordinates"]] if g.get("type") == "LineString" else g.get("coordinates", [])
        for line in lines:
            ns = []
            for x, y in line:
                k = (round(x, 6), round(y, 6))
                coords[k] = (y, x)
                ns.append(k)
            for a, b in zip(ns, ns[1:]):
                if a != b:
                    adj[a].add(b)
                    adj[b].add(a)
    # largest connected component
    seen, best = set(), []
    for s in adj:
        if s in seen:
            continue
        comp, q = [s], deque([s])
        seen.add(s)
        while q:
            for v in adj[q.popleft()]:
                if v not in seen:
                    seen.add(v)
                    comp.append(v)
                    q.append(v)
        if len(comp) > len(best):
            best = comp
    idx = {osm: i for i, osm in enumerate(best)}
    lat = array.array("d", (coords[o][0] for o in best))
    lng = array.array("d", (coords[o][1] for o in best))
    off, nbr, wt = array.array("i", [0]), array.array("i"), array.array("f")
    for o in best:
        for v in adj[o]:
            nbr.append(idx[v])
            wt.append(meters(lat[idx[o]], lng[idx[o]], lat[idx[v]], lng[idx[v]]))
        off.append(len(nbr))
    return Graph(lat, lng, off, nbr, wt)


def build():
    import time
    t = time.time()
    g = load_graph()
    print(f"graph: {len(g.lat)} nodes, {len(g.nbr) // 2} edges ({time.time() - t:.1f}s)")
    with gzip.open(BUILD / "runtime.pkl.gz", "rb") as f:
        homes = pickle.load(f)["homes"]
    sources = {h[0]: (h[1], h[2]) for h in homes.values()}
    sources.update({"S" + k: v for k, v in sim.load_stations().items()})
    src, pred = {}, {}
    for key, (la, ln) in sources.items():
        n = g.snap(la, ln)
        if n is None:
            continue
        src[key] = (n, la, ln)
        pred[key] = g.dijkstra(n)
    print(f"trees: {len(pred)} sources ({time.time() - t:.1f}s)")
    with gzip.open(OUT, "wb") as f:
        pickle.dump({"lat": g.lat, "lng": g.lng, "src": src, "pred": pred}, f, protocol=4)
    print(f"-> {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


_R = None


def _load():
    global _R
    if _R is None:
        with gzip.open(OUT, "rb") as f:
            r = pickle.load(f)
        g = Graph.__new__(Graph)
        g.lat, g.lng = r["lat"], r["lng"]
        g.grid = defaultdict(list)
        for i in range(len(g.lat)):
            g.grid[(int(g.lat[i] / BUCKET), int(g.lng[i] / BUCKET))].append(i)
        _R = (g, r["src"], r["pred"])
    return _R


def simplify(pts, tol=TOL_M):
    """Douglas-Peucker on [lng, lat] points, tolerance in meters."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    k = 111320 * 0.79  # meters per degree lng at SF; lat ~111320
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = pts[a], pts[b]
        ax, ay, bx, by = x1 * k, y1 * 111320, x2 * k, y2 * 111320
        L = math.hypot(bx - ax, by - ay) or 1e-9
        far, fd = None, tol
        for i in range(a + 1, b):
            px, py = pts[i][0] * k, pts[i][1] * 111320
            d = abs((bx - ax) * (ay - py) - (ax - px) * (by - ay)) / L
            if d > fd:
                far, fd = i, d
        if far is not None:
            keep[far] = True
            stack += [(a, far), (far, b)]
    return [p for p, kp in zip(pts, keep) if kp]


@lru_cache(maxsize=20000)
def route(home_key, lng, lat):
    """[[lng, lat], ...] along streets from a unit's home to (lng, lat), or None."""
    g, src, pred = _load()
    s, p = src.get(home_key), pred.get(home_key)
    if s is None:
        return None
    t = g.snap(lat, lng)
    if t is None or (p[t] == -1 and t != s[0]):
        return None
    nodes = []
    while t != -1:
        nodes.append(t)
        t = p[t]
    pts = [[s[2], s[1]]] + [[g.lng[n], g.lat[n]] for n in reversed(nodes)] + [[lng, lat]]
    return [[round(x, 5), round(y, 5)] for x, y in simplify(pts)]


def length(pts):
    return sum(meters(a[1], a[0], b[1], b[0]) for a, b in zip(pts, pts[1:]))


if __name__ == "__main__":
    if "--build" in sys.argv:
        build()
        sys.exit()
    st = sim.load_stations()["1"]  # (lat, lng)
    tgt = (-122.4194, 37.7749)  # Civic Center, ~1.4 km away
    r = route("S1", *tgt)
    assert r and len(r) > 2, r
    assert meters(r[0][1], r[0][0], *st) < 50, "route must start at the station"
    assert r[-1] == [round(tgt[0], 5), round(tgt[1], 5)], "route must end at the target"
    assert length(r) >= meters(st[0], st[1], tgt[1], tgt[0]), "street path can't beat a straight line"
    print(f"self-check ok: {len(r)} points, {length(r):.0f} m vs {meters(st[0], st[1], tgt[1], tgt[0]):.0f} m straight")
