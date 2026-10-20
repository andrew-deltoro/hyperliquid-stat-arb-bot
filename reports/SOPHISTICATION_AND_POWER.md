# Is the bot too sophisticated? — and is the validation choking ideas?

**Short answer: the bot is not too smart, it is under-measured.** 67 of 69 saved
walk-forward runs (97%) produced fewer than 20 trades — below the harness's own
"sample too small for a verdict" threshold — with a median of **6 trades**. A
pipeline that thin cannot reject an idea; it can only fail to measure one. The
fix is a cheap screen that produces **934 trades in ~1 minute**, and the first
thing it shows is where the real problem lies: **costs, not logic.**

---

## 1. The pasted checklist, answered against the bot's own data

The five "signs your bot is too sophisticated" were each checked against the
config, the source and the realized trades. Three of the five **do not hold**,
one holds partially, one holds — but not where the checklist looks.

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | Perfect backtest, terrible live | **Holds — for a specific reason** | The walk-forward harnesses never implement the live exit rule, so their out-of-sample claim does not cover the exit the bot runs. It is a *validation* gap, not a *logic* problem. |
| 2 | 10+ parameters ⇒ curve-fitting | **Mostly no** | 33 tunable numbers, and **zero** with the ≥5-decimal over-precision the claim describes. Values are round (1.0, 3.0, 0.25, 96h). Real risk is 4 hand-set half-life overrides. |
| 3 | Only works in one regime | **Can't be tested at n=33** | 33 realized exits across ~9 entry dates. Not one-regime over-perfection; *unmeasured* across regimes. |
| 4 | Overly complex logic | **Yes — but NOT the cross exit** | `exit_mode=cross` makes the **trailing-stop** system (`use_trailing_stop`, `trail_frac`, `trail_activation_frac`) unreachable. That is what the "unreachable knobs" finding refers to. The **cross exit is reachable, active, and separate** — see the correction in §4. |
| 5 | No robustness in walk-forward | **Often mis-stated** | The harnesses *are* out-of-sample. The failure is the opposite: too little sample to be robust at all. |

Two important corrections to the checklist itself:

- **"Perfect backtest" is not true here.** The walk-forward numbers are bad
  (−53% in one run), not suspiciously good. Claim 1's diagnostic assumes an
  *over*-fit curve. This bot fails for a different reason.
- **A long rule chain is not evidence of over-fitting.** Rule *length* is
  invisible to the market; only tuned *degrees of freedom* matter. The bot has
  a 6-rule exit chain with 2 tuned numbers. That is not the problem.

Exact parameter count with per-value precision: `33 tunable numbers, 0 with ≥5
decimal places, 5 booleans/structural toggles`. The `kalman_q=1e-05` and fee
constants that look over-precise are conventions, not fitted knobs.

## 2. The measurement diagnosis, measured

I inventoried every saved walk-forward output:

| metric | value |
|---|---|
| walk-forward runs saved | 69 |
| **distinct trade-sets** | **54** (15 files are re-runs of identical entries) |
| runs with **<20 trades** | **67 (97%)** |
| median trades per run | **6** |
| **all 522 trades pooled** | 522 |
| does the harness charge fees? | **No — walk_forward.py has no cost model** |

So the entire heavyweight validation corpus is **522 trades, before costs**. It
cannot distinguish a real edge from noise, which is exactly what the prior audit
found independently: *"the bot is not over-fit to one regime so much as
unmeasured across regimes."*

## 3. The cheap fix: a 1-sentence rule, no fitting

`scripts/simple_pair_screen.py` replaces per-pair Bayesian re-fitting with a
fixed rule:

> Spread = log A − log B. Enter when |z| ≥ 2 over a rolling 168-bar window.
> Exit on reversion to |z| ≤ 0.5, a stop at |z| ≥ 4, or after 72 bars.

No priors, no MAP, no kappa floor, no per-pair constants. Costs are charged at
**30 bp round trip** (2 legs × 2 sides × 4.5 bp taker + 3 bp slippage).

