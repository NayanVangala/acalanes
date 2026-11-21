"""SFPD dispatched calls (DataSF 2zdj-bwza, closed calls) for the replay month + district stations (wt27-4jim).

python3 scripts/fetch_police.py   -> data/sf/police_calls.csv.gz, data/sf/police_stations.json
Real records, not simulated: one row per call with dispatch, on-scene and close times (epoch seconds).
Officer-initiated stops (onview_flag='Y') are dropped: nobody was dispatched to them.
"""
import csv
import gzip
import io
import json
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/sf"
START, END = "2026-10-01", "2026-11-01"  # replay month (the simulator's held-out month)
SF = ZoneInfo("America/Los_Angeles")
# wt27-4jim common_name -> district; Southern station sits in the Public Safety Building
STATIONS = {"Central Station": "CENTRAL", "Southern": "SOUTHERN", "Public Safety Building": "SOUTHERN",
            "Bayview Station": "BAYVIEW", "Mission Station": "MISSION", "Northern Station": "NORTHERN",
            "Park Station": "PARK", "Richmond Station": "RICHMOND", "Ingleside Police Station": "INGLESIDE",
            "Taraval Station": "TARAVAL", "Tenderloin Station": "TENDERLOIN"}


def get(dataset, fmt="json", **params):
    # curl, not urllib: python.org builds on macOS often can't verify certificates
    url = f"https://data.sfgov.org/resource/{dataset}.{fmt}?" + urllib.parse.urlencode(params)
    return subprocess.run(["curl", "-sfL", "--max-time", "600", url], capture_output=True, check=True).stdout.decode()


def ts(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=SF).timestamp()) if s else ""


def stations():
    rows = json.loads(get("wt27-4jim", **{"$where": "department_name like '%Police%'", "$limit": 200}))
    out = {}
    for r in rows:
        d = STATIONS.get(r["common_name"])
        if d and d not in out:
            out[d] = {"name": f"{d.title()} Station", "district": d, "address": r["address"].title(),
                      "lat": round(float(r["latitude"]), 6), "lng": round(float(r["longitude"]), 6)}
    assert len(out) == 10, sorted(out)
    json.dump(sorted(out.values(), key=lambda s: s["district"]), open(OUT / "police_stations.json", "w"), indent=1)
    print("police stations:", len(out))


def calls():
    fields = ["cad_number", "dispatch_datetime", "onscene_datetime", "close_datetime", "intersection_point",
              "police_district", "call_type_final_desc", "priority_final"]
    rows, offset, page = [], 0, 50000
    while True:
        text = get("2zdj-bwza", "csv", **{
            "$select": ",".join(fields),
            "$where": f"dispatch_datetime >= '{START}' AND dispatch_datetime < '{END}' AND onscene_datetime IS NOT NULL "
                      "AND intersection_point IS NOT NULL AND onview_flag='N' AND police_district IS NOT NULL",
            "$order": "dispatch_datetime, cad_number", "$limit": page, "$offset": offset})
        batch = list(csv.DictReader(io.StringIO(text)))
        rows += batch
        if len(batch) < page:
            break
        offset += page
    kept = 0
    with gzip.open(OUT / "police_calls.csv.gz", "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cad", "dispatch", "onscene", "close", "lat", "lng", "district", "call_type", "priority"])
        for r in rows:
            d, o, c = ts(r["dispatch_datetime"]), ts(r["onscene_datetime"]), ts(r["close_datetime"])
            if not (d and o) or not 0 <= o - d < 3 * 3600:
                continue
            lng, lat = map(float, r["intersection_point"].removeprefix("POINT (").rstrip(")").split())
            if not (37.6 < lat < 37.85 and -122.55 < lng < -122.3):
                continue
            w.writerow([r["cad_number"], d, o, c, f"{lat:.5f}", f"{lng:.5f}", r["police_district"].upper(),
                        r["call_type_final_desc"].capitalize(), r["priority_final"]])
            kept += 1
    print(f"police calls: kept {kept} of {len(rows)}")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    stations()
    calls()
