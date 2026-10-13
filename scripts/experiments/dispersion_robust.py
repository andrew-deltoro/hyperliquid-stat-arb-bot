#!/usr/bin/env python3
"""
Robustness pass on the dispersion study: is the reversion edge real, or is it
one or two overshoot events (e.g. RSR/TAO) driving a small sample?

Adds: per-event distribution (mean vs median vs trimmed), a bootstrap CI,
a t-statistic, and the same stats with RSR/TAO removed.

Run: .venv/bin/python scripts/experiments/dispersion_robust.py
"""
from __future__ import annotations

import math
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
LOOKBACK = 720
COST_FRAC = 2.0 * (0.00045 + 3e-4)
CYCLE_HOURS = [0, 4, 8, 12, 16, 20]
WINDOW_DAYS = 60


def candles(coin, s, e):
    try:
        r = requests.post(API, json={"type": "candleSnapshot",
            "req": {"coin": coin, "interval": "1h", "startTime": s, "endTime": e}},
            timeout=30).json()
        return {int(b["t"]): float(b["c"]) for b in r} if isinstance(r, list) else {}
    except Exception:  # noqa: BLE001
        return {}


def cycle_times(a, b):
    t = a.replace(minute=0, second=0, microsecond=0)
    while t <= b:
        if t.hour in CYCLE_HOURS:
            yield t
        t += timedelta(hours=1)


def events_for(pair, px, horizon, thr):
    a, b = pair
    ta, tb = px.get(a, {}), px.get(b, {})
    ts = sorted(set(ta) & set(tb))
    if len(ts) < LOOKBACK + horizon * 2:
        return []
    idx = {t: i for i, t in enumerate(ts)}
    la = np.log(np.array([ta[t] for t in ts]))
    lb = np.log(np.array([tb[t] for t in ts]))
    times = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in ts]
    out = []
    for t in cycle_times(times[0], times[-1]):
        i = idx.get(int(t.timestamp() * 1000))
        if i is None or i < LOOKBACK or i + horizon >= len(ts):
            continue
        y, x = la[i - LOOKBACK:i], lb[i - LOOKBACK:i]
        xc = x - x.mean()
        beta = float((xc * (y - y.mean())).sum() / (xc * xc).sum())
        if not np.isfinite(beta) or beta <= 0:
            continue
        S = la - beta * lb
        d = np.diff(S, prepend=np.nan)
        dS = S[i] - S[i - horizon]
        sd = np.nanstd(d[i - LOOKBACK:i]) * math.sqrt(horizon)
        if not np.isfinite(sd) or sd <= 0:
            continue
        z = dS / sd
        if abs(z) < thr:
            continue
        fwd = S[i + horizon] - S[i]
        cost = COST_FRAC * (1.0 + beta) / 2.0
        rev = -np.sign(dS) * fwd * 0.5 - cost
        out.append((f"{a}/{b}", times[i].date().isoformat(), float(z), float(rev)))
    return out


def summ(rows, label):
    if not rows:
        print(f"{label}: no events")
        return
    r = np.array([x[3] for x in rows])
    n = len(r)
    mean, med = float(r.mean()), float(np.median(r))
    trim = float(np.mean(np.sort(r)[int(0.05 * n):n - int(0.05 * n)])) if n > 20 else mean
    t = mean / (r.std(ddof=1) / math.sqrt(n)) if n > 1 and r.std(ddof=1) > 0 else float("nan")
    rng = np.random.default_rng(0)
    boot = [float(rng.choice(r, n, replace=True).mean()) for _ in range(5000)]
    lo, hi = np.percentile(boot, [2.5, 97.5])
    print(f"{label:>22}: n={n:>4}  mean={mean:+.5f}  median={med:+.5f}  "
          f"5%trim={trim:+.5f}  t={t:+.2f}  boot95=[{lo:+.5f},{hi:+.5f}]")


def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=WINDOW_DAYS)
    coins = sorted({c for p in PAIRS for c in p})
    px = {c: candles(c, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
          for c in coins}
    time.sleep(0.2)

    for horizon, thr in ((24, 2.0), (24, 3.0), (48, 2.0)):
        rows = []
        for pair in PAIRS:
            rows += events_for(pair, px, horizon, thr)
        ex = [r for r in rows if r[0] != "RSR/TAO"]
        print("=" * 96)
        print(f"REVERSION net return/event  (horizon={horizon}h, |z|>={thr})")
        summ(rows, "all events")
        summ(ex, "excl. RSR/TAO")
        # contribution of the single worst/best event
        if rows:
            r = np.sort([x[3] for x in rows])
            print(f"{'':>22}  worst={r[0]:+.4f}  best={r[-1]:+.4f}  "
                  f"|max| share of |mean*n| = {abs(r[-1] or r[0])/max(1e-9,abs(np.mean(r)*len(r))):.2f}")
    print("\nInterpretation: if mean is positive but median/trimmed are ~0 or negative,")
    print("the edge is a few overshoot tails, not a broad reversion signal.")


if __name__ == "__main__":
    main()