Result over the 15 live pairs, 1h bars, ~208 days of history:

| | heavyweight harness | simple screen |
|---|---|---|
| trades per pair, median | **6** | **61** |
| total trades | 522 (all 69 runs) | **934** (one run) |
| runtime | hours | **~1 minute** |
| costs charged | no | yes |

So the answer to "was anything rejected for lack of power?" is **yes, almost
everything** — the pipeline was underpowered by roughly **20x**.

## 4. CORRECTION: the cross exit is not the problem — it works

**This supersedes the net figure in §7.** My first screen exited *at*
convergence (`|z| <= 0.5`). The live bot does the opposite: `exit_mode=cross`
closes **past** convergence, out the opposite side, by `cross_frac x
|entry_spread|` (`positions.py:597-614`). Those are different exits, and only
one of them is the live rule.

I ran both on identical entries (`scripts/exit_family_compare.py`):

| exit family | n | gross% | net% | hit% | bars | t |
|---|---|---|---|---|---|---|
| revert (at convergence) | 969 | +0.254 | **−0.046** | 55.0 | 20 | −0.29 |
| **cross (live rule, past convergence)** | 808 | +0.292 | **−0.008** | 44.7 | 72 | −0.04 |

Within the cross run, by exit reason:

| exit | n | gross mean |
|---|---|---|
| **cross** | **90** | **+6.574%** |
| time | 411 | +1.012% |
| stop | 307 | −2.513% |

**The cross exit is earning its keep.** Trades that run past convergence
average **+6.574%** gross — these are precisely the RSR-style continuations.
Holding past convergence beats exiting at convergence by **+0.039%/trade**, and
more than doubles the net edge (−0.046% → −0.008%).

So two corrections to my own analysis:

1. **The cost problem is less severe than §7 implied.** My figure used the
   conservative revert exit. Under the live cross exit the net mean is −0.008%,
   i.e. within rounding of breakeven rather than clearly negative.
2. **The "unreachable knob" finding is unrelated to cross.** It concerns the
   *trailing-stop* family that `exit_mode=cross` bypasses. The cross exit itself
   is active and should be kept.

The honest remaining statement: the cross exit turns a clearly-negative
proposition into a roughly-breakeven one on this proxy data. That is an argument
for **keeping** it and for finding cost reduction, not for changing the exit.

### 4.1 Is that +6.574% just a few lucky continuations?

No — it is diffuse (`scripts/cross_episode_concentration.py`, 96 cross trades):

| test | result |
|---|---|
| gross mean | **+6.744%** |
| gross **median** | **+6.181%** (median ≈ mean ⇒ not outlier-driven) |
| worst single cross trade | **+1.58%** (every cross trade was positive) |
| best trade's share of total | **2.7%** |
| best 3 trades' share | 7.8% |
| leave-one-out, dropping best 10 | still **+5.846%** |
| distinct episodes (same pair, ≤7d) | **74**; best episode only **4.8%** of total |
| distinct pairs contributing | **15 of 15** (each 2.4–10.7%) |
| distinct calendar weeks | **27**; best week only **8.6%** |
| entry span | 2026-03-31 → 2026-10-13 (~6.5 months) |

No single trade, pair, episode or week dominates. The continuation capture is a
real, repeated, broadly-distributed feature of these spreads.

**The important caveat on how to read it.** A `cross` exit *by construction* only
fires when the spread travelled all the way past convergence to the far side —
which mechanically excludes the losers, since those exit via `stop`. So +6.744%
is the return **conditional on continuation**, not the strategy's expected
per-trade return. The strategy-level mix is the cross winners (+6.744%, n=96)
against the stop losers (−2.513%, n=307) and the time-stops (+1.012%, n=411).

Correct conclusion: **the cross exit reliably harvests large, genuinely
distributed profit from the continuations** — it is not one RSR episode. It
remains a selection *within* the strategy, so it cannot rescue the strategy on
its own; it raises the average by capturing the right tail of the reversion.

## 5. CORRECTION: the "stop leg" was my proxy's artefact, not the live bot

