"""
Bounce vs Cross diagnostic v2 — single-pair mode.
Runs ONE pair per invocation. Saves incrementally. Called from a bash loop.
"""
from __future__ import annotations
import sys, json, gc
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
sys.stdout.reconfigure(line_buffering=True)

import numpy as np
import pandas as pd
import pymc as pm
from pathlib import Path

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices

INTERVAL = "1h"
LOOKBACK_HOURS = 24 * 200
FIT_WINDOW_HOURS = 24 * 30
TRACK_HOURS = 48
MIN_START_Z = 1.5
CONVERGE_THRESHOLD = 0.5
CROSS_THRESHOLD = -0.5
BOUNCE_THRESHOLD = 0.5


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


def process_pair(coin_a, coin_b, out_dir):
    print(f"--- {coin_a}/{coin_b} ---", flush=True)
    try:
        ts, ca, cb = aligned_close_prices(coin_a, coin_b, interval=INTERVAL,
                                          lookback_hours=LOOKBACK_HOURS)
    except Exception as e:
        print(f"  fetch failed: {e}", flush=True)
        return

    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    n = len(log_a)
    end = n - TRACK_HOURS

    events = []
    iters = 0
    for T in range(FIT_WINDOW_HOURS, end, 24):  # step=24h (was 12h)
        iters += 1
        if iters % 20 == 0:
            gc.collect()

        fit_start = T - FIT_WINDOW_HOURS
        coint = fit_coint_map(log_a[fit_start:T], log_b[fit_start:T])
        if coint is None:
            continue

        spread_fit = log_a[fit_start:T] - coint["alpha"] - coint["beta"] * log_b[fit_start:T]
        sd = float(spread_fit.std())
        if sd < 1e-9: continue

        # look back 24h for start z
        if T - 24 < fit_start: continue
        start_spread = log_a[T-24] - coint["alpha"] - coint["beta"] * log_b[T-24]
        z_start = (start_spread - spread_fit.mean()) / sd

        spread_T = log_a[T] - coint["alpha"] - coint["beta"] * log_b[T]
        z_T = (spread_T - spread_fit.mean()) / sd

        if abs(z_start) < MIN_START_Z: continue
        if abs(z_T) > CONVERGE_THRESHOLD: continue

        # forward track
        z_path = []
        for t in range(1, TRACK_HOURS + 1):
            sp = log_a[T + t] - coint["alpha"] - coint["beta"] * log_b[T + t]
            z = (sp - spread_fit.mean()) / sd
            z_path.append(z)
        z_path = np.array(z_path)

        start_sign = np.sign(z_start)
        z_rel = z_path * start_sign

        hit_cross = bool(np.any(z_rel < -CROSS_THRESHOLD))
        hit_bounce = bool(np.any(z_rel > BOUNCE_THRESHOLD))

        if hit_cross and not hit_bounce:
            outcome = "cross"
        elif hit_bounce and not hit_cross:
            outcome = "bounce"
        elif hit_cross and hit_bounce:
            fc = int(np.argmax(z_rel < -CROSS_THRESHOLD))
            fb = int(np.argmax(z_rel > BOUNCE_THRESHOLD))
            outcome = "cross" if fc < fb else "bounce"
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

    if events:
        out_path = out_dir / f"bounce_{coin_a.replace(chr(58), chr(95))}_{coin_b.replace(chr(58), chr(95))}.csv"
        pd.DataFrame(events).to_csv(out_path, index=False)
        print(f"  saved {out_path.name}", flush=True)
    else:
        print(f"  no events to save", flush=True)


if __name__ == "__main__":
    coin_a = sys.argv[1]
    coin_b = sys.argv[2]
    out_dir = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports")
    process_pair(coin_a, coin_b, out_dir)
