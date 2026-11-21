"""Download SF Fire Department calls for service (DataSF nuek-vuh3) -> data/calls.csv.gz

One row per incident: the first unit dispatched. Run once; the output is committed so deploys don't need it.
"""
import csv
import gzip
import io
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

URL = "https://data.sf.gov/resource/nuek-vuh3.csv"
START, END = "2026-05-01", "2026-11-01"
FIELDS = ["incident_number", "received_dttm", "response_dttm", "on_scene_dttm", "available_dttm", "unit_id",
          "unit_type", "station_area", "case_location", "neighborhoods_analysis_boundaries", "call_type",
          "call_type_group", "final_priority"]
OUT = Path(__file__).parent / "data/calls.csv.gz"


def ts(s):
    return datetime.fromisoformat(s).timestamp() if s else None


def main():
    rows, offset, page = [], 0, 50000
    while True:
        q = urllib.parse.urlencode({
            "$select": ",".join(FIELDS),
            "$where": f"received_dttm >= '{START}' AND received_dttm < '{END}' AND unit_sequence_in_call_dispatch='1' "
                      "AND on_scene_dttm IS NOT NULL AND case_location IS NOT NULL",
            "$order": "received_dttm", "$limit": page, "$offset": offset})
        text = urllib.request.urlopen(f"{URL}?{q}", timeout=300).read().decode()
        batch = list(csv.DictReader(io.StringIO(text)))
        rows += batch
        print(len(rows), "rows")
        if len(batch) < page:
            break
        offset += page

    OUT.parent.mkdir(exist_ok=True)
    kept = 0
    with gzip.open(OUT, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "dispatch_s", "travel_s", "busy_s", "lat", "lng", "unit", "unit_type", "station",
                    "hood", "call_type", "group", "priority"])
        for r in rows:
            rec, resp, on, avail = (ts(r[k]) for k in ["received_dttm", "response_dttm", "on_scene_dttm", "available_dttm"])
            lng, lat = map(float, r["case_location"].removeprefix("POINT (").rstrip(")").split())
            if not (resp and on and avail) or not (37.6 < lat < 37.85 and -122.55 < lng < -122.3):
                continue
            travel, busy = on - resp, avail - resp
            if not (0 < travel < 3600 and 0 < busy < 6 * 3600 and 0 <= resp - rec < 3600):
                continue
            w.writerow([int(rec), int(resp - rec), int(travel), int(busy), f"{lat:.5f}", f"{lng:.5f}", r["unit_id"],
                        r["unit_type"], r["station_area"], r["neighborhoods_analysis_boundaries"], r["call_type"],
                        r["call_type_group"], r["final_priority"]])
            kept += 1
    print(f"kept {kept} of {len(rows)} -> {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