I previously wrote that *"the stop leg at −2.513% across 307 trades is where the
strategy bleeds."* **That was wrong as a statement about the live bot.** Checking
the live semantics (`positions.py:325-327`):

```python
entry_spread = sig.current_spread                 # CENTRED spread (z * sigma)
target_spread = target_frac * entry_spread        # 0.75 x
stop_spread   = stop_mult * entry_spread          # 3.0  x
```

The stop is placed at **3× the distance from the centre**, not 3× from entry. So
for an entry at z = −2 the stop sits at z = −6, i.e. **4 more sigma of adverse
move**. It is a genuinely *wide* stop.

My screen used `z_stop = 4.0` in *absolute* terms, which from an entry at z = −2
is only 2 sigma of adverse room — half as wide. Hence my proxy fired 325 stops
where a live-shaped stop fires 9.

Running the live-shaped relative stop (`scripts/stop_efficiency.py`):

| | value |
|---|---|
| stops that fire | **9 of 573 trades (1.6%)** |
| actual mean on stopped | −12.06% |
| counterfactual (no stop) | −13.99% |
| **mean saved by the stop** | **+1.93%** per stopped trade |
| would have ended profitable without it | 22% |
| live `divergence_stop` share of realized exits | **1 / 33 ≈ 3%** (audit) |

**So the stop is protective, rarely fires, and is not the bleed.** It saves
~1.9% on the rare occasions it triggers. The 307-trade −2.513% "stop leg" was an
artefact of my stop being twice as tight as live. My apology for the earlier
claim — the corrected picture is that live's stop is nearly dormant.

### 5.1 Is there anything to gain in the stop dimension?

Width sweep on identical entries (net of 30 bp):

| K (× centre distance) | n | stops | net mean% | net total% | t |
|---|---|---|---|---|---|
| 1.5 | 712 | 177 (25%) | −0.167 | −118.7 | −0.65 |
| 2.0 | 606 | 58 (10%) | −0.107 | −64.6 | −0.35 |
| **2.5** | 580 | 19 (3%) | **−0.074** | **−42.8** | −0.24 |
| 3.0 (live) | 573 | 9 (2%) | −0.115 | −65.7 | −0.40 |
| 4.0 | 567 | 2 (0%) | −0.127 | −71.8 | −0.41 |
| none | 566 | 0 | −0.112 | −63.3 | −0.37 |

Every row is negative and the spread between them is small and noise-dominated
(9–19 stops). If anything, **K = 2.5 edges K = 3.0** — a *slightly tighter* stop —
but with so few stop events this is not a defensible change. **Conclusion: the
stop is not a meaningful efficiency lever.** Leave it alone.

### 5.2 Where the lever is NOT

| exit | live share | naive PnL share | true value (ablation) |
|---|---|---|---|
| beta_shift | 48.5% | ~6% (+$7 of $116) | **keep** (−5.4 bp if removed) |
| target | 24.2% | +17% | — |
| **time_stop** | 15.2% | −8% | **destroying +43.2 bp/trade; pre-empts beta_shift** |
| divergence_stop | 3.0% | +0.0% | keep (−16.5 bp if removed) |

The naive "PnL by exit reason" column is **misleading for protective rules** — it
cannot see the loss a rule prevented. Valued properly by ablation, `beta_shift`
and `divergence_stop` both earn their place, and it is the **time stop** that
destroys value: both the real book and the pooled proxy agree that relaxing it
helps monotonically, and on the proxy it flips the net result positive (§7.4).

## 6. Is the 4h "first bar past centre" exit good or bad? — Net GOOD

The config notes `cross_exit_frac` is **inert at 4h**: the exit fires at the
earliest 4h bar on the opposite side regardless of frac. So live is effectively
*"exit on the first 4h bar past the centre"* — cruder and more aggressive than a
level-targeted exit. Measured at matched calendar windows (7d lookback, 3d hold):

