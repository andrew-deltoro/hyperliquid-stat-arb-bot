"""
Full walk-forward validation of current live config vs pre-improvement baseline.

Baseline (old):  fixed target 0.5, no trail, no HL filter, 72h hold
Current (new):   trail 25%/50%, HL filter <= 96h, 120h hold, target 0.75 fallback

Same entries. Same pairs. Side-by-side comparison.
"""
from __future__ import annotations
import sys, traceback
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
sys.stdout.reconfigure(line_buffering=True)

import numpy as np
import pandas as pd
import pymc as pm
import gc
from pathlib import Path

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices

PAIRS = [
    ("SNX", "DYDX"), ("MEME", "TIA"), ("GMT", "MEME"),
    ("SNX", "TIA"), ("ORDI", "SOL"), ("SNX", "SUI"),
]

INTERVAL = "1h"
LOOKBACK_HOURS = 24 * 200
FIT_WINDOW_HOURS = 24 * 30
TEST_WINDOW_HOURS = 24 * 130

# Old baseline
OLD_TARGET_FRAC = 0.5
OLD_HOLD_HOURS = 72
OLD_STOP_MULT = 3.0
OLD_HL_MAX = 999999  # no filter

# Current live config
NEW_TARGET_FRAC = 0.75
NEW_HOLD_HOURS = 120
NEW_STOP_MULT = 3.0
NEW_HL_MAX = 96.0
TRAIL_FRAC = 0.25
TRAIL_ACTIVATION_FRAC = 0.50

Z_ENTRY_MIN = 1.0

OUT_DIR = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports")
PARTIAL_CSV = OUT_DIR / "full_config_validation_partial.csv"
FINAL_CSV = OUT_DIR / "full_config_validation.csv"


def fit_coint_map(log_a, log_b):
    log_b_mean = float(np.mean(log_b))
    log_b_c = log_b - log_b_mean
    with pm.Model() as model:
        alpha_c = pm.Normal("alpha_c", mu=0.0, sigma=5.0)
        beta = pm.Normal("beta", mu=1.0, sigma=1.0)
        sigma = pm.HalfNormal("sigma", sigma=0.5)
        nu = pm.Exponential("nu", lam=1 / 10.0)
        mu = alpha_c + beta * log_b_c
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)
        try:
            me = pm.find_MAP(progressbar=False, maxeval=500, method="L-BFGS-B")
        except Exception:
            return None
    alpha = float(me["alpha_c"]) - float(me["beta"]) * log_b_mean
    return {"alpha": alpha, "beta": float(me["beta"])}


def half_life_from_spread(spread):
    y = spread - spread.mean()
    lag = y[:-1]
    diff = np.diff(y)
    if lag.std() < 1e-9:
        return float("inf")
    theta = float(np.polyfit(lag, diff, 1)[0])
    if theta >= 0:
        return float("inf")
    return float(-np.log(2) / theta)


def simulate_baseline(log_a, log_b, T, coint, current, is_long):
    """Fixed target 0.5, no trail, 72h hold."""
    target = OLD_TARGET_FRAC * current
    stop = OLD_STOP_MULT * current
    entry_spread = log_a[T] - coint["alpha"] - coint["beta"] * log_b[T]
    for t in range(1, OLD_HOLD_HOURS + 1):
        sp = log_a[T + t] - coint["alpha"] - coint["beta"] * log_b[T + t]
        cur = current + (sp - entry_spread)
        if is_long:
            if cur >= target: return {"pnl_pct": (cur-current)*100, "reason": "target", "hours": t}
            if cur <= stop:   return {"pnl_pct": (cur-current)*100, "reason": "stop",   "hours": t}
        else:
            if cur <= target: return {"pnl_pct": (current-cur)*100, "reason": "target", "hours": t}
            if cur >= stop:   return {"pnl_pct": (current-cur)*100, "reason": "stop",   "hours": t}
    sp_f = log_a[T+OLD_HOLD_HOURS] - coint["alpha"] - coint["beta"] * log_b[T+OLD_HOLD_HOURS]
    cur = current + (sp_f - entry_spread)
    return {"pnl_pct": ((cur-current) if is_long else (current-cur))*100,
            "reason": "time_stop", "hours": OLD_HOLD_HOURS}


def simulate_current(log_a, log_b, T, coint, current, is_long):
    """Trail 25% activation 50%, NO target fallback (matches live code)."""
    stop = NEW_STOP_MULT * current
    entry_spread = log_a[T] - coint["alpha"] - coint["beta"] * log_b[T]
    entry_abs = abs(current)
    trail_dist = TRAIL_FRAC * entry_abs

    if is_long:
        activation_level = current + TRAIL_ACTIVATION_FRAC * entry_abs
    else:
        activation_level = current - TRAIL_ACTIVATION_FRAC * entry_abs

    best = current
    activated = False

    for t in range(1, NEW_HOLD_HOURS + 1):
        sp = log_a[T + t] - coint["alpha"] - coint["beta"] * log_b[T + t]
        cur = current + (sp - entry_spread)

        # Track best (closer to zero)
        if is_long and cur > best: best = cur
        if not is_long and cur < best: best = cur

        # Check stop FIRST (before trail)
        if is_long and cur <= stop:
            return {"pnl_pct": (cur-current)*100, "reason": "stop", "hours": t}
        if not is_long and cur >= stop:
            return {"pnl_pct": (current-cur)*100, "reason": "stop", "hours": t}

        # Check activation
        if not activated:
            if is_long and cur >= activation_level: activated = True
            if not is_long and cur <= activation_level: activated = True

        # Check trail exit (only if activated) — no target fallback
        if activated:
            if is_long and cur < best - trail_dist:
                return {"pnl_pct": (cur-current)*100, "reason": "trail", "hours": t}
            if not is_long and cur > best + trail_dist:
                return {"pnl_pct": (current-cur)*100, "reason": "trail", "hours": t}

    sp_f = log_a[T+NEW_HOLD_HOURS] - coint["alpha"] - coint["beta"] * log_b[T+NEW_HOLD_HOURS]
    cur = current + (sp_f - entry_spread)
    return {"pnl_pct": ((cur-current) if is_long else (current-cur))*100,
            "reason": "time_stop", "hours": NEW_HOLD_HOURS}


