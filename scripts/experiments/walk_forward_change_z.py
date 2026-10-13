#!/usr/bin/env python3
"""
Walk-forward + sensitivity for the change-z spread-overshoot reversion edge.

Pre-registered question: after a spread overshoot of |change-z| >= k over
H=24h (change-z uses a trailing, no-lookahead beta), does the spread revert
over the next 24h, net of the bot's round-trip costs?

Reported:
- per-fold stability (5 folds over 180d) -- temporal consistency
- a single locked 50/50 scatter/test split -- out-of-sample once
- pooled non-overlapping events -- effective sample
Thresholds 1.5-3.0 are shown to expose sensitivity (forking paths), NOT to
pick the best one after the fact.

Run: .venv/bin/python scripts/experiments/walk_forward_change_z.py
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
LOOKBACK = 720
COST_FRAC = 2.0 * (0.00045 + 3e-4)
CYCLE_HOURS = [0, 4, 8, 12, 16, 20]
WINDOW_DAYS = 180
HORIZON = 24
THRESHOLDS = [1.5, 2.0, 2.5, 3.0]


def candles(coin, s, e):
    try:
        r = requests.post(API, json={"type": "candleSnapshot",
            "req": {"coin": coin, "interval": "1h", "startTime": s, "endTime": e}},
            timeout=40).json()
        return {int(b["t"]): float(b["c"]) for b in r} if isinstance(r, list) else {}
    except Exception as exc:  # noqa: BLE001
        print(f"! {coin}: {exc}", file=sys.stderr)
        return {}


def cycle_times(a, b):
    t = a.replace(minute=0, second=0, microsecond=0)
    while t <= b:
        if t.hour in CYCLE_HOURS:
            yield t
        t += timedelta(hours=1)


def pair_events(pair, px):
    a, b = pair
    ta, tb = px.get(a, {}), px.get(b, {})
    ts = sorted(set(ta) & set(tb))
    if len(ts) < LOOKBACK + HORIZON * 2:
        return []
    idx = {t: i for i, t in enumerate(ts)}
    la = np.log(np.array([ta[t] for t in ts]))
    lb = np.log(np.array([tb[t] for t in ts]))
    times = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in ts]
    out = []
    for t in cycle_times(times[0], times[-1]):
        i = idx.get(int(t.timestamp() * 1000))
        if i is None or i < LOOKBACK or i + HORIZON >= len(ts):
            continue
        y, x = la[i - LOOKBACK:i], lb[i - LOOKBACK:i]
        xc = x - x.mean()
        beta = float((xc * (y - y.mean())).sum() / (xc * xc).sum())
        if not np.isfinite(beta) or beta <= 0:
            continue
        S = la - beta * lb
        dS = S[i] - S[i - HORIZON]
        sd = np.std(np.diff(S[i - LOOKBACK:i])) * math.sqrt(HORIZON)
        if not np.isfinite(sd) or sd <= 0:
            continue
        fwd = S[i + HORIZON] - S[i]
        cost = COST_FRAC * (1.0 + beta) / 2.0
        rev = -np.sign(dS) * fwd * 0.5 - cost
        out.append((times[i], float(dS / sd), float(rev)))
    return out


def tstat(r):
    r = np.asarray(r, dtype=float)
    if len(r) < 2 or r.std(ddof=1) == 0:
        return float("nan")
    return float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r))))


def nonoverlap(ev, thr):
    sel = [(t, r) for t, z, r in ev if abs(z) >= thr]
    out, last = [], None
    for t, r in sel:
        if last is None or (t - last) >= timedelta(hours=HORIZON):
            out.append(r)
            last = t
    return out


def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=WINDOW_DAYS)
    coins = sorted({c for p in PAIRS for c in p})
    print(f"Fetching {len(coins)} coins x {WINDOW_DAYS}d 1h candles...")
    px = {c: candles(c, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
          for c in coins}
    time.sleep(0.2)

    per_pair = {p: pair_events(p, px) for p in PAIRS}
    all_ev = sorted((e for ev in per_pair.values() for e in ev), key=lambda e: e[0])
    if not all_ev:
        print("no events"); return
    print(f"valid decision points: {len(all_ev)}")

    t0, t1 = all_ev[0][0], all_ev[-1][0]
    span = (t1 - t0) / 5

    print("\nPer-fold stability (fwd 24h net reversion):")
    for fi in range(5):
        lo, hi = t0 + span * fi, t0 + span * (fi + 1)
        cells = []
        for th in THRESHOLDS:
            r = [x[2] for x in all_ev if lo <= x[0] < hi and abs(x[1]) >= th]
            cells.append(f"n={len(r):>4} m={np.mean(r):+.4f} t={tstat(r):+.1f}" if len(r) >= 2
                         else f"n={len(r):>4}")
        lo_d = lo.strftime("%m-%d")
        print(f"  fold {fi+1} ({lo_d})  " + " | ".join(cells))

    mid = t0 + (t1 - t0) / 2
    print("\nLocked 50/50 split (scatter < mid <= test):")
    for th in THRESHOLDS:
        sc = [x[2] for x in all_ev if x[0] < mid and abs(x[1]) >= th]
        te = [x[2] for x in all_ev if x[0] >= mid and abs(x[1]) >= th]
        scm = f"{np.mean(sc):+.4f}" if sc else "  --  "
        tem = f"{np.mean(te):+.4f}" if te else "  --  "
        print(f"  z>={th}: scatter n={len(sc):>4} mean={scm} | TEST n={len(te):>4} "
              f"mean={tem} t={tstat(te):+.2f}")

    print("\nPooled non-overlapping (full 180d):")
    for th in THRESHOLDS:
        pool = []
        for p in PAIRS:
            pool += nonoverlap(per_pair[p], th)
        if len(pool) < 2:
            continue
        print(f"  z>={th}: n={len(pool):>4} mean={np.mean(pool):+.5f} "
              f"median={np.median(pool):+.5f} t={tstat(pool):+.2f}")


if __name__ == "__main__":
    main()
