#!/usr/bin/env python3
"""
Non-overlapping check for the dispersion study.

The 4h decision grid makes consecutive events' forward windows overlap, which
inflates the effective sample and the t-statistic. Here events are selected
greedily per pair with at least `horizon` hours between them, so forward
windows never overlap.

Run: .venv/bin/python scripts/experiments/dispersion_nonoverlap.py
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


def pair_events(pair, px, horizon, thr):
    a, b = pair
    ta, tb = px.get(a, {}), px.get(b, {})
    ts = sorted(set(ta) & set(tb))
    if len(ts) < LOOKBACK + horizon * 2:
        return []
    idx = {t: i for i, t in enumerate(ts)}
    la = np.log(np.array([ta[t] for t in ts]))
    lb = np.log(np.array([tb[t] for t in ts]))
    times = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in ts]
    cand = []
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
        cand.append((t, -np.sign(dS) * fwd * 0.5 - cost))
    # Greedy non-overlapping selection: >= horizon hours apart.
    out, last = [], None
    for t, r in cand:
        if last is None or (t - last) >= timedelta(hours=horizon):
            out.append(r)
            last = t
    return out, len(cand)


def summ(r, label):
    r = np.array(r)
    if len(r) < 2:
        print(f"{label:>22}: n={len(r)} (too few)")
        return
    mean = float(r.mean())
    t = mean / (r.std(ddof=1) / math.sqrt(len(r)))
    rng = np.random.default_rng(0)
    boot = [float(rng.choice(r, len(r), replace=True).mean()) for _ in range(5000)]
    lo, hi = np.percentile(boot, [2.5, 97.5])
    print(f"{label:>22}: n={len(r):>3}  mean={mean:+.5f}  median={float(np.median(r)):+.5f}  "
          f"t={t:+.2f}  boot95=[{lo:+.5f},{hi:+.5f}]")


def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=WINDOW_DAYS)
    coins = sorted({c for p in PAIRS for c in p})
    px = {c: candles(c, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
          for c in coins}
    time.sleep(0.2)

    for horizon, thr in ((24, 2.0), (24, 2.5), (24, 3.0), (48, 2.0)):
        all_r, all_n, ex_r = [], 0, []
        for pair in PAIRS:
            rr, n = pair_events(pair, px, horizon, thr)
            all_r += rr
            all_n += n
            if pair != ("RSR", "TAO"):
                ex_r += rr
        print("=" * 92)
        print(f"REVERSION net/event, NON-OVERLAPPING (horizon={horizon}h, |z|>={thr}) "
              f"[{all_n} raw -> {len(all_r)} non-overlap]")
        summ(all_r, "all events")
        summ(ex_r, "excl. RSR/TAO")


if __name__ == "__main__":
    main()
