"""LabTwin: replay a lab protocol step by step, flag quantities that look wrong.

Data: BioProt (100 public protocols.io protocols, BioPlanner paper, arXiv 2310.10632).
Model: every quantity (volume, temp, time, speed, ...) is compared with the same kind of
quantity in the most similar sentences from *other* real protocols (TF-IDF nearest neighbors).
Score = robust z-score (median / MAD, log scale except temperature).
Baselines for ablation: one range per unit kind ("kind"), or per kind + step action ("action").
Plus a recipe check: do component volumes add up to the stated reaction volume?
"""
import glob
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(__file__).parent / "data/raw/bioplanner/bioprot"

UNITS = {  # unit -> (kind, factor to base unit)
    "nl": ("volume", 1e-3), "µl": ("volume", 1), "ul": ("volume", 1), "ml": ("volume", 1e3), "l": ("volume", 1e6),
    "°c": ("temp", 1), "ºc": ("temp", 1),
    "s": ("time", 1), "sec": ("time", 1), "secs": ("time", 1), "seconds": ("time", 1), "second": ("time", 1),
    "min": ("time", 60), "mins": ("time", 60), "minutes": ("time", 60), "minute": ("time", 60),
    "h": ("time", 3600), "hr": ("time", 3600), "hrs": ("time", 3600), "hours": ("time", 3600), "hour": ("time", 3600),
    "nm": ("conc", 1e-3), "µm": ("conc", 1), "um": ("conc", 1), "mm": ("conc", 1e3), "m": ("conc", 1e6),
    "ng": ("mass", 1e-3), "µg": ("mass", 1), "ug": ("mass", 1), "mg": ("mass", 1e3),
    "rpm": ("rpm", 1), "xg": ("rcf", 1), "×g": ("rcf", 1),
}
BASE = {"volume": "µL", "temp": "°C", "time": "s", "conc": "µM", "mass": "µg", "rpm": "rpm", "rcf": "×g"}
NUM = r"(\d+(?:,\d{3})*(?:\.\d+)?)"
QTY = re.compile(NUM + r"\s*(x\s*g|×\s*g|°\s*c|º\s*c|[a-zµμ]+)\b", re.I)

ACTIONS = [  # first match wins
    ("centrifuge", r"centrifug|spin|pellet"),
    ("pcr", r"pcr|denatur|anneal|extension|cycles?\b"),
    ("incubate", r"incubat|heat|warm|thaw|digest"),
    ("store", r"store|freez|keep|hold|-20|-80"),
    ("wash", r"wash|rinse"),
    ("elute", r"elut|resuspend|dissolve"),
    ("add", r"add|pipett|transfer|mix|combin|prepar"),
]
STOP = set("the a an of to and in for with at was were is are be by on or from then this that each into using use".split())
WINDOW = 60  # chars of context on each side of a quantity
LOG_FLOOR, TEMP_FLOOR = 0.2, 5.0  # min spread (log10 units, °C) so tight groups don't flag everything; tuned in sweep.py


def norm_unit(u):
    return re.sub(r"\s+", "", u.lower()).replace("μ", "µ")


def quantities(text):
    """All (value_in_base_unit, kind, start, end, raw) found in text."""
    out = []
    for m in QTY.finditer(text):
        u = norm_unit(m.group(2))
        if u not in UNITS:
            continue
        kind, f = UNITS[u]
        v = float(m.group(1).replace(",", ""))
        if kind == "temp" and text[max(0, m.start() - 1):m.start()] == "-":
            v = -v
        out.append((v * f, kind, m.start(), m.end(), m.group(0)))
    return out


def action(text):
    t = text.lower()
    return next((a for a, pat in ACTIONS if re.search(pat, t)), "other")


def split_steps(raw):
    """BioProt steps are '1. ...' numbered lines; section headers are dropped."""
    parts = re.split(r"(?m)^\s*(\d+)\.\s", raw)
    return [p.strip() for p in parts[2::2]] or [raw.strip()]


def load():
    out = []
    for f in sorted(glob.glob(str(DATA / "*.json"))):
        d = json.load(open(f))
        out.append({"id": d["id"], "title": d["title"], "url": d["protocols.io url"], "steps": split_steps(d["steps"])})
    return out


def scale(v, kind):
    # temps are linear (can be negative); everything else spans orders of magnitude
    return v if kind == "temp" else math.log10(max(v, 1e-9))


def unscale(x, kind):
    return x if kind == "temp" else 10 ** x


def tokens(text):
    return [w for w in re.findall(r"[a-z]{2,}", text.lower()) if w not in STOP]


def context(text, start, end):
    return tokens(text[max(0, start - WINDOW):end + WINDOW])


