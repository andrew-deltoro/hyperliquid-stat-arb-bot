"""
Bounce vs Cross diagnostic.

For each pair, find every "convergence event" (z crosses from >|1.5| to <|0.5|).
Track 48h forward. Classify as bounce, cross, or stall.

An asymmetry in these outcomes informs exit strategy.
"""
from __future__ import annotations
import sys, time, json
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
    ("xyz:MU", "xyz:TSM"),
]

INTERVAL = "1h"
LOOKBACK_HOURS = 24 * 200
FIT_WINDOW_HOURS = 24 * 30
TRACK_HOURS = 48          # forward window after convergence
MIN_START_Z = 1.5
CONVERGE_THRESHOLD = 0.5
CROSS_THRESHOLD = -0.5    # must go below this to count as "crossed"
BOUNCE_THRESHOLD = 0.5    # must go above this to count as "bounced"


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


def detect_events(coin_a, coin_b):
    print(f"--- {coin_a}/{coin_b} ---", flush=True)
    try:
        ts, ca, cb = aligned_close_prices(coin_a, coin_b, interval=INTERVAL,
                                          lookback_hours=LOOKBACK_HOURS)
    except Exception as e:
        print(f"  fetch failed: {e}", flush=True)
        return []

    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    n = len(log_a)
    end = n - TRACK_HOURS

    events = []
    iter_count = 0
    for T in range(FIT_WINDOW_HOURS, end, 12):   # scan every 12h
        iter_count += 1
        if iter_count % 20 == 0:
            gc.collect()

        fit_start = T - FIT_WINDOW_HOURS
        coint = fit_coint_map(log_a[fit_start:T], log_b[fit_start:T])
        if coint is None:
            continue

        # Compute z at time T
        spread_fit = log_a[fit_start:T] - coint["alpha"] - coint["beta"] * log_b[fit_start:T]
        sd = float(spread_fit.std())
        if sd < 1e-9: continue

        spread_T = log_a[T] - coint["alpha"] - coint["beta"] * log_b[T]
        z_T = (spread_T - spread_fit.mean()) / sd

        # We need to look for a "start" event: z crossed |1.5| recently and now < |0.5|
        # Look back 24h
        look_back = 24
        if T - look_back < fit_start: continue

        start_spread = log_a[T-look_back] - coint["alpha"] - coint["beta"] * log_b[T-look_back]
        z_start = (start_spread - spread_fit.mean()) / sd

        # Convergence event: started |z| > 1.5, now |z| < 0.5
        if abs(z_start) < MIN_START_Z: continue
        if abs(z_T) > CONVERGE_THRESHOLD: continue

        # Track forward TRACK_HOURS
        z_path = []
        for t in range(1, TRACK_HOURS + 1):
            sp = log_a[T + t] - coint["alpha"] - coint["beta"] * log_b[T + t]
            z = (sp - spread_fit.mean()) / sd
            z_path.append(z)
        z_path = np.array(z_path)

        # Classify
        start_sign = np.sign(z_start)
        # Convert z_path relative to start_sign
        z_relative = z_path * start_sign  # positive = same side as start, negative = crossed

        hit_cross = bool(np.any(z_relative < -CROSS_THRESHOLD))
        hit_bounce = bool(np.any(z_relative > BOUNCE_THRESHOLD))

        if hit_cross and not hit_bounce:
            outcome = "cross"
        elif hit_bounce and not hit_cross:
            outcome = "bounce"
        elif hit_cross and hit_bounce:
            # Whichever came first
            first_cross = int(np.argmax(z_relative < -CROSS_THRESHOLD))
            first_bounce = int(np.argmax(z_relative > BOUNCE_THRESHOLD))
            outcome = "cross" if first_cross < first_bounce else "bounce"
        else:
            outcome = "stall"

        events.append({
            "pair": f"{coin_a}/{coin_b}",
            "T": T,
            "z_start": float(z_start),
            "z_end": float(z_T),
            "outcome": outcome,
            "hours_to_min_z": int(np.argmin(np.abs(z_path))),
        })

    print(f"  {len(events)} convergence events detected", flush=True)
    return events


def main():
    all_events = []
    for a, b in PAIRS:
        ev = detect_events(a, b)
        all_events.extend(ev)
        gc.collect()

    if not all_events:
        print("No events found.")
        return

    df = pd.DataFrame(all_events)
    out = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports/bounce_vs_cross.csv")
    df.to_csv(out, index=False)

    print()
    print("=" * 82)
    print("BOUNCE vs CROSS DIAGNOSTIC")
    print("=" * 82)
    print(f"Total convergence events: {len(df)}")
    print()

    # Overall
    counts = df.outcome.value_counts()
    total = len(df)
    print("Overall outcome distribution:")
    for outcome in ["bounce", "cross", "stall"]:
        n = counts.get(outcome, 0)
        print(f"  {outcome:>8}: {n:>4} ({100*n/total:>5.1f}%)")
    print()

    # Per-pair
    print(f"{'pair':>20}  {'n':>4}  {'bounce%':>8}  {'cross%':>8}  {'stall%':>8}  {'median_h_to_0':>14}")
    print("-" * 74)
    for pair, group in df.groupby("pair"):
        n = len(group)
        b = (group.outcome == "bounce").mean() * 100
        c = (group.outcome == "cross").mean() * 100
        s = (group.outcome == "stall").mean() * 100
        med_h = group.hours_to_min_z.median()
        print(f"{pair:>20}  {n:>4}  {b:>7.1f}%  {c:>7.1f}%  {s:>7.1f}%  {med_h:>13.0f}h")

    print()
    print("Interpretation:")
    print("  bounce% > 60  → exit at target (or before). Don't chase past zero.")
    print("  cross%  > 60  → hold past target. Trail can run.")
    print("  stall%  > 50  → exit at target. Holding loses.")


if __name__ == "__main__":
    main()
