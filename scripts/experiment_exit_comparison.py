"""
Walk-forward exit strategy comparison.

For each entry: simulate 3 exit strategies.
  A) Current: trail (50% activation, 25% retracement) + fixed target fallback
  B) Cross partial: exit when spread reaches -0.75*|entry| on opposite side
  C) Cross full: exit when spread reaches -1.00*|entry| on opposite side

Compare mean PnL, win rate, hold time.
"""
from __future__ import annotations
import sys, traceback, gc
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
sys.stdout.reconfigure(line_buffering=True)

import numpy as np
import pandas as pd
import pymc as pm
from pathlib import Path

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices

PAIRS = [
    ("SNX", "DYDX"), ("MEME", "TIA"), ("GMT", "MEME"),
    ("SNX", "TIA"), ("ORDI", "SOL"), ("SNX", "SUI"),
    ("xyz:MU", "xyz:TSM"),
]

INTERVAL = "1h"
LOOKBACK_HOURS = 24 * 200
FIT_WINDOW_HOURS = 24 * 30
TEST_WINDOW_HOURS = 24 * 130
HOLD_HORIZON_HOURS = 120

Z_ENTRY_MIN = 1.0
STOP_MULT = 3.0

# Strategy A (current)
A_TARGET_FRAC = 0.75
A_TRAIL_FRAC = 0.25
A_TRAIL_ACTIVATION_FRAC = 0.50

# Strategy B (cross partial)
B_CROSS_FRAC = 0.75    # exit when spread reaches +0.75*|entry| on opposite side

# Strategy C (cross full)
C_CROSS_FRAC = 1.00    # exit when spread reaches +1.00*|entry| on opposite side

OUT = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports")


def fit_coint_map(log_a, log_b):
    log_b_mean = float(np.mean(log_b))
    log_b_c = log_b - log_b_mean
    with pm.Model() as model:
        alpha_c = pm.Normal("alpha_c", mu=0.0, sigma=5.0)
        beta = pm.Normal("beta", mu=1.0, sigma=1.0)
        sigma = pm.HalfNormal("sigma", sigma=0.5)
        nu = pm.Exponential("nu", lam=1/10.0)
        mu = alpha_c + beta * log_b_c
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)
        try:
            me = pm.find_MAP(progressbar=False, maxeval=500, method="L-BFGS-B")
        except Exception:
            return None
    alpha = float(me["alpha_c"]) - float(me["beta"]) * log_b_mean
    return {"alpha": alpha, "beta": float(me["beta"])}


def simulate_A_current(log_a, log_b, T, coint, current, is_long):
    target = A_TARGET_FRAC * current
    stop = STOP_MULT * current
    entry_spread = log_a[T] - coint["alpha"] - coint["beta"] * log_b[T]
    entry_abs = abs(current)
    trail_dist = A_TRAIL_FRAC * entry_abs
    activation = current + A_TRAIL_ACTIVATION_FRAC * entry_abs if is_long else current - A_TRAIL_ACTIVATION_FRAC * entry_abs
    best = current
    activated = False
    for t in range(1, HOLD_HORIZON_HOURS + 1):
        sp = log_a[T+t] - coint["alpha"] - coint["beta"] * log_b[T+t]
        cur = current + (sp - entry_spread)
        if is_long and cur > best: best = cur
        if not is_long and cur < best: best = cur
        if is_long and cur <= stop: return {"pnl_pct": (cur-current)*100, "reason": "stop", "hours": t}
        if not is_long and cur >= stop: return {"pnl_pct": (current-cur)*100, "reason": "stop", "hours": t}
        if not activated:
            if is_long and cur >= activation: activated = True
            if not is_long and cur <= activation: activated = True
        if activated:
            if is_long and cur < best - trail_dist:
                return {"pnl_pct": (cur-current)*100, "reason": "trail", "hours": t}
            if not is_long and cur > best + trail_dist:
                return {"pnl_pct": (current-cur)*100, "reason": "trail", "hours": t}
    sp_f = log_a[T+HOLD_HORIZON_HOURS] - coint["alpha"] - coint["beta"] * log_b[T+HOLD_HORIZON_HOURS]
    cur = current + (sp_f - entry_spread)
    return {"pnl_pct": ((cur-current) if is_long else (current-cur))*100,
            "reason": "time_stop", "hours": HOLD_HORIZON_HOURS}


def simulate_cross(log_a, log_b, T, coint, current, is_long, cross_frac):
    """Exit only when spread reaches +/- cross_frac * |entry| on the OPPOSITE side."""
    stop = STOP_MULT * current
    entry_spread = log_a[T] - coint["alpha"] - coint["beta"] * log_b[T]
    entry_abs = abs(current)
    # Exit target on opposite side
    if is_long:
        # Entered when current < 0. Exit when current >= +cross_frac*|entry|
        exit_level = cross_frac * entry_abs
    else:
        exit_level = -cross_frac * entry_abs
    for t in range(1, HOLD_HORIZON_HOURS + 1):
        sp = log_a[T+t] - coint["alpha"] - coint["beta"] * log_b[T+t]
        cur = current + (sp - entry_spread)
        if is_long and cur <= stop: return {"pnl_pct": (cur-current)*100, "reason": "stop", "hours": t}
        if not is_long and cur >= stop: return {"pnl_pct": (current-cur)*100, "reason": "stop", "hours": t}
        if is_long and cur >= exit_level:
            return {"pnl_pct": (cur-current)*100, "reason": "cross", "hours": t}
        if not is_long and cur <= exit_level:
            return {"pnl_pct": (current-cur)*100, "reason": "cross", "hours": t}
    sp_f = log_a[T+HOLD_HORIZON_HOURS] - coint["alpha"] - coint["beta"] * log_b[T+HOLD_HORIZON_HOURS]
    cur = current + (sp_f - entry_spread)
    return {"pnl_pct": ((cur-current) if is_long else (current-cur))*100,
            "reason": "time_stop", "hours": HOLD_HORIZON_HOURS}


