"""SF emergency hospitals (the ones 911 ambulances go to) -> data/sf/hospitals.json

- Which hospitals: DataSF Hospital Diversions (t4sf-777q) lists the 10 SF emergency departments, and how long
  each spent on ambulance diversion ("send ambulances elsewhere") in the last 12 months.
- Where they are: California HHS licensed facility locations (health_facility_locations.csv).
  The VA isn't state-licensed, so its coordinates are filled in by hand from its address (approximate).
"""
import csv
import io
import json
import subprocess
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/sf/hospitals.json"
CA = "https://data.chhs.ca.gov/dataset/3b5b80e8-6b8d-4715-b3c0-2699af6e72e5/resource/f0ae5731-fef8-417f-839d-54a0ed3a126e/download/health_facility_locations.csv"
SINCE = "2025-11-01"

# DataSF name -> (short label, CA facility name)
MATCH = {
    "Zuckerberg San Francisco General": ("SF General (trauma center)", "PRISCILLA CHAN AND MARK ZUCKERBERG SAN FRANCISCO GENERAL HOSPITAL AND TRAUMA CENTER"),
    "California Pacific Medical Center - Van Ness": ("CPMC Van Ness", "CALIFORNIA PACIFIC MEDICAL CENTER - VAN NESS CAMPUS"),
    "California Pacific Medical Center - Davies": ("CPMC Davies", "CALIFORNIA PACIFIC MEDICAL CENTER - DAVIES CAMPUS"),
    "California Pacific Medical Center - Mission Bernal": ("CPMC Mission Bernal", "CALIFORNIA PACIFIC MEDICAL CENTER - MISSION BERNAL CAMPUS AND ORTHOPEDIC INSTITUTE"),
    "Kaiser Permanente, San Francisco": ("Kaiser SF", "KAISER FOUNDATION HOSPITAL - SAN FRANCISCO"),
    "University of California, San Francisco Medical Center - Parnassus": ("UCSF Parnassus", "UCSF MEDICAL CENTER"),
    "University of California, San Francisco Medical Center - Hyde": ("UCSF Hyde", "UCSF HEALTH HYDE HOSPITAL"),
    "University of California, San Francisco Medical Center - Stanyan": ("UCSF Stanyan", "UCSF HEALTH STANYAN HOSPITAL"),
    "Chinese Hospital": ("Chinese Hospital", "CHINESE HOSPITAL"),
    "Veterans Administration Medical Center": ("SF VA Medical Center", None),
}
VA = {"address": "4150 CLEMENT ST", "lat": 37.7823, "lng": -122.5047}  # approximate, from the address


def curl(url):
    return subprocess.run(["curl", "-sfL", "--max-time", "300", url], capture_output=True, check=True).stdout.decode("utf-8-sig")


def main():
    ca = {r["FACNAME"]: r for r in csv.DictReader(io.StringIO(curl(CA)))
          if r["COUNTY_NAME"].upper() == "SAN FRANCISCO"}
    q = urllib.parse.urlencode({"$select": "hospital_full, sum(duration_in_minutes) as minutes, count(*) as n",
                                "$where": f"started >= '{SINCE}'", "$group": "hospital_full"})
    div = {r["hospital_full"]: r for r in json.loads(curl(f"https://data.sfgov.org/resource/t4sf-777q.json?{q}"))}
    out = []
    for name, (short, ca_name) in MATCH.items():
        loc = VA if ca_name is None else {"address": ca[ca_name]["ADDRESS"], "lat": float(ca[ca_name]["LATITUDE"]),
                                          "lng": float(ca[ca_name]["LONGITUDE"])}
        d = div.get(name, {})
        out.append({"name": name, "short": short, **loc, "approx_location": ca_name is None,
                    "diversion_hours_12mo": round(float(d.get("minutes", 0)) / 60, 1),
                    "diversions_12mo": int(d.get("n", 0))})
    OUT.write_text(json.dumps(out, indent=1))
    print(f"{len(out)} hospitals -> {OUT}")
    for h in sorted(out, key=lambda h: -h["diversion_hours_12mo"]):
        print(f"  {h['short']:28} {h['diversion_hours_12mo']:7.1f} h on diversion ({h['diversions_12mo']} times)")


if __name__ == "__main__":
    main()
