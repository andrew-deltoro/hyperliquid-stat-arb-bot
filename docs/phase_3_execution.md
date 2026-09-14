# Phase 3 — Execution: A Study Guide

*Companion notes for the stat-arb bot. Read alongside McElreath,* Statistical Rethinking, *2nd ed.*

**Status: living document. Updated as phases 3.3, 3.4, and 4 are added.**

---

## 0. The one-sentence summary

Phase 1 said *what* to trade. Phase 2 said *when* and *how much*. Phase 3 is the **state machine that actually manages a portfolio over time**.

---

## 1. Architecture: the four moving parts

```
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│   SIGNALS       │─────▶│   POSITIONS     │─────▶│     STATE       │
│ (statistical)   │      │  (lifecycle)    │      │  (persistence)  │
└─────────────────┘      └─────────────────┘      └─────────────────┘
        ▲                                                 │
        │                                                 │
        │              ┌─────────────────┐                │
        └──────────────│   SCHEDULER     │◀───────────────┘
                       │ (orchestration) │
                       └─────────────────┘
```

**Signals** — Bayesian statistics. Fits models, computes P(revert), suggests entry or skip.

**Positions** — Trade lifecycle. Opens, monitors, closes. Doesn't do math, follows rules.

**State** — JSON file on disk. Persists positions across scheduler runs.

**Scheduler** — Runs every 4 hours. Calls the other three in the right order.

Each module is testable independently. **This separation is deliberate** — you can change the signal model without touching the position manager.

---

## 2. The three entry gates (McElreath Ch 3 §3.2.3)

A signal fires only if **all three** conditions hold:

```python
enough_z    = |z| >= 1.5            # deviation is meaningful
enough_edge = p_revert - 0.5 >= 0.05 # edge above coin flip
models_ok   = coint.converged and ou.converged
```

**Gate 1: `|z| >= 1.5`**

The spread is far enough from its mean to be worth trading. **Note this is a *naive* z** — computed from `spread / std(spread)`. We use it as a *filter*, not as the final signal. The actual signal is `P(revert)`.

Why 1.5? It's a heuristic. Lower and you're chasing noise. Higher and you miss opportunities. **Tune this empirically** once you have 2 weeks of dry-run data.

**Gate 2: `edge >= 0.05`**

`P(revert) − 0.5 >= 0.05` means P(revert) > 0.55. **The posterior must be meaningfully above 0.5.**

This is where the Student-t likelihood and posterior predictive simulation earn their keep. A naive z-score would fire at |z| = 2 regardless of σ, ν, or half-life. Our gate says: *given how fat the tails are and how slow the reversion is, is there actually an edge?*

**Gate 3: convergence**

Both models must have `max_rhat < 1.05`. **This is a safety gate.** If the sampler didn't converge, we don't trust the posterior, so we don't trade.

**Caveat:** 1.05 is loose. PyMC warns at 1.01. For a live bot, tighten this to 1.02 for at-risk pairs like ETH/BTC.

**If any gate fails, `action = SKIP`.** The signal is logged but not traded.

---

## 3. Position construction: β is a hedge ratio

When a signal fires:

```python
leg_a_usd = notional / 2
leg_b_usd = (notional / 2) * β
```

For LINK/PYTH with β = 0.636, notional = $78:

```
leg_a_usd = $39  (LONG LINK)
leg_b_usd = $25  (SHORT PYTH)
```

**Why `β`?**

Recall the spread definition: `spread = log(A) − α − β·log(B)`. β is how many units of B's log-move correspond to one unit of A's. To stay market-neutral, we need `β` dollars of B for each dollar of A. Anything else is an unhedged directional bet.

**The common mistake:** trading at 1:1 because "they're both crypto." For LINK/PYTH, 1:1 would mean $39 LINK vs $39 PYTH — you'd be **long 22% more β than intended**. Directional exposure in a "market-neutral" trade.

**This is what our old bot got catastrophically wrong.** `pair_mid = market["mid"] * 0.04` was a fixed ratio that ignored β entirely.

---

## 4. The four exit rules

Once open, a position is checked every cycle for four exit conditions:

### Rule 1: TARGET — spread returned toward equilibrium

```python
if long_a and current_spread >= target_spread:
    close("target")
```

`target_spread = 0.5 * entry_spread`. We exit when the spread has reverted halfway.

**Why half?** Bayesian logic. Full reversion (to zero) is rare. Half-reversion captures ~80% of the expected profit with much lower risk. **The 0.5 is a `config.yaml` parameter** — tune it.

### Rule 2: DIVERGENCE STOP — spread widened adversely

```python
if long_a and current_spread <= stop_spread:
    close("divergence_stop")
```

`stop_spread = 3.0 * entry_spread`. If the spread widens past 3× the entry deviation, we're wrong. Exit.

**Why 3×?** Statistical heuristic. With fat tails (ν ≈ 3–10), 2× is too tight (frequent stop-outs on noise), 5× is too loose (big losses). 3× is a compromise. **Tune empirically.**

### Rule 3: TIME STOP — horizon elapsed

```python
if now >= time_stop_iso:
    close("time_stop")
```

`time_stop = 2 × half_life`. If reversion hasn't happened by 2 half-lives, the model was wrong. Exit regardless of P&L.

**This is the large-world protection McElreath emphasizes (Ch 2 §2.2).** The 90% is a small-world number. If reality disagrees with the model, the time stop is how we exit.

### Rule 4: BETA DRIFT STOP — the pair has decoupled

```python
beta_shift = abs(fresh_beta - pos["beta_entry"]) / abs(pos["beta_entry"])
if beta_shift > 0.10:
    close("beta_shift")
```

