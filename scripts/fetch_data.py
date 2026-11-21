"""Download every dataset Runnr uses into data/. Run once; outputs are committed so deploys don't need it.

- calls.csv.gz      SF Fire Dept calls for service, every unit dispatched (DataSF nuek-vuh3)
- stations.json     SF fire station locations (DataSF wt27-4jim, City-owned facilities)
- hoods.geojson     SF Analysis Neighborhood boundaries (DataSF j2bu-swwd)
- people.json       population by neighborhood, ACS 2019-2023 (DataSF 4qbq-hvtt)
"""
import csv
import gzip
import io
import json
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data/sf"
START, END = "2024-11-01", "2026-11-01"


def get(dataset, fmt="json", **params):
    # curl, not urllib: python.org builds on macOS often can't verify certificates
    url = f"https://data.sf.gov/resource/{dataset}.{fmt}?" + urllib.parse.urlencode(params)
    return subprocess.run(["curl", "-sfL", "--max-time", "600", url], capture_output=True, check=True).stdout.decode()


def ts(s):
    return int(datetime.fromisoformat(s).timestamp()) if s else ""


def calls():
    fields = ["incident_number", "unit_sequence_in_call_dispatch", "received_dttm", "dispatch_dttm", "response_dttm",
              "on_scene_dttm", "available_dttm", "unit_id", "unit_type", "station_area", "case_location",
              "neighborhoods_analysis_boundaries", "call_type", "call_type_group", "final_priority"]
    path = OUT / "calls.csv.gz"
    n = 0
    with gzip.open(path, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["incident", "seq", "received", "dispatch", "enroute", "onscene", "available", "unit", "unit_type",
                    "station", "lat", "lng", "hood", "call_type", "group", "priority"])
        offset, page = 0, 100000
        while True:
            text = get("nuek-vuh3", "csv", **{
                "$select": ",".join(fields),
                "$where": f"received_dttm >= '{START}' AND received_dttm < '{END}' AND case_location IS NOT NULL",
                "$order": "received_dttm, incident_number, unit_sequence_in_call_dispatch",
                "$limit": page, "$offset": offset})
            batch = list(csv.DictReader(io.StringIO(text)))
            for r in batch:
                lng, lat = map(float, r["case_location"].removeprefix("POINT (").rstrip(")").split())
                w.writerow([r["incident_number"], r["unit_sequence_in_call_dispatch"],
                            *(ts(r[k]) for k in ["received_dttm", "dispatch_dttm", "response_dttm", "on_scene_dttm",
                                                 "available_dttm"]),
                            r["unit_id"], r["unit_type"], r["station_area"], f"{lat:.5f}", f"{lng:.5f}",
                            r["neighborhoods_analysis_boundaries"], r["call_type"], r["call_type_group"],
                            r["final_priority"]])
            n += len(batch)
            print(f"calls: {n}")
            if len(batch) < page:
                break
            offset += page
    print(f"calls -> {path} ({path.stat().st_size / 1e6:.1f} MB)")


def stations():
    rows = json.loads(get("wt27-4jim", **{"$where": "common_name like 'Fire Station #%'", "$limit": 200}))
    out = sorted(({"station": r["common_name"].split("#")[1].strip(), "name": r["common_name"], "address": r["address"],
                   "lat": float(r["latitude"]), "lng": float(r["longitude"])} for r in rows),
                 key=lambda s: int(s["station"]) if s["station"].isdigit() else 999)
    json.dump(out, open(OUT / "stations.json", "w"), indent=1)
    print(f"stations: {len(out)}")


def simplify_ring(ring, tol=0.00004):
    """Douglas-Peucker in degrees (~4 m): city-scale outlines don't need survey-grade detail."""
    if len(ring) < 5:
        return ring
    def rec(pts):
        (x1, y1), (x2, y2) = pts[0], pts[-1]
        dx, dy = x2 - x1, y2 - y1
        n = (dx * dx + dy * dy) ** 0.5 or 1e-12
        far, idx = 0.0, 0
        for i in range(1, len(pts) - 1):
            d = abs(dy * pts[i][0] - dx * pts[i][1] + x2 * y1 - y2 * x1) / n
            if d > far:
                far, idx = d, i
        if far <= tol:
            return [pts[0], pts[-1]]
        return rec(pts[:idx + 1])[:-1] + rec(pts[idx:])
    # closed ring: split at the point farthest from the start, simplify each half
    far = max(range(len(ring)), key=lambda i: (ring[i][0] - ring[0][0]) ** 2 + (ring[i][1] - ring[0][1]) ** 2)
    out = rec(ring[:far + 1])[:-1] + rec(ring[far:])
    return out if len(out) >= 4 else ring


def simplify_geojson(gj):
    for f in gj["features"]:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for poly in polys:
            for k, ring in enumerate(poly):
                poly[k] = [[round(x, 5), round(y, 5)] for x, y in simplify_ring(ring)]
        f["properties"] = {"nhood": f["properties"]["nhood"]}
    return gj


def hoods():
    gj = simplify_geojson(json.loads(get("j2bu-swwd", "geojson", **{"$limit": 100})))
    (OUT / "hoods.geojson").write_text(json.dumps(gj, separators=(",", ":")))
    print("hoods:", len(gj["features"]))


def people():
    rows = json.loads(get("4qbq-hvtt", **{
        "$where": "geography='neighborhood' AND end_year='2023' AND unit='population count'",
        "$select": "geography_name, demographic_category, demographic_category_label, estimate", "$limit": 5000}))
    out = {}
    for r in rows:
        h = out.setdefault(r["geography_name"], {})
        label = "total" if r["demographic_category"] == "all" else r["demographic_category_label"].rstrip(":")
        h[label] = round(float(r["estimate"]))
    for h in out.values():
        h["age_65_plus"] = h.get("65 to 74 years", 0) + h.get("75 years and over", 0)
    json.dump(out, open(OUT / "people.json", "w"), indent=1)
    print(f"people: {len(out)} neighborhoods")


def transports(start="2026-09-01", end="2026-11-01"):
    """Which units took a patient to hospital (calibration + held-out months): incident, unit, transport, hospital."""
    path = OUT / "transports.csv.gz"
    text = get("nuek-vuh3", "csv", **{
        "$select": "incident_number, unit_id, transport_dttm, hospital_dttm",
        "$where": f"received_dttm >= '{start}' AND received_dttm < '{end}' AND transport_dttm IS NOT NULL",
        "$limit": 200000})
    rows = list(csv.DictReader(io.StringIO(text)))
    with gzip.open(path, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["incident", "unit", "transport", "hospital"])
        for r in rows:
            w.writerow([r["incident_number"], r["unit_id"], ts(r["transport_dttm"]), ts(r["hospital_dttm"])])
    print(f"transports: {len(rows)} -> {path}")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    stations()
    hoods()
    people()
    calls()
