"""Ground height grid for San Francisco -> data/sf/elevation.json

Source: AWS Terrain Tiles ("terrarium" PNGs, USGS 3DEP-derived; free, no key).
Each pixel: height_m = R*256 + G + B/256 - 32768 (meters above sea level).
Output: a regular lng/lat grid (~35 m cells) the browser samples to put vehicles, trails and buildings
on the ground of Google's 3D city (which is measured from the WGS84 ellipsoid; the page adds the geoid offset).
"""
import json
import math
import struct
import subprocess
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/sf/elevation.json"
BBOX = [-122.545, 37.69, -122.34, 37.845]  # SF plus a little margin
STEP = [0.0004, 0.00032]                   # ~35 m x ~35 m
Z = 13                                     # ~19 m per pixel at this latitude
URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"


def png_rgb(data):
    """Minimal PNG decoder for 8-bit RGB/RGBA, non-interlaced (what terrarium tiles are)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w = 8, b"", 0
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and ctype in (2, 6) and interlace == 0, (depth, ctype, interlace)
            bpp = 3 if ctype == 2 else 4
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    raw, stride, rows, prev = zlib.decompress(idat), w * bpp, [], bytearray(w * bpp)
    for r in range(h):
        f, line = raw[r * (stride + 1)], bytearray(raw[r * (stride + 1) + 1:(r + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], prev[i - bpp] if i >= bpp else 0
            if f == 1: line[i] = (line[i] + a) & 255
            elif f == 2: line[i] = (line[i] + b) & 255
            elif f == 3: line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append([(line[i * bpp], line[i * bpp + 1], line[i * bpp + 2]) for i in range(w)])
        prev = line
    return rows


def tile_xy(lng, lat):
    n = 2 ** Z
    return (lng + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n


def main():
    x0, y0 = tile_xy(BBOX[0], BBOX[3])
    x1, y1 = tile_xy(BBOX[2], BBOX[1])
    tiles = {}
    for tx in range(int(x0), int(x1) + 1):
        for ty in range(int(y0), int(y1) + 1):
            png = subprocess.run(["curl", "-sfL", "--max-time", "60", URL.format(z=Z, x=tx, y=ty)],
                                 capture_output=True, check=True).stdout
            tiles[(tx, ty)] = png_rgb(png)
    print(f"{len(tiles)} tiles at z{Z}")

    def height(lng, lat):
        fx, fy = tile_xy(lng, lat)
        px, py = (fx % 1) * 256, (fy % 1) * 256
        r, g, b = tiles[(int(fx), int(fy))][min(255, int(py))][min(255, int(px))]
        return r * 256 + g + b / 256 - 32768

    nx = int(round((BBOX[2] - BBOX[0]) / STEP[0])) + 1
    ny = int(round((BBOX[3] - BBOX[1]) / STEP[1])) + 1
    grid = [[max(0, round(height(BBOX[0] + i * STEP[0], BBOX[1] + j * STEP[1]))) for i in range(nx)] for j in range(ny)]
    OUT.write_text(json.dumps({"bbox": BBOX, "step": STEP, "nx": nx, "ny": ny, "units": "m above sea level",
                               "source": "AWS Terrain Tiles (terrarium), USGS 3DEP", "rows": grid}, separators=(",", ":")))
    flat = [v for row in grid for v in row]
    print(f"{nx}x{ny} grid -> {OUT} ({OUT.stat().st_size / 1e3:.0f} KB), {min(flat)}-{max(flat)} m")
    # sanity: Twin Peaks summit ~280 m, Embarcadero ~2 m
    tp, emb = height(-122.4477, 37.7544), height(-122.3933, 37.7955)
    print(f"Twin Peaks {tp:.0f} m, Embarcadero {emb:.0f} m")
    assert 230 < tp < 300 and emb < 15, (tp, emb)


if __name__ == "__main__":
    main()
