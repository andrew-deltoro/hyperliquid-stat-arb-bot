"""
Lookback agreement filter experiment.

For each entry point, fit cointegration at 3 lookbacks (14d, 30d, 60d).
Compute z-score at each. Only enter when signs agree.

Compare: agreement-filtered mean PnL vs unfiltered.
"""
from __future__ import annotations
import sys
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
sys.stdout.reconfigure(line_buffering=True)

import numpy as np
import pandas as pd
import pymc as pm
import gc

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices

PAIRS = [
    ("SNX", "DYDX"), ("MEME", "TIA"), ("GMT", "MEME"),
    ("SNX", "TIA"), ("ORDI", "SOL"), ("SNX", "SUI"),
]

INTERVAL = "1h"
LOOKBACK_HOURS = 24 * 200
LOOKBACKS_HOURS = [24 * 14, 24 * 30, 24 * 60]
TEST_WINDOW_HOURS = 24 * 130
HOLD_HORIZON_HOURS = 24 * 5

Z_ENTRY_MIN = 1.0
TARGET_FRAC = 0.75
STOP_MULT = 3.0


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


def simulate_exit(log_a, log_b, T, coint, current, is_long):
    target = TARGET_FRAC * current
    stop = STOP_MULT * current
    entry_spread = log_a[T] - coint["alpha"] - coint["beta"] * log_b[T]
    for t in range(1, HOLD_HORIZON_HOURS + 1):
        sp_now = log_a[T + t] - coint["alpha"] - coint["beta"] * log_b[T + t]
        delta = sp_now - entry_spread
        cur = current + delta
        if is_long:
            if cur >= target: return {"pnl_pct": (cur - current) * 100, "reason": "target", "hours": t}
            if cur <= stop:   return {"pnl_pct": (cur - current) * 100, "reason": "stop",   "hours": t}
        else:
            if cur <= target: return {"pnl_pct": (current - cur) * 100, "reason": "target", "hours": t}
            if cur >= stop:   return {"pnl_pct": (current - cur) * 100, "reason": "stop",   "hours": t}
    sp_f = log_a[T + HOLD_HORIZON_HOURS] - coint["alpha"] - coint["beta"] * log_b[T + HOLD_HORIZON_HOURS]
    cur = current + (sp_f - entry_spread)
    return {"pnl_pct": ((cur - current) if is_long else (current - cur)) * 100,
            "reason": "time_stop", "hours": HOLD_HORIZON_HOURS}


def compute_z_per_lookback(log_a, log_b, T, lookbacks):
    """Return list of z-scores, one per lookback. None if any fit fails."""
    z_scores = []
    for lb in lookbacks:
        start = T - lb
        if start < 0:
            return None
        coint = fit_coint_map(log_a[start:T], log_b[start:T])
        if coint is None:
            return None
        spread = log_a[start:T] - coint["alpha"] - coint["beta"] * log_b[start:T]
        spread_c = spread - spread.mean()
        sd = float(spread_c.std())
        if sd < 1e-9:
            return None
        z = float(spread_c[-1] / sd)
        z_scores.append((coint, z))
    return z_scores


def run_pair(coin_a, coin_b):
    pair = f"{coin_a}/{coin_b}"
    print(f"--- {pair} ---", flush=True)
    try:
        ts, ca, cb = aligned_close_prices(coin_a, coin_b, interval=INTERVAL,
                                          lookback_hours=LOOKBACK_HOURS)
    except Exception:
        return []
    if len(ts) < LOOKBACKS_HOURS[-1] + TEST_WINDOW_HOURS + HOLD_HORIZON_HOURS:
        return []

    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    n = len(log_a)
    test_end = n - HOLD_HORIZON_HOURS
    test_start = test_end - TEST_WINDOW_HOURS

    trades = []
    for T in range(test_start, test_end, 48):
        if (T - test_start) % (48 * 5) == 0 and T > test_start:
            gc.collect()

        zs = compute_z_per_lookback(log_a, log_b, T, LOOKBACKS_HOURS)
        if zs is None:
            continue

        # Use medium (30d) coint for the trade
        coint_medium, z_medium = zs[1]
        if abs(z_medium) < Z_ENTRY_MIN:
            continue

        # Agreement: do all 3 z-scores have the same sign?
        signs = [np.sign(z) for _, z in zs]
        signs = [s for s in signs if s != 0]  # drop zeros
        if len(signs) < 3:
            continue
        agree = len(set(signs)) == 1

        # Compute spread and simulate using medium coint
        start = T - LOOKBACKS_HOURS[1]
        spread = log_a[start:T] - coint_medium["alpha"] - coint_medium["beta"] * log_b[start:T]
        spread_c = spread - spread.mean()
        current = float(spread_c[-1])
        is_long = current < 0

        out = simulate_exit(log_a, log_b, T, coint_medium, current, is_long)
        out.update({
            "pair": pair, "entry_z": z_medium, "entry_idx": T,
            "z_14d": float(zs[0][1]),
            "z_30d": float(zs[1][1]),
            "z_60d": float(zs[2][1]),
            "agree": agree,
        })
        trades.append(out)

    print(f"  {len(trades)} trades", flush=True)
    return trades


def main():
    all_t = []
    for a, b in PAIRS:
        all_t.extend(run_pair(a, b))

    df = pd.DataFrame(all_t)
    df.to_csv("/home/hlbot/hyperliquid-stat-arb-bot/reports/lookback_agreement_experiment.csv", index=False)

    print()
    print("=" * 82)
    print("LOOKBACK AGREEMENT FILTER EXPERIMENT")
    print("=" * 82)
    print(f"Total trades: {len(df)}")
    print()

    def stats(sub):
        if len(sub) < 3: return None
        pnls = sub.pnl_pct.values
        n = len(pnls)
        rng = np.random.default_rng(42)
        boot = np.array([(lambda s: s.mean()/s.std() if s.std()>0 else 0.0)(
            rng.choice(pnls, size=n, replace=True)) for _ in range(5000)])
        lo, med, hi = np.percentile(boot, [2.5, 50, 97.5])
        return n, 100*(pnls>0).mean(), pnls.mean(), pnls.sum(), lo, med, hi

    print(f"{'filter':>20}  {'n':>4}  {'win%':>6}  {'mean':>7}  {'total':>8}  {'CI low':>8}  {'median':>8}")
    print("-" * 76)

    for label, sub in [("unfiltered (all)", df), ("3-lookback agree", df[df.agree == True])]:
        s = stats(sub)
        if s:
            n, win, mean, tot, lo, med, hi = s
            print(f"{label:>20}  {n:>4}  {win:>5.1f}  {mean:>+6.3f}  {tot:>+7.2f}  {lo:>+7.3f}  {med:>+7.3f}")

    print()
    print(f"Agreement rate: {df.agree.mean()*100:.1f}% ({df.agree.sum()}/{len(df)})")
    print("Interpretation: if agree-filtered mean PnL is >15% better, ship the filter.")


if __name__ == "__main__":
    main()
