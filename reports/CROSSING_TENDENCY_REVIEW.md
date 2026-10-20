# Can We Select Pairs That "Cross Through" the z=0 Reversion?

**Question:** is there a way to test for pairs that have a tendency to cross through
the z=0 reversion?

**Short answer:** the statistic is computable, and the answer is *no* — not in a
way you can select on. A pair's post-convergence overshoot is **not stable out of
sample** (permutation p = 0.16). The overshoot is a property of the entry event
and the spread construction, not a persistent per-pair trait. The correct response
is to run the `cross` exit on **every** pair — which is already the live behaviour —
rather than maintaining a whitelist of "crossing" pairs.

Artifacts:
- `scripts/overshoot_tendency.py` — the test (residual overshoot + persistence)
- `scripts/cross_only_counterfactual.py` — ground-truth check on realized trades

---

## 1. Two traps that make the naive version of this test meaningless

### Trap 1 — the wrong spread
The first version of this test measured `log(A) - log(B)` from the entry-dataset
cache. That is a **random walk** and never reverts, so essentially every episode
hit the horizon cap with `bars->0 = 168`. The bot does **not** trade that. It trades
the **cointegration residual**

```
residual_t = log(A_t) - alpha - beta * log(B_t)
```

with `(alpha, beta)` fitted on the lookback, the residual mean-subtracted, and
`z = residual / sigma` (`src/.../phase3/signals.py`: `current_spread` is
`spread_centered[-1]`, `spread_sd` the centred std). The test was rebuilt around the
residual.

### Trap 2 — rolling z is mechanical, and random walks are recurrent
Two sub-problems:

1. If `z` is measured against a **rolling** mean, then "z crossed zero" is partly
   guaranteed by the window sliding forward. It is not evidence of anything.
2. Even with a fixed centre, a random walk is **recurrent** — it passes any level
   with probability → 1 given enough time. So `P(cross)` is **not** a discriminator.

The fix for both: fit `(alpha, beta, mean, sigma)` on the window **strictly before**
each episode and hold it fixed over the forward path. This mirrors exactly what the
bot freezes at entry (`entry_mean_raw`, `entry_sigma_raw`, `entry_spread`). Then
report, instead of `P(cross)`:

- **overshoot depth** — how far past the centre it goes, in `z` units, inside a
  fixed horizon (`cross_frac`-relative);
- **speed** — bars from entry to first touch of the centre;
- **the economics** — `hold-to-cross` vs `exit-at-centre` P&L on identical episodes;
- and decisively, **persistence**.

---

## 2. What the data says (19 live pairs, 1h bars, 30-day fit window)

Entry `|z| >= 2.0`, horizon 168 bars (7d), `cross_frac = 0.75`.

```
  pair                      n  P(cross)  ovshoot/|z|  bars->0      retC      retX      X-C  X>btr%
  AAVE/COMP                11       18%        -0.35      168     0.44%     1.13%    0.69%       27%
  AIXBT/DOGE               13       38%         0.57       32     2.56%     3.37%    0.80%       69%
  AIXBT/LAYER              16        6%         0.06      120     3.40%     2.55%   -0.85%        6%
  ETC/NEO                  13       23%        -0.12      103     0.69%     1.12%    0.43%       31%
  ...
  ORDI/SOL                 11       27%        -0.13      168     5.05%     7.67%    2.62%       27%
  RSR/TAO                  12        8%        -0.29      168     0.22%    -0.52%   -0.74%        8%
  ...
  ALL PAIRS               247       17%        -0.14      160     0.89%     1.03%    0.14%       27%
```

Only ~17% of episodes even reach the `0.75x` cross level within the week. The
median episode **never fully reverts** (bars->0 = 160 of 168). The centre exit wins
on 73% of episodes; the mean `(cross - centre)` is +0.14% with a heavy right tail.

### Null test
Shuffling the increments of leg B (destroying the cointegrating link) still
produces shallow positive overshoot in some draws, but the real data overshoots
deeper than the shuffled null in **91% of draws** and the real `X - C` exceeds the
null p95 (0.98%). So the overshoot is *real* — it is just rare and unreliable.

### Persistence — the decisive test
Split each pair's episodes in half by time; does the first half's `(cross - centre)`
tendency predict the second half?

```
  corr(1st half, 2nd half) = +0.284
  sign agreement = 71%   (50% = chance)
  permutation test (2000 draws): p = 0.164
```

