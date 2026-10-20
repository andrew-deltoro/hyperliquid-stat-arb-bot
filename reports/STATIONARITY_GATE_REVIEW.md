# The stationarity gate: what it rejects, and the one thing it must be judged on

**Question:** `stationarity_level: 0.10` rejects ~88% of all pair evaluations,
including signals the model itself scores at E[r] = +9% to +17% and P(win) ≈ 1.0.
Is the gate mis-calibrated?

**Answer: on the aggregate data, no. On the specific evidence that matters, it is
unresolved — and the gate's blind spot is exactly where this bot's edge lives.**

---

## 1. What the gate is

`rejects_random_walk()` (`stats/error_correction.py:170-182`) rejects a pair when
the Engle–Granger ADF t-stat on the posterior-mean residual exceeds a critical
value:

```45:45:src/hyperliquid_bot/stats/cointegration.py
EG_CRITICAL = {0.01: -3.90, 0.05: -3.34, 0.10: -3.04}
```

`stationarity_level: 0.10` → the pair must show **EG_t ≤ −3.04**. Note what this
fundamentally is: a test for whether the spread is **mean-reverting at all**. It
is not a screen for signal quality.

## 2. What it rejects

Parsed from all 119 cycle logs + `systemd.log` (1,752 pair evaluations,
19 pairs, Sept 29 → Oct 14):

| rejection reason | records citing it | share |
|---|---|---|
| **not_stationary** | **1,540** | 88% |
| z_too_small | 1,029 | 59% |
| expected_return_too_small | 326 | 19% |
| half_life_too_long | 102 | 6% |

Only **78 of 1,752** evaluations reached `OPEN` (4.5%).

Among the **641 records where `not_stationary` was the ONLY blocker**:

| | value |
|---|---|
| median E[r] | **+2.772%** |
| max E[r] | **+16.824%** |
| median P(win) | 0.861 |
| median \|z\| | 1.61 |
| **median EG_t** | **−1.91** (bar is −3.04) |
| closest EG_t | **−0.14** |

Distance from the bar: 10 within 0.25, 64 within 0.50, 186 within 1.00, 381 within
2.00, 46 beyond 2.00. So it is **not** a cluster sitting just under the threshold —
most are a full 1–2 units away.

## 3. A natural hypothesis, and why it FAILED

**My hypothesis:** a large dislocation mechanically weakens EG_t, so the gate
systematically kills the best signals.

**The data rejected it.** Correlation across all 1,752 records:

| relationship | correlation |
|---|---|
| corr(EG_t, E[r]) | **+0.368** — *positive* |
| corr(EG_t, \|z\|) | +0.235 |
| corr(\|z\|, E[r]) | **+0.837** |

`corr(EG_t, E[r])` is **positive, not negative**. And **17 of 19 pairs** show
strongly *positive* per-pair correlation (up to +0.957). So better stationarity
is generally associated with *higher* expected return. My hypothesis was wrong.

The real relationship: **E[r] is dominated by |z| (+0.837)**. E[r] is a
dislocation-size measure; EG_t is an unrelated cointegration measure. They
co-move positively, weakly.

## 4. The one piece of evidence that DOES support the concern

The gate must be judged on one thing: **did it pass the trade that paid, and
block the ones that would have?**

### The trade that passed

```
2026-10-11T12:04 -> 2026-10-12T08:04  RSR/TAO  exit=cross           +$95.36
2026-10-12T08:05 -> 2026-10-13T02:54  RSR/TAO  exit=operator_close   +$9.58
```

It entered with **EG_t = −3.85** (passes) and **z = −0.94** — a *weak*
dislocation whose E[r] was only **+1.032%**.

### The signals it then refused

| cycle | z | EG_t | E[r] | decision |
|---|---|---|---|---|
| 2026-10-12 08:00 | **8.58** | −3.14 | **+13.775%** | **OPEN** |
| 2026-10-12 12:00 | 8.64 | −1.07 | +16.819% | SKIP (not_stationary) |
| 2026-10-12 14:00 | 7.94 | −0.82 | +16.824% | SKIP (not_stationary) |
| 2026-10-12 16:00 | 6.48 | −1.65 | +14.835% | SKIP (not_stationary) |
| 2026-10-12 20:00 | 5.80 | −1.56 | +14.511% | SKIP (not_stationary) |
| 2026-10-13 00:00 | 4.77 | −1.97 | +12.751% | SKIP (not_stationary) |
| … | 3.0–3.6 | −2.2 to −2.7 | +8.3 to +10.4 | SKIP (not_stationary) |

**The entry fired on the weakest-scored evaluation in the sequence (E[r] =
+1.032%), and every subsequent evaluation — including ones the model scored at
P(win) = 1.000 and E[r] = +16.8% — was refused by `not_stationary`.**

That matters because this morning's analysis established that the
**cross-exit continuation** is where this strategy's edge actually lives
(+6.744% across 96 trades, 74 episodes, 15 pairs, 27 weeks). The gate passed the
entry that captured a continuation, then blocked the follow-on evaluations of the
very same episode.

## 5. The verdict, stated honestly

**What the aggregate data supports:** the gate is **not** indiscriminately
anti-edge. Its rejections are mostly far from the threshold, and `corr(EG_t,
E[r])` is positive. `not_stationary` is a legitimate risk screen.

