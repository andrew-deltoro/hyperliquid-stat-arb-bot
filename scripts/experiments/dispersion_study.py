#!/usr/bin/env python3
"""
Dispersion study: do large spread moves continue or revert?

Motivation: the RSR/TAO +$95 trade was really a one-leg momentum trade
(94% of PnL from long RSR, +42% in ~20h) that the mean-reversion bot caught
by accident. Before building any scanner for "setups like that", measure
whether the effect is real across the whole pair universe.

Method (no lookahead):
- For each configured pair, load 1h closes for both legs over the window.
- Walk the 4h decision grid. At each point t, fit beta by OLS on the
  trailing LOOKBACK bars of log A ~ log B (same lookback as the bot).
- Spread S = log A - beta*log B. Event move dS = S_t - S_{t-H}.
- z = dS / rolling_std(dS over the trailing window).
- Event when |z| >= threshold.
- Forward move fwd = S_{t+H} - S_t.
  Continuation: sign(fwd) == sign(dS)   (trend)
  Reversion:    sign(fwd) == -sign(dS)  (what the bot bets on)
- Net return per unit gross-leg notional uses the bot's cost model.

Prints the continuation vs reversion economics by horizon and threshold.

Run: .venv/bin/python scripts/experiments/dispersion_study.py
"""
from __future__ import annotations

import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
import yaml

REPO = Path(__file__).resolve().parents[2]
CFG = yaml.safe_load((REPO / "config" / "phase3-live.yaml").read_text())
API = CFG.get("api_url", "https://api.hyperliquid.xyz/info")
PAIRS = [tuple(p) for p in CFG["pairs"]]
LOOKBACK = 720          # bars for the beta fit / spread mean (bot uses 720h)
COST_FRAC = 2.0 * (0.00045 + 3e-4)   # round-trip fee+slippage, per unit notional

CYCLE_HOURS = [0, 4, 8, 12, 16, 20]
WINDOW_DAYS = 60


def candles(coin, start_ms, end_ms, interval="1h"):
    try:
        r = requests.post(
            API,
            json={"type": "candleSnapshot",
                  "req": {"coin": coin, "interval": interval,
                          "startTime": start_ms, "endTime": end_ms}},
            timeout=30,
        ).json()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {coin}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(r, list):
        return {}
    out = {}
    for bar in r:
        try:
            out[int(bar["t"])] = float(bar["c"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def load_prices(coins, start_ms, end_ms):
    data = {}
    for c in sorted(coins):
        data[c] = candles(c, start_ms, end_ms)
        time.sleep(0.1)
    return data


def cycle_times(start, end):
    t = start.replace(minute=0, second=0, microsecond=0)
    while t <= end:
        if t.hour in CYCLE_HOURS:
            yield t
        t += timedelta(hours=1)


def analyze(pair, px, horizon_bars, thresholds=(2.0, 2.5, 3.0)):
    a, b = pair
    ta, tb = px.get(a, {}), px.get(b, {})
    ts = sorted(set(ta) & set(tb))
    if len(ts) < LOOKBACK + horizon_bars * 2:
        return None
    idx = {t: i for i, t in enumerate(ts)}
    la = np.log(np.array([ta[t] for t in ts]))
    lb = np.log(np.array([tb[t] for t in ts]))
    times = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in ts]

    events = []
    for t in cycle_times(times[0], times[-1]):
        i = idx.get(int(t.timestamp() * 1000))
        if i is None or i < LOOKBACK or i + horizon_bars >= len(ts):
            continue
        # beta from trailing window (no lookahead)
        y = la[i - LOOKBACK:i]
        x = lb[i - LOOKBACK:i]
        xc = x - x.mean()
        beta = float((xc * (y - y.mean())).sum() / (xc * xc).sum())
        if not np.isfinite(beta) or beta <= 0:
            continue
        S = la - beta * lb
        d = S - np.roll(S, 1)          # 1-bar spread changes
        d[:LOOKBACK] = np.nan
        # event move over horizon
        dS = S[i] - S[i - horizon_bars]
        hist = d[i - LOOKBACK:i]
        sd = np.nanstd(hist) * math.sqrt(horizon_bars)
        if not np.isfinite(sd) or sd <= 0:
            continue
        z = dS / sd
        fwd = S[i + horizon_bars] - S[i]
        events.append((t, beta, z, dS, fwd))
    if not events:
        return None

    out = {}
    for thr in thresholds:
        sel = [e for e in events if abs(e[2]) >= thr]
        if not sel:
            out[thr] = None
            continue
        zs = np.array([e[2] for e in sel])
        fwd = np.array([e[4] for e in sel])
        dS = np.array([e[3] for e in sel])
        betas = np.array([e[1] for e in sel])
        # Continuation bets with fwd in the event direction; reversion bets
        # fwd against it. Per unit gross-leg notional the spread return maps
        # to PnL as ~ (rA - beta*rB)/2 ... use dS/2 * (relative to N).
        cont = np.sign(dS) * fwd * 0.5      # positive if spread continued
        rev = -np.sign(dS) * fwd * 0.5      # positive if spread reverted
        # costs: round trip on both legs, gross = (1+|beta|)/2 per unit N
        cost = COST_FRAC * (1.0 + betas) / 2.0
        out[thr] = {
            "n": len(sel),
            "cont_hit": float((cont > 0).mean()),
            "cont_mean_gross": float(cont.mean()),
            "cont_mean_net": float((cont - cost).mean()),
            "rev_mean_net": float((rev - cost).mean()),
        }
    return out


def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=WINDOW_DAYS)
    coins = sorted({c for p in PAIRS for c in p})
    print(f"Loading {len(coins)} coins, {WINDOW_DAYS}d 1h candles...")
    px = load_prices(coins, int(start.timestamp() * 1000), int(end.timestamp() * 1000))

    for horizon in (24, 48):
        print("\n" + "=" * 78)
        print(f"HORIZON = {horizon}h   (event move over {horizon}h, forward {horizon}h)")
        print("=" * 78)
        agg = {thr: {"n": 0, "cont": 0, "cnet": 0.0, "rnet": 0.0} for thr in (2.0, 2.5, 3.0)}
        for pair in PAIRS:
            r = analyze(pair, px, horizon)
            if not r:
                continue
            for thr, st in r.items():
                if st:
                    a = agg[thr]
                    a["n"] += st["n"]
                    a["cont"] += st["cont_hit"] * st["n"]
                    a["cnet"] += st["cont_mean_net"] * st["n"]
                    a["rnet"] += st["rev_mean_net"] * st["n"]
        print(f"{'|z|>=':>6} {'events':>7} {'cont_hit':>9} {'cont_net/tr':>12} {'rev_net/tr':>11}")
        for thr in (2.0, 2.5, 3.0):
            a = agg[thr]
            if not a["n"]:
                continue
            print(f"{thr:>6} {a['n']:>7} {a['cont']/a['n']*100:>8.1f}% "
                  f"{a['cnet']/a['n']:>+12.5f} {a['rnet']/a['n']:>+11.5f}")
    print("\n(Positive cont_net/tr = spread momentum pays; positive rev_net/tr = "
          "reversion pays, which is what the live bot bets on.)")


if __name__ == "__main__":
    main()
