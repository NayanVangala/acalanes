"""Validation: inject known errors into held-out protocols, measure catch rate vs false alarms.

5-fold cross-validation by protocol: the twin never learns from the protocol it is checking.
Error types mimic real slips: 10x / 0.1x typo, wrong unit (µL vs mL, min vs h), temperature off by 40 °C.
A slip counts as caught if the edited quantity scores above the threshold.
False alarm rate = share of untouched, real quantities that score above the threshold.
Also audits every recipe in the dataset for volumes that don't add up.
"""
import json
import random
import re

from labtwin import REACTION, Twin, load, recipe_check

ERRORS = ["x10", "x0.1", "unit", "temp+40"]
THRESHOLDS = [2.5, 3, 3.5, 4, 5, 6]
MODES = ["kind", "action", "knn"]
SWAP = {"µl": "ml", "μl": "ml", "ul": "ml", "µL": "mL", "μL": "mL", "uL": "mL", "min": "h", "minutes": "hours",
        "s": "min", "seconds": "minutes", "mM": "M", "µM": "mM", "μM": "mM", "ng": "µg", "µg": "mg", "μg": "mg"}


def mutate(step, q, err):
    """Return (edited step, start of edited quantity) or None if the error doesn't apply."""
    m = re.match(r"([\d,]+(?:\.\d+)?)(\s*)(.*)", q[4])
    num, sep, unit = float(m.group(1).replace(",", "")), m.group(2), m.group(3)
    if err == "x10":
        new = f"{num * 10:g}{sep}{unit}"
    elif err == "x0.1":
        new = f"{num / 10:g}{sep}{unit}"
    elif err == "unit" and unit in SWAP:
        new = f"{m.group(1)}{sep}{SWAP[unit]}"
    elif err == "temp+40" and q[1] == "temp":
        new = f"{num + 40:g}{sep}{unit}"
    else:
        return None
    return step[:q[2]] + new + step[q[3]:], q[2]


def evaluate(protocols, folds=5, seed=0):
    ids = [p["id"] for p in protocols]
    random.Random(seed).shuffle(ids)
    fold_of = {pid: i % folds for i, pid in enumerate(ids)}
    clean = {m: [] for m in MODES}                         # z of every real quantity
    injected = {m: {e: [] for e in ERRORS} for m in MODES}  # z of every edited quantity
    for f in range(folds):
        train = [p for p in protocols if fold_of[p["id"]] != f]
        test = [p for p in protocols if fold_of[p["id"]] == f]
        for mode in MODES:
            twin = Twin(train, mode=mode)
            for p in test:
                for s in p["steps"]:
                    zs = twin.zscores(s)
                    clean[mode] += [z for _, z, _, _ in zs]
                    for q, *_ in zs:
                        for e in ERRORS:
                            mut = mutate(s, q, e)
                            if mut:
                                bad, start = mut
                                hit = [z for q2, z, _, _ in twin.zscores(bad) if q2[2] == start]
                                if hit:
                                    injected[mode][e].append(hit[0])
    table = {}
    for mode in MODES:
        for t in THRESHOLDS:
            table[f"{mode}@{t}"] = {"false_alarm": sum(z > t for z in clean[mode]) / len(clean[mode]),
                                    **{e: sum(z > t for z in injected[mode][e]) / len(injected[mode][e]) for e in ERRORS}}
    counts = {"real_quantities": len(clean["knn"]), **{e: len(injected["knn"][e]) for e in ERRORS}}
    return table, counts


def recipe_audit(protocols):
    n, hits = 0, []
    for p in protocols:
        for i, s in enumerate(p["steps"], 1):
            n += bool(REACTION.search(s))
            hits += [{"protocol": p["title"], "url": p["url"], "step": i, "why": fl["why"]} for fl in recipe_check(s)]
    return n, hits


if __name__ == "__main__":
    ps = load()
    table, counts = evaluate(ps)
    print("Test cases:", counts)
    print(f"{'model@threshold':18}{'false alarm':>12}" + "".join(f"{e:>9}" for e in ERRORS))
    for key, r in table.items():
        print(f"{key:18}{r['false_alarm']:>12.1%}" + "".join(f"{r[e]:>9.1%}" for e in ERRORS))
    n, hits = recipe_audit(ps)
    print(f"\nRecipe audit: {n} steps state a reaction volume, {len(hits)} don't add up:")
    for h in hits:
        print(f"  - {h['protocol'][:60]} (step {h['step']}): {h['why']}  {h['url']}")
    json.dump({"counts": counts, "table": table, "recipe_steps": n, "recipe_mismatches": hits},
              open("results.json", "w"), indent=1)