| | 1h | **4h (live)** | direction |
|---|---|---|---|
| trades | 864 | 767 | — |
| **hit rate** | 44.1% | **48.1%** | better |
| net mean | −0.045% | **−0.009%** | better |
| t-stat | −0.23 | **−0.04** | better |
| **cross trades** | 96 @ +6.744% | 76 @ **+7.622%** | better |
| cross share of gross | 39% | **48%** | better |
| stop-outs | 325 | **151** | fewer |
| stop mean when it fires | −2.546% | **−5.243%** | worse |

**Verdict: net good, and for a specific reason.** The coarser cadence is *less
twitchy*: intraday noise averages out, so **54% fewer whipsaw stop-outs** and a
higher hit rate, and the cross continuations run **further** (+7.62% vs +6.74%)
because the exit can't fire until a 4h bar has already travelled past the
centre. Cross exits rise from 39% to **48% of gross profit**.

**The cost of that:** when a stop does fire, a single 4h bar can move much
further, so the stop is **deeper** (−5.24% vs −2.55%). You trade many shallow
stop-outs for few deep ones. On this data the trade is clearly worth it — better
net, better hit rate, better t.

**But be careful not to over-read it.** Both cadences remain **net ≈ zero and
statistically indistinguishable from zero** (CI crosses zero in both). The 4h
cadence improves the margin; it does not create an edge. Its real value is that
it is *cheaper* (fewer trades per unit time) and *less noisy* — which matters
because costs are the binding constraint (§7).

## 7. The exit rules valued properly: beta_shift is fine, the TIME STOP is the bleed

### 7.1 The naive "PnL by exit reason" table is misleading

The audit (and my §5.2 summary of it) says `beta_shift` fires **48.5% of exits
for only ~+$7 of +$116**, implying it is dead weight. **That metric cannot value
a protective rule.** It credits a rule only with where the trade *ended*, never
with where it would have gone *without* it.

Concrete case from the real book:

| GMT/MEME | outcome |
|---|---|
| live: exits via `beta_shift` | **+100.9 bp** |
| without the rule: falls to `divergence_stop` | **−519.5 bp** |
| **true value of the rule** | **+620 bp** |

The naive table calls that `+100.9` and concludes "small". It is actually
`+620`. So I was repeating a measurement artefact.

### 7.2 Ablating each exit rule through the real evaluator

Same real paths, the bot's real `evaluate_open`, one rule disabled at a time
(`scripts/experiments/exit_rule_valuation.py`):

| rule disabled | fires now | mean bp | **delta bp** | 95% CI | P(d≤0) | verdict |
|---|---|---|---|---|---|---|
| `beta_shift` | 7 | 166.8 | **−5.4** | [−10.4, +0.0] | **1.00** | keep |
| `divergence_stop` | 1 | 155.7 | **−16.5** | [−60.0, +0.0] | **1.00** | keep |
| **`time_stop`** | 7 | **215.5** | **+43.2** | [−1.0, +128.6] | 0.11 | **suspect** |

Negative delta = disabling it *loses* money = the rule earns its place.

- **`beta_shift` is net positive** (+124.9 bp across its 7 trades). It *saves*
  882 bp by exiting early and *gives up* 757 bp — a real trade-off, net thin but
  positive. Not the problem.
- **`divergence_stop` is protective** and fires on only 1 trade. Fine.
- **The time stop is the one destroying value.** Disabling it *gains* **+43.2
  bp/trade** — the largest single effect found anywhere in this investigation.

### 7.3 Two independent methods agree

| method | time-stop effect |
|---|---|
| audit's tape sweep (`time stop x5.0`) | **+43.2 bp** |
| my counterfactual (disable `time_stop`) | **+43.2 bp** |

Exact agreement from two different procedures. The `exit_horizon_mult = 3.33 x
half-life` time stop fires on 7 of 23 entries and is cutting winners short.

**This is the lever.** And it is *not* the stop, the cross exit, or `beta_shift`.

### 7.4 What not to do about it