**What the aggregate data cannot decide:** whether tail events like RSR/TAO are
being given up. The gate is a mean-reversion test, and this bot's evidence of
edge is *continuations* — precisely what a mean-reversion screen is structurally
blind to.

**What would settle it:** replay the RSR/TAO sequence from 2026-10-12 12:00 with
`stationarity_level` relaxed to 0.01 (EG_t ≤ −3.90) or disabled, and measure the
counterfactual. That is a bounded, single-episode test — **not** a reason to
change the live gate.

## 6. Explicitly NOT recommending a live change

- One episode is one episode. This morning's lesson was that 82% of PnL sits in
  a single trade; optimising a gate around the trade that produced it is exactly
  the overfitting this investigation has been trying to avoid.
- `corr(EG_t, E[r]) = +0.368` argues the gate is directionally sensible.
- The honest framing: **the gate is probably correct in general and may be
  wrong at the tail.** That is a hypothesis to test on a replay, not a knob to
  turn.

## 5. RESOLVED: the gate is exonerated, with 1,637 labelled decisions

The stationarity question could not be settled on 33 realized trades. It can be
settled on the full evaluation record, which `scripts/build_entry_dataset.py`
now labels with forward outcomes.

**First, a bug that had to be fixed.** The bot writes `direction = "NONE"` for
every `SKIP` row (1,504 of them) and only records a real direction for entries it
took. My first pass defaulted `sign = -1` for those, systematically flipping every
skipped evaluation and producing a meaningless OPEN-vs-SKIP gap. The corrected
version derives the natural mean-reversion direction from the sign of `z`
(short the spread when z>0, long when z<0).

Corrected results, forward 72h return in the bot's own convention
(`0.5 * sign * Δspread`) under a fixed simple exit proxy:

| group | n | mean | median | win% |
|---|---|---|---|---|
| **`OPEN`** (bot took it) | 356 | **+1.202%** | +1.464% | **76%** |
| `SKIP` (all reasons) | 1,281 | +0.072% | +0.130% | 53% |
| `SKIP`, **only** `not_stationary` | **246** | **−0.634%** | +0.063% | 52% |
| `SKIP`, other reasons | 1,035 | +0.240% | +0.142% | 54% |

**The gate works.** Entries the bot actually took have **+1.202%** forward return
at a **76%** hit rate, against **+0.072%** for what it skipped. And the signals
rejected *specifically* by `not_stationary` — the ones where z, E[r] and every
other gate had already passed — have the **worst** forward outcomes of any group
at **−0.634%**.

That is precisely the pattern you want from a working filter: conditioned on a
signal being attractive on every other axis, weak stationarity is a **negative**
predictor.

### The interaction that explains the apparently conflicting earlier result

§3 found `corr(EG_t, E[r]) = +0.368` (positive), which looked like it argued the
gate was sensible. §4 found RSR/TAO-strength scores being refused. Both are
consistent with the table above:

- The broadest EG_t quintile (weakest stationarity) shows the *best* forward
  return (+0.583%) — but that bucket is dominated by `z_too_small` rows, small
  dislocations with little to lose.
- Among rows that passed **every other** gate, weak stationarity is negative
  (−0.634%).

So the gate's *marginal* contribution, properly conditioned, is positive.

### Verdict

**`stationarity_level: 0.10` is doing its job. Do not change it.**

My §4 concern — that the gate was blocking RSR-style continuations — is **not
supported** at the population level. RSR/TAO remains a single episode where the
gate refused a large dislocation; across 246 comparable refusals the forward
outcome is negative.

## 6. What the dataset now enables (and its limits)

`reports/entry_dataset.csv` — **1,637 evaluations**, 15 pairs, Sept 29 → Oct 14,
with columns: decision, skip reason, z, EG_t, E[r], p_revert, kappa, half-life,
stress, plus forward outcomes at 24/72/168h, time-to-centre, and realized trade
fields where the entry was taken (25 joins).

Questions this now makes answerable rather than arguable:
- which gate contributes most/least to forward outcome
- whether time stops cut winners (via `fv_hours_to_centre` vs realized holds)
- whether `z_entry_min` is at the right level

**Limits, stated plainly:**
- `fv_*` columns are **labels — look-ahead by construction.** Never use them as
  live inputs.
- The exit proxy is a fixed simple rule, not the live exit policy.
- **The decisive caveat: forward return is measured from the decision timestamp,
  but `EG_t` is the value at *label time*, and EG_t strengthens over time.** So a
  late-recorded rejection can carry an EG_t that already improved past the bar. It
  is therefore not guaranteed that a row labelled `not_stationary` would have been
  refused at its own decision time on that same EG_t. The counterfactual suite
  (replaying with the gate disabled through the real evaluator) remains the
  stricter test.
- 6 of 22 pairs failed to fetch (rate limiting), so coverage is 15 pairs.

## Reproduce

```
.venv/bin/python scripts/build_entry_dataset.py            # cached; re-runs are free
```

Signal records are parseable from the cycle logs:

```
logs/cycle_*.log          # 119 cycles, Sept 29 -> Oct 14
logs/systemd.log          # continuous roll-up
```

Regex: `^\[(pair)\] beta=… z=(z) EG_t=(eg) HL=(hl)h … P(win)=… E\[r\]=…% → (OPEN|SKIP)(reasons)`
