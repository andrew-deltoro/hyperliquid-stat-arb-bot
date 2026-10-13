#!/usr/bin/env python3
"""
Is the change-z reversion edge stable across time windows, or was the
earlier "+1.08%, t=3.10" an artifact of a 60-day window?

Fetches 180d once, then reports the pooled NON-OVERLAPPING reversion mean/t
in rolling 60d windows and in expanding windows. A real edge should be
positive in most windows; a regime artifact will flip sign.

Run: .venv/bin/python scripts/experiments/change_z_window_sensitivity.py
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
import yaml
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CFG = yaml.safe_load((REPO / "config" / "phase3-live.yaml").read_text())
API = CFG.get("api_url", "https://api.hyperliquid.xyz/info")
PAIRS = [tuple(p) for p in CFG["pairs"]]
LOOKBACK = 720
COST_FRAC = 2.0 * (0.00045 + 3e-4)
CYCLE_HOURS = [0, 4, 8, 12, 16, 20]
HORIZON = 24
THR = 2.0


def candles(coin, s, e):
    try:
        r = requests.post(API, json={"type": "candleSnapshot",
            "req": {"coin": coin, "interval": "1h", "startTime": s, "endTime": e}},
            timeout=40).json()
        return {int(b["t"]): float(b["c"]) for b in r} if isinstance(r, list) else {}
    except Exception as exc:  # noqa: BLE001
        print(f"! {coin}: {exc}", file=sys.stderr); return {}


def tstat(r):
    r = np.asarray(r, float)
    if len(r) < 2 or r.std(ddof=1) == 0:
        return float("nan")
    return float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r))))


def events_for_pair(pair, px):
    a, b = pair
    ta, tb = px.get(a, {}), px.get(b, {})
    ts = sorted(set(ta) & set(tb))
    if len(ts) < LOOKBACK + HORIZON * 2:
        return []
    la = np.log(np.array([ta[t] for t in ts]))
    lb = np.log(np.array([tb[t] for t in ts]))
    times = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in ts]
    out, last = [], None
    t = times[0].replace(minute=0, second=0, microsecond=0)
    while t <= times[-1]:
        if t.hour not in CYCLE_HOURS:
            t += timedelta(hours=1); continue
        i = None
        lo, hi = 0, len(ts) - 1
        want = int(t.timestamp() * 1000)
        while lo <= hi:
            m = (lo + hi) // 2
            if ts[m] == want:
                i = m; break
            if ts[m] < want:
                lo = m + 1
            else:
                hi = m - 1
        if i is None or i < LOOKBACK or i + HORIZON >= len(ts):
            t += timedelta(hours=1); continue
        y, x = la[i - LOOKBACK:i], lb[i - LOOKBACK:i]
        xc = x - x.mean()
        beta = float((xc * (y - y.mean())).sum() / (xc * xc).sum())
        if not np.isfinite(beta) or beta <= 0:
            t += timedelta(hours=1); continue
        S = la - beta * lb
        dS = S[i] - S[i - HORIZON]
        sd = np.std(np.diff(S[i - LOOKBACK:i])) * math.sqrt(HORIZON)
        if not np.isfinite(sd) or sd <= 0 or abs(dS / sd) < THR:
            t += timedelta(hours=1); continue
        if last is not None and (t - last) < timedelta(hours=HORIZON):
            t += timedelta(hours=1); continue
        fwd = S[i + HORIZON] - S[i]
        cost = COST_FRAC * (1.0 + beta) / 2.0
        out.append((t, -np.sign(dS) * fwd * 0.5 - cost))
        last = t
        t += timedelta(hours=1)
    return out


def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=180)
    coins = sorted({c for p in PAIRS for c in p})
    print("Fetching 180d...")
    px = {c: candles(c, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
          for c in coins}
    ev = sorted((e for p in PAIRS for e in events_for_pair(p, px)), key=lambda e: e[0])
    print(f"non-overlapping change-z>=2 events (180d): {len(ev)}\n")

    t0, t1 = ev[0][0], ev[-1][0]
    print("Rolling 60d windows (non-overlapping z>=2):")
    for k in range(0, 4):
        lo = t0 + timedelta(days=60 * k)
        hi = lo + timedelta(days=60)
        r = [x[1] for x in ev if lo <= x[0] < hi]
        tag = "OK " if len(r) >= 2 and np.mean(r) > 0 else "!! "
        m = f"{np.mean(r):+.5f}" if r else "  --   "
        print(f"  {tag}{lo:%Y-%m-%d}..{hi:%m-%d}  n={len(r):>3}  mean={m}  t={tstat(r):+.2f}")

    print("\nExpanding windows ending today:")
    for days in (30, 60, 90, 120, 150, 180):
        lo = t1 - timedelta(days=days)
        r = [x[1] for x in ev if x[0] >= lo]
        print(f"  last {days:>3}d  n={len(r):>3}  mean={np.mean(r):+.5f}  t={tstat(r):+.2f}")

    pos = sum(1 for k in range(0, 4)
              if (rr := [x[1] for x in ev if t0 + timedelta(days=60*k) <= x[0]
                         < t0 + timedelta(days=60*(k+1))]) and np.mean(rr) > 0)
    print(f"\n=> {pos}/4 rolling 60d windows have positive mean.")


if __name__ == "__main__":
    main()