The CI is **[−1.0, +128.6] with P(d≤0) = 0.11** on 23 entries / 9 dates, with one
trade carrying 43% of PnL. That is a strong *signal*, not proof. Recommended next
step is a proper sweep of `exit_horizon_mult` (1x, 2x, 3.33x, 5x, none) on the
replay harness and on pooled proxy data — **not** a live config change.

### 7.4 The sweep: both sources agree, and it un-locks the first positive net result

`scripts/experiments/time_stop_sweep.py` sweeps the hold horizon on two
independent samples.

**A) Real entries** (23 positions, 9 dates) through the real evaluator — monotone
in the hold length:

| mult | ≈half-lives | time_stop fires | mean bp | **delta bp** | P(d≤0) |
|---|---|---|---|---|---|
| 0.50 | 0.15x | 18 | 97.4 | −73.9 | 0.96 |
| 1.00 | 0.30x | 14 | 112.9 | −58.3 | 0.93 |
| 2.00 | 0.60x | 10 | 168.3 | −3.0 | 0.57 |
| **3.33 (live)** | 1.00x | 7 | 178.7 | — | — |
| 5.00 | 1.50x | 2 | 197.6 | **+26.4** | 0.11 |
| 10.00 | 3.00x | 1 | 208.9 | **+37.6** | 0.11 |
| none | — | 0 | 214.5 | **+43.2** | 0.11 |

Every relaxation helps. Tightening hurts badly. The live setting is on the wrong
side of a monotone curve.

**B) Pooled proxy** (15 pairs, ~208 days, 1h) — also monotone, and it crosses
into profit:

| max_hold | ≈days@4h | n | **net mean%** | net total% | t | annSR |
|---|---|---|---|---|---|---|
| **18 (live 3d)** | 3.0 | 1410 | **−0.157** | −221.2 | −1.58 | −0.93 |
| 30 | 5.0 | 1151 | −0.115 | −132.2 | −0.85 | −0.43 |
| 42 | 7.0 | 1020 | −0.017 | −17.2 | −0.10 | −0.05 |
| 60 | 10.0 | 905 | **+0.036** | +32.6 | 0.19 | 0.08 |
| 90 | 15.0 | 833 | **+0.037** | +30.9 | 0.17 | 0.07 |
| 120 | 20.0 | 792 | **+0.147** | +116.3 | 0.62 | 0.25 |
| 200 | 33.3 | 695 | **+0.193** | +134.4 | 0.68 | 0.35 |
| unlimited | — | 555 | **+0.559** | +310.2 | 1.52 | 1.14 |

**This is the first positive net-of-cost result found anywhere in this
investigation.** At the live 3-day horizon the proxy is clearly negative
(−0.157%, t = −1.58). At a 10-day horizon it is positive (+0.036%). At 33 days,
+0.193%. The sign flips purely by holding longer.

### 7.5 Mechanism: the time stop is pre-empting `beta_shift`, and doing it worse

For each of the 7 time-stopped trades, what would have happened without it?

| pair | live bp | no-time-stop bp | would exit via | effect bp |
|---|---|---|---|---|
| AAVE/COMP | −345.3 | **+244.1** | beta_shift | **+589.4** |
| ORDI/SOL | −258.9 | **+40.0** | beta_shift | **+298.9** |
| SNX/DYDX | +164.1 | +218.6 | beta_shift | +54.5 |
| SNX/DYDX | +148.4 | +202.6 | beta_shift | +54.2 |
| LTC/DOGE | −98.8 | −88.6 | beta_shift | +10.2 |
| ETC/NEO | +103.2 | +95.1 | beta_shift | −8.1 |
| AAVE/COMP | +404.6 | +400.2 | beta_shift | −4.4 |

- **5 of 7 time-stopped trades were recovering winners being cut.**
- Mean effect of removing the time stop: **+142.1 bp** on those trades.
- **In all 7 cases the counterfactual exit is `beta_shift`.**

That last row is the whole story. Relaxing the time stop doesn't remove an exit —
it **hands the decision to `beta_shift`**, which then exits at a better point.
The time stop is *redundant* with `beta_shift` but strictly worse at the job: it
fires on a clock rather than on evidence, and it pre-empts the smarter rule. The
two rules are not complements; the clock is cutting the evidence-based rule off
at the pass.

