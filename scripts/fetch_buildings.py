"""SF building footprints with LiDAR heights (DataSF ynuv-fyni) -> data/buildings.json

python3 fetch_buildings.py          download + build
python3 fetch_buildings.py --check  self-check the output

Output: {"buildings": [[height_m, [lng1, lat1, lng2, lat2, ...]], ...]}  (outer ring, flat coords)
Height = LiDAR median roof height above ground (hgt_median_m). Max height is noisy (antennas, cranes).
"""
import json
import math
import subprocess
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/buildings"
OUT = ROOT / "data/sf/buildings.json"
BBOX = (-122.528, 37.700, -122.350, 37.835)
DOWNTOWN = (-122.425, 37.770, -122.385, 37.800)
MAX_BYTES = 12_000_000
MIN_STEP_M = 1.5


def get(params):
    # curl, not urllib: python.org builds on macOS often can't verify certificates
    url = "https://data.sfgov.org/resource/ynuv-fyni.json?" + urllib.parse.urlencode(params)
    return json.loads(subprocess.run(["curl", "-sfL", "--max-time", "600", url], capture_output=True, check=True).stdout)


def download():
    RAW.mkdir(parents=True, exist_ok=True)
    rows, offset, page = [], 0, 50000
    while True:
        batch = get({"$select": "sf16_bldgid,hgt_median_m,shape", "$order": "sf16_bldgid", "$limit": page, "$offset": offset})
        rows += batch
        print("downloaded", len(rows))
        if len(batch) < page:
            break
        offset += page
    (RAW / "footprints.json").write_text(json.dumps(rows))
    return rows


def meters(a, b, lat):
    return 6371000 * math.hypot(math.radians(b[0] - a[0]) * math.cos(math.radians(lat)), math.radians(b[1] - a[1]))


def seg_dist(p, a, b, lat):
    """Meters from p to segment ab (local flat approximation)."""
    k = math.cos(math.radians(lat)) * 111320
    ax, ay, bx, by, px, py = a[0] * k, a[1] * 110540, b[0] * k, b[1] * 110540, p[0] * k, p[1] * 110540
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1)))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def douglas_peucker(pts, tol, lat):
    keep, stack = {0, len(pts) - 1}, [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        best, idx = 0.0, None
        for k in range(i + 1, j):
            d = seg_dist(pts[k], pts[i], pts[j], lat)
            if d > best:
                best, idx = d, k
        if idx is not None and best > tol:
            keep.add(idx)
            stack += [(i, idx), (idx, j)]
    return [pts[k] for k in sorted(keep)]


def simplify(ring):
    """Drop points within MIN_STEP_M of the shape (Douglas-Peucker on the ring split at its far point)."""
    if ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 4:
        return ring
    lat = ring[0][1]
    far = max(range(len(ring)), key=lambda k: meters(ring[0], ring[k], lat))
    a = douglas_peucker(ring[:far + 1], MIN_STEP_M, lat)
    b = douglas_peucker(ring[far:] + [ring[0]], MIN_STEP_M, lat)
    return a + b[1:-1]


def area(ring):  # shoelace in degrees², only used to rank small footprints
    return abs(sum(ring[i][0] * ring[i - 1][1] - ring[i - 1][0] * ring[i][1] for i in range(len(ring)))) / 2


def inside(p, box):
    return box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]


def build(rows):
    kept = []
    for r in rows:
        h, shape = r.get("hgt_median_m"), r.get("shape")
        if not h or not shape or float(h) < 3:
            continue
        polys = shape["coordinates"] if shape["type"] == "MultiPolygon" else [shape["coordinates"]]
        outer = max((p[0] for p in polys), key=len)
        ring = simplify(outer)
        if len(ring) < 4 or not all(inside(p, BBOX) for p in ring):
            continue
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        kept.append((round(float(h)), ring, area(ring), inside((cx, cy), DOWNTOWN)))
    total = len(kept)

    def dump(items):
        return json.dumps({"buildings": [[h, [round(c, 5) for p in ring for c in p]] for h, ring, _, _ in items]},
                          separators=(",", ":"))

    text = dump(kept)
    if len(text) > MAX_BYTES:  # drop the smallest footprints outside downtown until it fits
        keep_dt = [k for k in kept if k[3]]
        rest = sorted((k for k in kept if not k[3]), key=lambda k: -k[2])
        lo, hi = 0, len(rest)
        while lo < hi:  # binary search: most outside-downtown buildings that still fit
            mid = (lo + hi + 1) // 2
            if len(dump(keep_dt + rest[:mid])) <= MAX_BYTES:
                lo = mid
            else:
                hi = mid - 1
        kept = keep_dt + rest[:lo]
        text = dump(kept)
    OUT.write_text(text)
    print(f"kept {len(kept)} of {total} buildings (>= 3 m, in bbox) -> {OUT} ({len(text) / 1e6:.1f} MB)")


def check():
    d = json.loads(OUT.read_text())
    b = d["buildings"]
    assert isinstance(b, list) and len(b) > 1000, len(b)
    for h, coords in b:
        assert isinstance(h, int) and 3 <= h <= 330, h
        assert len(coords) >= 8 and len(coords) % 2 == 0
        assert all(BBOX[0] <= coords[i] <= BBOX[2] and BBOX[1] <= coords[i + 1] <= BBOX[3] for i in range(0, len(coords), 2))
    tall = sorted(b, key=lambda x: -x[0])[:8]
    near_sf_tower = [h for h, c in tall if abs(c[0] + 122.3969) < 0.002 and abs(c[1] - 37.7897) < 0.002]
    assert near_sf_tower, "Salesforce Tower should be among the tallest"
    print(f"check ok: {len(b)} buildings; tallest:", [(h, c[0], c[1]) for h, c in tall])


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        raw = RAW / "footprints.json"
        build(json.loads(raw.read_text()) if raw.exists() and "--refresh" not in sys.argv else download())
        check()