Even a favourable `cross_frac` can overfit, so consistency across halves — which
spans two regimes with overshoot holding up — is itself the weak point. Let me
state it plainly:

- **71% sign agreement sounds good and is not.** With ~7 episodes per half it is
  well within chance (p = 0.16).
- Of the "stable" pairs, four are stable-**negative** (cross is persistently
  *worse*: `AIXBT/LAYER`, `SNX/SUI`, `SNX/TIA`, `RSR/TAO`) and four **flip sign**
  between halves (`SNX/DYDX`, `LINEA/UMA`, `MEME/TIA`, `xyz:NVDA/xyz:PLTR`).
- `RSR/TAO` — the *exemplar* of the "crossing" thesis — shows cross **worse** than
  the centre exit in this screen (X-C = -0.74%), and its tendency flips to
  strongly negative in the second half. Its +$97 winner was one event, not a trait.

---

## 3. Ground truth: the real trades agree

`scripts/cross_only_counterfactual.py` replays every **actual** closed trade under a
cross-only exit (no target/trail/beta_shift), using the production cost model:

```
  Actual total (all 29):            $ +108.87
  Cross-only total (replayable 25): $  +89.77
  Delta:                            $   -2.13
  Cross-only winners: 13/25  (+$135.07)
  Cross-only losers:  12/25  ( -$45.30)
```

The cross-only world is a **coin flip** that nets out roughly flat — *except* for
`RSR/TAO` (+$97). Strip the RSR event and the cross-only exit is clearly worse.
This is exactly what "the overshoot is an event, not a per-pair trait" predicts.

Note this does **not** argue for removing the cross exit: the cross exit is what
*captured* the RSR continuation, and `evaluate_open` also carries the safety legs
(`divergence_stop`, `beta_shift`) that the cross-only world lacks. Removing `cross`
would have left that money on the table — the same point you made about the RSR
short. The finding is narrower: **you cannot predict which pair will cross next.**

---

## 4. Conclusion and recommended action

| Question | Answer |
|---|---|
| Can we measure cross-through tendency? | Yes — residual overshoot + exit economics, look-ahead-free |
| Is overshoot real (vs a random-walk null)? | Yes (deeper than shuffled null in 91% of draws) |
| Is it *stable per pair*? | **No** — persistence p = 0.16, 4 stable-negative, 4 flips |
| Should we whitelist "crossing" pairs? | **No** — the trait is not selectable |

**Recommendation: keep the `cross` exit universal** (current live behaviour) and do
not add per-pair exit whitelists. Structurally this is the right design:

- The cross exit is **distributional, not predictive** — it costs a little when the
  spread stalls at the centre and pays a lot when it overshoots. That is a cheap
  convexity, not a directional bet, and it should be on everywhere (just as the
  safety legs are on everywhere).
- Selecting pairs on overshoot history would be fitting one or two events per pair
  (the RSR pattern), which is precisely the over-sophistication failure mode we set
  out to avoid.

**If you want to pursue the crossing edge further**, the productive lever is not
*pair selection* but *horizon/holding policy* — e.g. a `cross` variant that keeps a
runner after the cross level is hit, since the payload is a handful of large
continuations. That is a single global parameter, not 19 separate decisions, so it
stays within the statistical budget.

---

## Limits

- 1h bars; the live bot evaluates at the **4h** cycle grid, so a cross that arrives
  between evaluations is missed here.
- Static OLS `beta`; the live bot uses a hierarchical Bayesian ECM with a possible
  Kalman terminal overlay. Slopes inside a 30d window are close, but not identical.
- One regime, 19 pairs, ~13 episodes each — enough to *reject* persistence, not
  enough to *confirm* it for any single pair.
- Horizon-capped: a cross arriving after 168 bars is scored as a miss.
- Funding excluded from the counterfactual (not in the tape) — slightly conservative.

---

## 5. Does a "runner" prototype add edge or profit? (No.)

`scripts/runner_counterfactual.py` tests the natural follow-up: once the cross level
is hit, **keep holding** and trail the extreme by `runner_frac` of the distance
travelled beyond the cross level. Replayed on the same 29 real trades, same 4h
grid, same production cost model.