def robust(xs, kind):
    xs = sorted(xs)
    med = xs[len(xs) // 2]
    mad = sorted(abs(x - med) for x in xs)[len(xs) // 2]
    floor = TEMP_FLOOR if kind == "temp" else LOG_FLOOR
    return med, max(1.4826 * mad, floor)


class Twin:
    """mode: 'knn' (context neighbors, main model), 'action' or 'kind' (baselines)."""

    def __init__(self, protocols, mode="knn", k=15, min_group=8):
        self.mode, self.k = mode, k
        groups = defaultdict(list)
        self.docs = defaultdict(list)  # kind -> [(tfidf vector, scaled value)]
        raw_docs = []
        for p in protocols:
            for s in p["steps"]:
                a = action(s)
                for v, kd, st, en, _ in quantities(s):
                    x = scale(v, kd)
                    groups[(a, kd)].append(x)
                    groups[("*", kd)].append(x)
                    raw_docs.append((kd, context(s, st, en), x))
        self.stats = {key: robust(xs, key[1]) for key, xs in groups.items() if len(xs) >= min_group}
        df = Counter(w for _, toks, _ in raw_docs for w in set(toks))
        self.idf = {w: math.log(len(raw_docs) / c) for w, c in df.items()}
        for kd, toks, x in raw_docs:
            self.docs[kd].append((self.vec(toks), x))

    def vec(self, toks):
        tf = Counter(toks)
        v = {w: c * self.idf.get(w, math.log(len(self.idf) + 1)) for w, c in tf.items()}
        n = math.sqrt(sum(x * x for x in v.values())) or 1
        return {w: x / n for w, x in v.items()}

    def reference(self, text, q):
        """(median, spread, label) the quantity is compared against."""
        v, kd, st, en, _ = q
        if self.mode == "knn" and self.docs.get(kd):
            qv = self.vec(context(text, st, en))
            sims = sorted(((sum(w * d.get(t, 0) for t, w in qv.items()), x) for d, x in self.docs[kd]), reverse=True)
            near = [x for sim, x in sims[:self.k] if sim > 0]
            if len(near) >= 5:
                return (*robust(near, kd), "similar steps in other protocols")
        a = action(text) if self.mode != "kind" else "*"
        st_ = self.stats.get((a, kd)) or self.stats.get(("*", kd))
        return (*st_, f"'{a}' steps") if st_ else None

    def zscores(self, text):
        out = []
        for q in quantities(text):
            ref = self.reference(text, q)
            if ref:
                med, spread, label = ref
                out.append((q, abs(scale(q[0], q[1]) - med) / spread, unscale(med, q[1]), label))
        return out

    def run(self, steps, threshold=4.0):
        """Replay protocol. Returns per-step trace with flags."""
        trace = []
        for i, s in enumerate(steps, 1):
            flags = [{"type": "unusual " + q[1], "text": q[4], "z": round(z, 1), "pos": q[2:4],
                      "why": f"{q[4]} is unusual vs {label} (typical ≈ {med:.3g} {BASE[q[1]]})"}
                     for q, z, med, label in self.zscores(s) if z > threshold]
            trace.append({"step": i, "action": action(s), "text": s, "flags": flags + recipe_check(s)})
        return trace


REACTION = re.compile(NUM + r"\s*(µl|μl|ul)\s+(?:total\s+)?(?:pcr\s+)?(?:reactions?|rxn|total volume|final volume)", re.I)


def recipe_check(step, tol=0.02):
    """If a step states an N µL reaction and lists component volumes, do they sum to N?"""
    m = REACTION.search(step)
    if not m:
        return []
    total = float(m.group(1))
    comps = 0.0
    n = 0
    for line in step[m.end():].splitlines():
        vols = [q for q in quantities(line) if q[1] == "volume"]
        if not vols:
            continue
        mult = 2 if re.search(r"\beach\b.*\band\b", line, re.I) else 1
        comps += vols[0][0] * mult
        n += 1
    if n < 3 or abs(comps - total) / total <= tol:
        return []
    return [{"type": "recipe", "text": m.group(0), "z": None, "pos": m.span(),
             "why": f"components add to {comps:g} µL but reaction is {total:g} µL"}]


if __name__ == "__main__":
    # self-check
    assert quantities("spin at 12,000 x g for 5 min")[0][:2] == (12000.0, "rcf")
    assert quantities("add 5 μl")[0][:2] == (5.0, "volume")
    assert quantities("store at -20 °C")[0][:2] == (-20.0, "temp")
    bad = "20 μl reactions using:\n2 μl DNA\n10 μl mix\n0.8 μl of each forward and reverse primer\n7.2 μl water"
    assert recipe_check(bad) and "20.8" in recipe_check(bad)[0]["why"]
    assert not recipe_check(bad.replace("7.2", "6.4"))
    ps = load()
    twin = Twin(ps[1:])
    z_ok = twin.zscores("Centrifuge at 12,000 x g for 1 min")[0][1]
    z_bad = twin.zscores("Centrifuge at 12,000,000 x g for 1 min")[0][1]
    assert z_bad > z_ok, (z_ok, z_bad)
    print(len(ps), "protocols,", sum(len(p["steps"]) for p in ps), "steps, self-check ok")