def run_pair(coin_a, coin_b):
    pair = f"{coin_a}/{coin_b}"
    print(f"--- {pair} ---", flush=True)
    try:
        ts, ca, cb = aligned_close_prices(coin_a, coin_b, interval=INTERVAL,
                                          lookback_hours=LOOKBACK_HOURS)
    except Exception as e:
        print(f"  fetch failed: {e}", flush=True)
        return []

    if len(ts) < FIT_WINDOW_HOURS + TEST_WINDOW_HOURS + HOLD_HORIZON_HOURS:
        print(f"  insufficient: {len(ts)} bars", flush=True)
        return []

    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    n = len(log_a)
    test_end = n - HOLD_HORIZON_HOURS
    test_start = test_end - TEST_WINDOW_HOURS

    trades = []
    iters = 0
    for T in range(test_start, test_end, 48):
        iters += 1
        if iters % 10 == 0: gc.collect()
        fit_start = T - FIT_WINDOW_HOURS
        if fit_start < 0: continue

        coint = fit_coint_map(log_a[fit_start:T], log_b[fit_start:T])
        if coint is None: continue

        spread_fit = log_a[fit_start:T] - coint["alpha"] - coint["beta"] * log_b[fit_start:T]
        spread_c = spread_fit - spread_fit.mean()
        sd = float(spread_c.std())
        if sd < 1e-9: continue
        current = float(spread_c[-1])
        z = current / sd
        if abs(z) < Z_ENTRY_MIN: continue

        is_long = current < 0

        # Strategy A
        a_out = simulate_A_current(log_a, log_b, T, coint, current, is_long)
        a_out.update({"pair": pair, "strategy": "A_current", "entry_z": z, "entry_idx": T})
        trades.append(a_out)

        # Strategy B
        b_out = simulate_cross(log_a, log_b, T, coint, current, is_long, B_CROSS_FRAC)
        b_out.update({"pair": pair, "strategy": "B_cross_0.75", "entry_z": z, "entry_idx": T})
        trades.append(b_out)

        # Strategy C
        c_out = simulate_cross(log_a, log_b, T, coint, current, is_long, C_CROSS_FRAC)
        c_out.update({"pair": pair, "strategy": "C_cross_1.00", "entry_z": z, "entry_idx": T})
        trades.append(c_out)

    print(f"  {len(trades)//3} entries → {len(trades)} strategy rows", flush=True)
    return trades


def main():
    all_t = []
    for i, (a, b) in enumerate(PAIRS):
        trades = run_pair(a, b)
        all_t.extend(trades)
        gc.collect()

    if not all_t:
        print("No trades", flush=True)
        return

    df = pd.DataFrame(all_t)
    df.to_csv(OUT / "exit_comparison.csv", index=False)
    print(f"\nSaved {len(df)} rows", flush=True)

    print()
    print("=" * 88)
    print("EXIT STRATEGY COMPARISON")
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
        return n, 100*(pnls>0).mean(), pnls.mean(), pnls.sum(), lo, med, hi, sub.hours.mean()

    print(f"{'strategy':>18}  {'n':>4}  {'win%':>6}  {'mean':>8}  {'total':>9}  {'CI low':>8}  {'median':>8}  {'avg_h':>7}")
    print("-" * 88)

    for label, key in [("A current", "A_current"), ("B cross 0.75", "B_cross_0.75"), ("C cross 1.00", "C_cross_1.00")]:
        s = stats(df[df.strategy == key])
        if s:
            n, win, mean, tot, lo, med, hi, avg_h = s
            print(f"{label:>18}  {n:>4}  {win:>5.1f}  {mean:>+7.3f}  {tot:>+8.2f}  {lo:>+7.3f}  {med:>+7.3f}  {avg_h:>6.1f}")

    print()
    print("Exit reason breakdown:")
    print(df.groupby(["strategy", "reason"]).size().unstack(fill_value=0))
    print()

    # Improvement calc
    base = df[df.strategy == "A_current"].pnl_pct.values
    for name, key in [("B", "B_cross_0.75"), ("C", "C_cross_1.00")]:
        sub = df[df.strategy == key].pnl_pct.values
        if len(sub) >= 3 and len(base) >= 3:
            imp = (sub.mean() - base.mean()) / abs(base.mean()) * 100
            print(f"{name} vs A: mean PnL improvement {imp:+.1f}%, trade count {len(sub)} vs {len(base)}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\nCRASH", flush=True)
        traceback.print_exc()
        sys.exit(1)
