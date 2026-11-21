# LabTwin

Acalanes Data Fest 2026 project. A "digital twin" that replays a lab protocol step by step and flags the steps most likely to ruin the experiment, before anyone wastes a day at the bench.

## Question

Can data from real published protocols catch typos and unit mistakes in a new protocol (for example 10× too much reagent, mL instead of µL, or a wrong temperature)?

## Data

**BioProt**: 100 public protocols from protocols.io, 1,288 steps, 1,925 numeric quantities (volumes, temperatures, times, speeds, concentrations, masses). Source: the BioPlanner paper, arXiv [2310.10632](https://arxiv.org/abs/2310.10632), repo [bioplanner/bioplanner](https://github.com/bioplanner/bioplanner).

Limits: 100 protocols is small, they are mostly molecular biology, and nobody has labeled which protocols really failed. That is why validation uses errors we insert on purpose (see below).

## Method

1. **Parse.** Split each protocol into steps and pull out every quantity and its unit, converted to one base unit (µL, °C, s, ...).
2. **Learn what's normal.** For each quantity, find the 15 most similar sentences in *other* protocols (TF-IDF nearest neighbors) and take the median and spread (MAD) of the same kind of quantity there.
3. **Score.** Compute a robust z-score (log scale, except temperature is linear). If z > 4, flag the step and explain why ("12,000,000 × g is unusual vs similar steps, typical ≈ 12,000 × g").
4. **Recipe check.** If a step says "20 µL reaction" and lists the components, check that they add up.

Baselines for comparison: one range per unit kind (`kind`), and one range per unit kind and step action, such as centrifuge or incubate (`action`).

## Validation

5-fold cross-validation by protocol, so the model never learns from the protocol it is checking. Errors are inserted into held-out steps, and each one counts as caught if the edited quantity is flagged. A false alarm is a real, untouched quantity that gets flagged.

| Model (threshold) | False alarms | 10× typo | 0.1× typo | Wrong unit | Temp +40 °C |
|---|---|---|---|---|---|
| `kind` (2.5) | 5.1% | 19.0% | 10.5% | 29.7% | 19.8% |
| `action` (3) | 6.6% | 20.9% | 14.4% | 30.4% | 49.5% |
| **`knn` (4), ours** | 5.5% | **23.8%** | **17.8%** | **30.6%** | 37.9% |

Test cases: 1,921 (10×), 1,925 (0.1×), 1,063 (unit), 182 (temperature).

Takeaways:
- Context helps: with about the same false-alarm rate, the nearest-neighbor model catches more volume and amount typos than either baseline.
- The `action` baseline is better on temperature errors.
- Most typos still get through. A 10× change often lands inside the real range, because protocols differ so much.

**Real finding:** the recipe audit found a published eDNA PCR protocol ([protocols.io/view/10051](https://www.protocols.io/view/10051), step 2) where the components add to 20.8 µL for a stated 20 µL reaction. Only 2 steps in the dataset state a reaction volume this way, so the recipe check covers very little of the data.

## Run it

```bash
git clone --depth 1 https://github.com/bioplanner/bioplanner.git data/raw/bioplanner
python3 labtwin.py     # self-check
python3 evaluate.py    # prints the table above, writes results.json
```

Needs Python 3.10+. No extra packages.

## Files

- `labtwin.py`: parser, twin model, recipe check, self-check
- `evaluate.py`: cross-validation with inserted errors, recipe audit
- `results.json`: latest evaluation output

## MVP status / next

- [x] Data, model, baselines, validation, recipe audit
- [ ] Demo app: paste a protocol, see a step-by-step replay with flagged steps and reasons
- [ ] "Fix it": an LLM suggests a corrected step for each flag (optional; needs an API key)
- [ ] Slides: question, data, build, finding, limits (5 minutes)