```
  scheme                     n      total      mean    median  winners       max       min
  cross (rf=0)              25      89.77      3.59      0.39    13/25      96.95    -11.25
  trail rf=0.25             25      57.70      2.31      0.39    13/25      65.96    -11.25
  trail rf=0.5              25      47.57      1.90      0.39    13/25      55.83    -11.25
  trail rf=0.75             24      -9.08     -0.38      0.18    12/24       7.61    -11.25
  trail rf=1.0              24      -9.18     -0.38      0.18    12/24       7.61    -11.25
  trail(inf)=hold           23     -14.70     -0.64     -0.04    11/23       7.61    -11.25
```

**Every runner variant is worse, and monotonically so** — the longer you hold past
the cross, the more you give back. This holds on all three slices:

| slice | cross | rf=0.25 | rf=inf |
|---|---|---|---|
| all replayable (n≈25) | +89.77 | +57.70 | −14.70 |
| RSR removed (n=27) | −7.18 | −8.26 | −14.70 |
| common subset (n=23) | −11.95 | −12.78 | −14.70 |

### Why it fails
The payload — a genuine continuation that keeps running past the cross — is
**one event** (`RSR/TAO`). Everything else either stalls at the centre or turns
right back. On RSR itself, trailing is actively harmful: a 0.25 trail cut
+96.95 → +65.96 because the spread retraced a quarter of its beyond-cross
distance *before* continuing. For a still-live mean-reversion spread, the
expected path after `z=0` is further reversion, so a runner is a bet on the
exception and pays the giveback on the rule.

### Conclusion
**No — the runner does not add edge or profit; it subtracts.** It is the wrong
lever. Moreover the plain cross exit's own positive total is carried entirely by
RSR: without it, cross-only is −7.18. The honest reading is that **this strategy's
edge does not live in exit tuning** — the exits are already in the right
neighbourhood, and both "hold longer" (runner) and "select crossing pairs"
(§2–3) fail for the same reason: continuation is an unpredictable, rare event.

**Recommendation: leave the exit logic as-is.** Do not add a runner, do not add
per-pair exit modes. If more edge is wanted, it should come from *entry quality*
or *sizing*, not from holding longer.

---

## 6. Entry quality (the lever that actually matters)