If the fresh cointegration fit gives a β that's drifted more than 10% from entry, the pair is broken. The original β is no longer the right hedge ratio. Exit and re-evaluate.

**This catches structural breaks** — new exchange listings, liquidity changes, sector rotations. Without this rule, you'd keep trading a pair that has silently decoupled.

---

## 5. The coordinate-frame trap (the bug we just fixed)

**This is the most subtle bug in the whole system. Read it twice.**

When we open a position, we compute `entry_spread` in **centered coordinates**:

```
entry_spread = raw_spread_at_entry − mean_of_spread_at_entry
```

When we re-evaluate next cycle, we need `current_spread` in the **same coordinates**. But the model is refit on a **fresh 30-day window** each cycle. The new mean is different. So the "current spread" from a fresh fit is **not comparable** to the entry spread.

**Symptom:** the position never hits target, never hits stop, sits forever until time-stop.

**The fix:** store `entry_alpha`, `entry_mean_raw`, and `entry_sigma_raw` with each position. On re-evaluation, compute:

```
raw_now = log(price_a_now) − entry_alpha − entry_beta × log(price_b_now)
current_centered = raw_now − entry_mean_raw
```

**Now both spreads are in the same frame.** The comparison is meaningful.

**The lesson (McElreath Ch 4 §4.4.3.1):** parameters only mean something *in context*. The mean of a variable is not a property of the variable — it's a property of the window you computed it on. Store the context, not just the number.

---

## 6. Atomic JSON state

Positions are stored in `state/positions.json`:

```json
{
  "positions": [
    {
      "pair": "LINK/PYTH",
      "direction": "LONG_A_SHORT_B",
      "beta_entry": 0.6355,
      "entry_alpha": 4.36,
      "entry_mean_raw": -0.0017,
      "entry_sigma_raw": 0.0412,
      "entry_spread": -0.0903,
      ...
    }
  ]
}
```

**Why not a database?**

- Single writer. Only the scheduler touches it.
- Small. Even with hundreds of positions, < 1 MB.
- Human-readable. You can inspect it, diff it, back it up.
- **Atomic writes.** We write to `.tmp` and `mv` — this prevents corruption if the process dies mid-write.

**When to graduate to SQLite:** when you have > 1000 positions or need concurrent access. Not yet.

**When to graduate to a time-series DB:** when you're logging every bar for every pair. Not yet.

---

## 7. The scheduler loop (order matters)

Each cycle runs in this **exact order**:

```
1. Load config
2. Load state
3. Fit cointegration + OU for every pair → signals
4. Evaluate existing open positions using fresh prices
5. Open new positions from OPEN signals
6. Log everything to logs/cycle_*.log
7. Save state
```

**Why this order?**

**Step 3 before 4:** we need the fresh β for the β-drift check in step 4.

**Step 4 before 5:** free up capacity (close positions) before deciding what to open. Otherwise a full portfolio would never cycle.

**Step 5 last:** only open positions after we know the current state of the world.

**A common bug:** evaluating positions *after* opening new ones. The new position would be evaluated immediately and immediately closed.

---

## 8. Cron and its limitations

**The scheduler runs every 4 hours via cron.** In WSL:

```
0 */4 * * * cd /mnt/c/Users/andre/hyperliquid-stat-arb-bot && \
  /home/andre/hlbot-venv/bin/python scripts/run_cycle.py >> logs/cron.log 2>&1
```

**WSL caveat:** cron only runs when WSL is alive. Close all WSL terminals, and the VM shuts down. Three fixes:

1. **Keep WSL running** — Windows power settings to never sleep. Fragile.
2. **Windows Task Scheduler invokes `wsl.exe`** — reliable but clunky.
3. **Move to a VPS** — proper Linux. **This is what live trading requires.**

**The bot is designed to be portable.** Everything is `/mnt/c/...` paths and venv isolation, but on a VPS it's just `/home/user/...`. The config and state files move with it. **No code changes needed to migrate.**

---

## 9. Dry-run vs live: the transition plan

**Right now: dry-run.** The bot logs trades but does not execute. This is *deliberate*.

**The two-week dry-run protocol:**

1. Run the scheduler every 4 hours for 14 days
2. Log every signal, every open, every close
3. At the end, analyze `reports/trades.csv`:
   - **Win rate.** Should be > 60% if the model is calibrated.
   - **Average P&L per trade.** Should be positive after fees.
   - **Drawdown.** Largest peak-to-trough loss across the portfolio.
   - **Calibration.** Did "P = 0.90" trades actually work 90% of the time?
4. If all metrics pass, flip to live with **minimum size** (10% of model output).
5. After 2 more weeks, scale to full size.

**Why two weeks?**

- **Statistical:** with 4-hour cycles, ~84 cycles. That's ~84 independent trade decisions. Enough to compute a standard error.
- **Behavioral:** you need to see the bot through multiple regimes — weekends, volatility spikes, funding-rate resets, at least one structural break.

**Live trading has a single rule:** if the model loses money in dry-run, **do not go live**. Fix the model first.

---

## 10. If you remember only five things

1. **Signals are statistical. Positions are mechanical.** Don't let the stat model decide exit rules; don't let the position manager do statistics.
2. **β is a hedge ratio, not a round number.** Size the two legs proportionally.
3. **Four exit rules, not one.** Target + divergence stop + time stop + β drift. Each catches a different failure mode.
4. **Store the context, not just the number.** Entry spread alone is meaningless without entry α, β, mean, and σ.
5. **Never skip dry-run.** Two weeks of logged trades is your large-world calibration check. The small-world math can be perfect and still be wrong about reality.