### 7.6 What this does and does not license

**Established** (two independent samples, agreeing direction): the live
`exit_horizon_mult = 3.33` is set too tight, and raising it improves results on
both the real book (+26 to +43 bp) and the pooled proxy (sign flips from negative
to positive).

**Not established**, and I am deliberately not proposing a live change:

- The **real-entry** sample is 23 entries / 9 dates; the +142.1 bp mechanism
  figure rests on 7 trades and CI crosses zero.
- The proxy's **best row is not deployable** — "unlimited" means holding to the
  end of the data with capital locked indefinitely. The *deployable* range
  (60–200 bars) yields a **modest** +0.036% to +0.193% net, t = 0.17–0.68.
- Holding longer ties up capital longer, so per-trade improvement ≠ annual
  improvement. The annualised column accounts for this and still improves
  (0.08 → 0.35), but both are weak.
- The replay can only see paths that occurred, so a 5x hold may resolve as
  `open_at_path_end` rather than a real exit.

**Suggested next step:** sweep `exit_horizon_mult` at 5x and 10x on a larger
point-in-time entry sample before touching the live config — and specifically
test the hypothesis that `time_stop` and `beta_shift` should not both exist.

## 8. What the screen, the null tests, and the bottom line say

Pooled across 15 live pairs, 934 trades:

| metric | value |
|---|---|
| gross mean per trade | **+0.240%** |
| net mean after 30 bp | **−0.060%** |
| median net | +0.501% |
| hit rate | 55% |
| t-stat | −0.37 |
| 95% CI (net) | [−0.369%, +0.282%] |

By exit reason: reversion-triggered exits average **+3.58%**, stops **−2.38%**,
time-stops **−3.40%**. The rule's edge is entirely in the reversions, and the
costs plus the losers eat it.

> **Note:** these pooled figures use the *revert* exit. The live rule is the
> *cross* exit, which performs better — see §4.

Two decompositions separate "the signal is absent" from "the signal is too
small to trade":

**TEST A — vs the null.** Run the same rule on **random unrelated series pairs**.
It earns **+0.543%** gross, *more* than the real pairs' +0.240%. One-tailed
**p = 0.64**. The "cointegration" signal is statistically indistinguishable from
the drift of unrelated crypto series.

**TEST B — randomized entry.** Keep each real trade's pair and holding period,
but choose the entry bar and direction at random. That yields **−0.007%** gross.
Real entries earn +0.240%. One-tailed **p = 0.030**.

## 9. The answer to the question asked

The z-score **entry timing does carry information** (p=0.030). What kills it is
that **the market factor is larger than the signal**: 934 trades of *random*
long/short crypto exposure also earn +0.54%, so the pair-selection and spread
construction contribute nothing beyond what any directional crypto bet would.

Conclusion: the bot's problem is **not** over-complication rejecting good ideas.
Nor is it the cross exit, which tests *positive* (§4). The bind is that **the
edge per trade is close to the round-trip cost (~30 bp on two legs)** — with the
live cross exit the net is roughly breakeven (−0.008%/trade), not comfortably
positive.

The practical implication is the opposite of the checklist's advice: the bot
does not need simpler *logic*, and it should **keep** the cross exit. It needs
either **lower-cost execution** or a **larger expected move** before the edge is
comfortably positive. Simplification's value is diagnostic — it lets you see
this in a minute instead of an hour.

## 10. What was fixed / added

- `scripts/simple_pair_screen.py` — the 1-sentence fixed-rule screen, now with
  `--exit-mode revert|cross` so it can mirror the live exit. Output saved to
  `reports/simple_screen.log`.
- `scripts/power_or_disproof.py` — TEST A (null distribution on random pairs)
  and TEST B (randomized entry dates), both ~1 minute.
- `scripts/exit_family_compare.py` — measures the cross-vs-revert difference on
  identical entries (the §4 result).