`scripts/entry_quality_screen.py`. Forward outcome is measured on the **residual**
centred at entry (`centered_t = current_spread + ΔlogA − β·ΔlogB`, using each
signal's recorded `beta`) — not the raw log-ratio — and split in time for OOS.

### Baseline: the existing gate works
```
OPEN (taken)     n=384 rows   mean r168 = +1.051%   hit_centre 74%
SKIP (rejected)  n=1486 rows  mean r168 = +0.101%   hit_centre 59%
  z_too_small    n=707 rows   mean      = +0.630%
  not_stationary n=761 rows   mean      = -0.457%
```
The combined gate is extracting a **+0.95% per-168h spread** between taken and
rejected signals. The `not_stationary` gate is the strongest component
(−0.457% on the rows it rejects) — confirming the earlier stationarity review.

### Feature ICs (Spearman vs forward return, all rows)
```
z            +0.240      (higher |dislocation| -> bigger reversion)
nu_coint     +0.238
edge/p_revert+0.171      (largely a z-rescaled duplicate)
abs_z        +0.122
half_life    +0.077      positive: longer half-life -> MORE forward return
kappa        -0.077
```
`z` is the one genuinely persistent signal (time-stable IC, monotone quintiles:
centre quintile −1.05% → top two quintiles +2.00% / +1.29%). But the bot already
gates on `z`, so this is confirmation, not new information.

### Incremental value among OPEN trades: none survives
Testing whether trimming a feature's tail helps *consistently in both halves*:
the best candidates (`kappa < 70th pct`, `half_life > 20–30th pct`) add only
**~+0.3%** to each half, and only by discarding 20–30% of the sample. Every
"live" feature in this dataset is a near-duplicate of `z` or of the two existing
gates, so no independent refinement emerges.

### The decisive constraint: effective sample size
```
OPEN: 384 signal rows -> 50 distinct episodes  (7.7 overlapping signals each)
SKIP: 1486 signal rows -> 75 distinct episodes
```
The 4h cadence **re-signals the same open position ~8×**, so forward windows
overlap heavily. The true independent N is **~125, not 1,870**. Every IC printed
above is therefore computed on a few dozen independent events. This will later
generalise into a policy candidate: **decorrelate the signal record before any
feature research** (one row per episode, not per bar), otherwise IC tables look
far more precise than they are.

### Conclusion
**No feature-based entry filter should be added.** The recorded features cannot
improve entry selection out of sample, and any attempt would be fitting ~10–50
independent events — the same over-sophistication trap. The gate is already the
edge; the honest next lever is **sizing**, not more gating.

---

## 7. Conviction sizing (the last lever)

`scripts/conviction_scaling.py`. At the **episode level** (one row per open
position, 50 episodes; see §6 on effective N). Weights normalised to **equal
capital**, so any gain must be *risk-adjusted*, not just more exposure. Outcome
uses the bot's own target exit (`r_target`) as well as the 168h mark.

### The naive result looks great
```
scheme        mean      std     Sharpe   vs flat
flat         0.791%    2.562%   0.326    --
abs_z        2.077%    2.462%   0.844    +0.518   (perm p=0.008, bootstrap CI excludes 0)
low_sigma_ou 1.090%    2.418%   0.451    +0.125
```
`abs_z` scaling appears to *both* raise mean and *lower* std — the dream result.
`corr(abs_z, low_sigma_ou) = +0.11`, so they are near-independent, and a combined
weight reaches Sharpe 0.85.

### But it does not survive a deployable rule
A shippable rule is a **bounded** tilt, `w = 1 + λ·(2·rank(|z|) − 1)` capped to
`[1−ρ, 1+ρ]`. Sweeping it:
```
lam=1.0 cap=0.50  ->  Sharpe 0.434   (perm p = 0.072  NOT significant)
lam=1.0 cap=0.25  ->  Sharpe 0.383
```
The bounded tilt captures **only about a third** of the headline lift. The rest
lives in the unbounded weight `w = abs_z`, where:
```
episodes with >2x average weight: 6 / 50
single largest episode's share of the book: 22.1%
```
That is not a sizing policy — it is a bet on ~6 events, one of which is a fifth of
the book.

### Verdict
**Flat sizing stays.** The apparent edge is again a handful of events you cannot
reliably size into, and the only bounded form is not significant and merely re-uses
the `abs_z` the entry gate already screens on. This closes the loop: **exit tuning,
pair selection, feature filters, and conviction sizing all fail for the same
reason** — the real edge is the entry *gate*, and the sample (~50 independent
episodes) is too small for any of these refinements to be validated rather than
overfit.

**Recommended posture: leave the bot as-is and let it accumulate episodes.** The
one legitimate future use of conviction scaling is *forward-tested*: log the
counterfactual weight at entry now (costs nothing), and re-run this test after
another ~150–200 independent episodes. If the bounded-tilt lift persists with a
p < 0.05 on that data, it would be justified then — not on this sample.

---

## 8. Implemented: frozen counterfactual-weight logging (observation only)

Per the §7 recommendation, the bot now **logs** what a fixed bounded |z| tilt would
have sized, at zero behavioural risk. Nothing about execution changes: gates,
sizes and exits are untouched.

- **`src/hyperliquid_bot/phase3/conviction.py`** — the frozen rule
  `w = clip(1 + 1.0·(|z| − 2.0)/3.0, 0.5, 1.5)`. Pure, stateless (the bot runs as
  a 4h oneshot, so no in-memory history is needed), and reproducible from the log
  alone. Constants `CF_LAM/CF_RHO/CF_Z_REF/CF_Z_SCALE` are append-only history.
- **`journal.py`** — new `cf_weight` column on `signals.csv`, written on every
  signal (SKIP and OPEN). Failure to compute degrades to `""`, never raising.
- **`tests/test_conviction.py`** — boundedness, monotonicity, statelessness, NaN
  safety, cap behaviour. Full suite: 134 passed.
- **`scripts/backfill_cf_weight.py`** — deterministic backfill of the 1,908
  existing rows from their recorded `z` (idempotent; took a `.bak-cf` backup).
- **`scripts/cf_weight_monitor.py`** — joins `cf_weight` to forward episode
  outcomes and reports the Sharpe delta with a permutation p-value.

### First read (50 episodes, the existing history)
```
flat:   Sharpe 0.326
scaled: Sharpe 0.391   (delta +0.065, permutation p = 0.019)
```
This is the **deployed** bounded rule, and its benefit is a small fraction of the
unbounded toy result from §7 — exactly as predicted. The monitor holds at "keep
sizing flat" until ≥ ~80 episodes.

### Forward test
Re-run `scripts/cf_weight_monitor.py` after the bot accumulates another **~150–200
independent episodes**. Conviction sizing becomes justified **only if** the Sharpe
delta stays positive and p < 0.05 on that larger sample. Until then: flat sizing,
no per-pair exits, no runner.

---

## 9. beta_shift is firing on the wrong population (and the fix)

Prompted by "should we hold the legs longer?" The answer turned out to be that the
**time stop is not the binding constraint — `beta_shift` is.**

### The exit mix says the target rule is dead code
```
target      8 exits   all entries 2026-09-18 .. 2026-09-29   (pre-cross era)
cross       1 exit    2026-10-11
beta_shift 17 exits   2026-09-16 .. 2026-10-11   (47% of all exits, median 40h)
time_stop   7 exits
```
With `exit_mode: cross`, `evaluate_open` takes the cross branch and never
evaluates `target_spread`; every `target` exit predates the switch. So the trades
that end "not reverted" are cut by the **β-drift gate**, not by the clock.

### Holding longer is nearly exhausted anyway
OU asymptotics from a −1.22σ entry with a 50h half-life:

```
horizon   P(revert to target)   marginal gain   hours per extra win
  167h          78.9%                 +7.4pt             22h
  250h          81.1%                 +2.1pt            120h
  334h          81.8%                 +0.7pt            470h
  668h          82.1%                 +0.0pt         42,821h
```
Live runs at `exit_horizon_mult: 3.33` ≈ 79% of the achievable reversion. And the
capital-efficiency check that actually matters with 10 concurrent slots:

```
live 3.33x    +160.0 bp   mean 92 bars   3.93 bp/bar
no time stop  +207.3 bp   mean 116 bars  4.04 bp/bar
```
+3% bp/bar for +25% holding time — nil. The proxy sweep that claimed otherwise is
an artifact: its **median** trade is negative at every hold length and the
top-1 trade is 21–1000% of the total (at max_hold=60 one trade *is* 10× the total).

### The real finding (ground truth from the journal)
```
beta_shift exits: 17
  fired while spread AHEAD of entry (already reverting): 11 (65%)  +$19.39
  fired while spread BEHIND (losing)                   :  6 (35%)  −$ 9.56
  mean reversion captured at exit: +20% of entry displacement
```
**65% of β_shift exits clip trades that are already winning**, at ~20% of the
available reversion. The other 35% are genuine protection — all six were losses.
The two effects roughly cancel, which is exactly why the aggregate valuation is
~+10 bp: the rule is not worthless, it is *firing on the wrong population*.

Mechanism: re-estimating β over a 30-day window while the spread reverts makes
**ordinary reversion look like β drift**. On the winning side the trigger is
mostly a false-positive generator.

### Implemented: asymmetric threshold
`positions.py` now chooses the threshold by side:
- spread **behind** entry → `beta_shift_min_posterior_sd` (2.0), unchanged — this
  is where the gate earns its keep;
- spread **ahead** of entry → `beta_shift_min_posterior_sd_when_ahead` (4.0), so
  a benign drift no longer clips a winner.

`_is_ahead_of_entry()` is direction-aware (LONG_A_SHORT_B is ahead when the
spread rises from entry; SHORT_A_LONG_B when it falls) and defaults to *not ahead*
when `entry_spread` is missing, which preserves the old behaviour for
un-backfilled positions. `exit_detail` now records `threshold_sd` and
`spread_ahead_of_entry` so the next review can measure the rule directly.
Tests: 4 new cases in `tests/test_risk_controls.py`; full suite **189 passed**.

### Validity caveat that shaped the decision
The tape replay was **not** used to set the threshold. Cross-checking replay exit
reasons against the journal showed only **13/26 agreement** — replay reconstructs
the spread with a *frozen* `beta_entry` while live `evaluate_open` uses the fresh
β each cycle, so β-drift is systematically under-detected. Any threshold sweep
from that harness (including the apparent "+13.3 bp at sd>=8") is unreliable.
The ahead/behind split above comes from the journal, which is ground truth.

### What to watch
This defers ~$19.39 of booked PnL into open risk across 11 trades. The hypothesis
is that they continue to the cross level or time stop and do better; the risk is
that some of those winners turn into the losers the gate would have avoided. The
new `exit_detail` fields make this measurable: after ~20 further exits, compare
realized PnL on trades that *would* have been β_shift-closed while ahead against
the $19.39 baseline. Revert the config to `when_ahead: 2.0` if it underperforms.