def run_pair(coin_a, coin_b):
    pair = f"{coin_a}/{coin_b}"
    print(f"--- {pair} ---", flush=True)
    try:
        ts, ca, cb = aligned_close_prices(coin_a, coin_b, interval=INTERVAL,
                                          lookback_hours=LOOKBACK_HOURS)
    except Exception as e:
        print(f"  fetch failed: {e}", flush=True)
        return []
    if len(ts) < FIT_WINDOW_HOURS + TEST_WINDOW_HOURS + NEW_HOLD_HOURS:
        print(f"  insufficient: {len(ts)} bars", flush=True)
        return []

    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    n = len(log_a)
    test_end = n - NEW_HOLD_HOURS
    test_start = test_end - TEST_WINDOW_HOURS

    trades = []
    iters = 0
    for T in range(test_start, test_end, 48):
        iters += 1
        if iters % 10 == 0:
            gc.collect()

        fit_start = T - FIT_WINDOW_HOURS
        if fit_start < 0:
            continue

        coint = fit_coint_map(log_a[fit_start:T], log_b[fit_start:T])
        if coint is None:
            continue

        spread_fit = log_a[fit_start:T] - coint["alpha"] - coint["beta"] * log_b[fit_start:T]
        spread_c = spread_fit - spread_fit.mean()
        sd = float(spread_c.std())
        if sd < 1e-9:
            continue
        current = float(spread_c[-1])
        z = current / sd
        if abs(z) < Z_ENTRY_MIN:
            continue

        hl = half_life_from_spread(spread_fit)
        is_long = current < 0

        # Baseline always trades
        base_out = simulate_baseline(log_a, log_b, T, coint, current, is_long)
        base_out.update({"pair": pair, "strategy": "baseline_old",
                         "entry_z": z, "half_life": hl, "entry_idx": T})
        trades.append(base_out)

        # Current config: HL filter
        if hl <= NEW_HL_MAX:
            cur_out = simulate_current(log_a, log_b, T, coint, current, is_long)
            cur_out.update({"pair": pair, "strategy": "current_live",
                            "entry_z": z, "half_life": hl, "entry_idx": T})
            trades.append(cur_out)

    print(f"  {len(trades)} trade-strategy rows", flush=True)
    return trades


def main():
    all_t = []
    for i, (a, b) in enumerate(PAIRS):
        pair_trades = run_pair(a, b)
        all_t.extend(pair_trades)
        if pair_trades:
            pd.DataFrame(all_t).to_csv(PARTIAL_CSV, index=False)
            print(f"  [saved partial: {len(all_t)} rows]", flush=True)
        gc.collect()

    if not all_t:
        print("No trades — nothing to analyze.", flush=True)
        return

    df = pd.DataFrame(all_t)
    df.to_csv(FINAL_CSV, index=False)
    print(f"\nSaved to {FINAL_CSV}", flush=True)

    print()
    print("=" * 88)
    print("FULL CONFIG VALIDATION: OLD BASELINE vs CURRENT LIVE")
    print("=" * 88)
    print()

    def stats(sub):
        if len(sub) < 3: return None
        pnls = sub.pnl_pct.values
        n = len(pnls)
        rng = np.random.default_rng(42)
        boot = np.array([(lambda s: s.mean()/s.std() if s.std()>0 else 0.0)(
            rng.choice(pnls, size=n, replace=True)) for _ in range(10000)])
        lo, med, hi = np.percentile(boot, [2.5, 50, 97.5])
        return n, 100*(pnls>0).mean(), pnls.mean(), pnls.sum(), lo, med, hi

    print(f"{'strategy':>18}  {'n':>4}  {'win%':>6}  {'mean':>7}  {'total':>8}  {'CI low':>8}  {'median':>8}  {'avg_h':>7}")
    print("-" * 82)

    for label, key in [("baseline (old)", "baseline_old"), ("current (new)", "current_live")]:
        sub = df[df.strategy == key]
        s = stats(sub)
        if s:
            n, win, mean, tot, lo, med, hi = s
            avg_h = sub.hours.mean()
            print(f"{label:>18}  {n:>4}  {win:>5.1f}  {mean:>+6.3f}  {tot:>+7.2f}  {lo:>+7.3f}  {med:>+7.3f}  {avg_h:>7.1f}")

    print()
    print("Exit reason breakdown:")
    print(df.groupby(["strategy", "reason"]).size().unstack(fill_value=0))
    print()

    # Improvement calc
    base = df[df.strategy == "baseline_old"].pnl_pct.values
    curr = df[df.strategy == "current_live"].pnl_pct.values
    if len(base) >= 3 and len(curr) >= 3:
        improvement = (curr.mean() - base.mean()) / abs(base.mean()) * 100
        print(f"Mean PnL improvement: {improvement:+.1f}%")
        print(f"Trade count change: {len(curr)} vs {len(base)} ({100*(len(curr)-len(base))/len(base):+.1f}%)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\nEXPERIMENT CRASHED", flush=True)
        traceback.print_exc()
        sys.exit(1)