- `scripts/cross_episode_concentration.py` — leave-one-out and episode/pair/week
  concentration of the cross-exit edge (§4.1).
- `scripts/stop_efficiency.py` — stop counterfactual and width sweep, using the
  live's *relative* stop semantics (§5).
- `scripts/experiments/exit_rule_valuation.py` — values each exit rule by
  disabling it and re-stepping the real paths (§7).
- `scripts/experiments/beta_shift_sweep.py` — sweeps
  `beta_shift_min_posterior_sd` through the real evaluator (§7).
- `scripts/experiments/time_stop_sweep.py` — sweeps the hold horizon on both the
  real entries and the pooled proxy, plus the mechanism diagnostic (§7.4, §7.5).
- All are **additive**: no live config, no exit path, and no existing harness
  was modified. The live bot is untouched and still running.

## 11. Deliberate non-actions (and why)

- **Did not change any live parameter.** One trade currently carries most of the
  realized PnL (43% of summed PnL from the single best trade), so any live change
  would be unmeasurable.
- **Did not strip the Bayesian entry model.** The entry timing tests *positive*
  (p=0.030). Removing the one component that shows signal, on a config-based
  suspicion, would be simplifying the wrong part.
- **Did not touch the cross exit.** It was questioned, measured, and kept: it
  beats the revert exit by +0.039%/trade and its continuation trades average
  +6.744% across 96 trades, 74 episodes, 15 pairs and 27 weeks (§4, §4.1).
- **Did not touch the stop.** Its live semantics are 3× the distance from the
  centre, not 3× from entry, so it fires on only 1.6% of trades and is net
  protective. The width sweep is noise-dominated (§5).
- **Did not touch `beta_shift`.** I suspected it, then measured it: disabling it
  *costs* 5.4 bp on the real book, and it saves 882 bp against 757 bp given up
  (§7). The audit's "48.5% of exits for 6% of PnL" is a measurement artefact of
  naive attribution, not evidence of dead weight.
- **Did not touch the time stop either** — despite it being the clear suspect and
  despite two sources agreeing. The proxy's best rows are not deployable
  (unlimited hold) and the deployable range is only +0.036% to +0.193% net at
  t = 0.17–0.68. It needs a larger point-in-time sample first (§7.6).
- **Did not delete the walk-forward harnesses.** They answer a different
  question (does the *fitted model* generalise). They should be pointed at the
  live exit rule, per the prior audit's recommendation #2.

## 12. Honest limits

- **Survivorship.** The 15 pairs are today's live list, chosen with hindsight.
- **One regime.** ~208 days of 1h history is a single market stretch.
- **Proxy rule.** The screen's fixed exit is *not* the live exit rule, so this
  measures whether an idea is **marginal or measurable**, not whether the live
  bot works.
- **No funding costs.** Perp funding is ignored; for a delta-neutral pair it
  partially nets out but is not zero.
- **TEST A pools correlated series**, so its "random pairs" are not independent
  draws; treat p=0.64 as indicative of "no better than naive exposure", not as a
  precise p-value.
- **Live is 4h, the proxy is 1h on the same calendar windows.** §6 compares both;
  the 4h numbers are the live-shaped ones, but neither is the live exit rule
  bit-for-bit.

## Reproduce

```
.venv/bin/python scripts/simple_pair_screen.py                        # 15 pairs, ~1 min
.venv/bin/python scripts/simple_pair_screen.py --exit-mode cross       # live-shaped exit
.venv/bin/python scripts/simple_pair_screen.py --exit-mode cross --interval 4h \
    --lookback 42 --max-hold 18                                        # live cadence
.venv/bin/python scripts/power_or_disproof.py --shuffles 300
.venv/bin/python scripts/exit_family_compare.py
.venv/bin/python scripts/cross_episode_concentration.py
.venv/bin/python scripts/stop_efficiency.py
.venv/bin/python scripts/experiments/exit_rule_valuation.py
.venv/bin/python scripts/experiments/beta_shift_sweep.py
.venv/bin/python scripts/experiments/time_stop_sweep.py
```
