#!/usr/bin/env python3
"""
Event study: after a single-leg shock, does price CONTINUE or FADE?

Definition of the scan trigger (deliberately simple and repeatable):
  shock hour = |1h return| >= MOVE and volume z (vs trailing 168h) >= VOLZ

For every such hour we measure the *same leg's* forward return at
+1h, +4h, +24h, signed so that a positive number = continuation of the
shock direction. Run across the full pair-universe coin set over 180d.

This is the honest question behind "find more RSR trades": a live scanner
can only react after the shock prints, so we need to know whether reacting
pays. Reported per move-size bucket, plus the cost hurdle.

Run: .venv/bin/python scripts/experiments/momentum_event_study.py
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
# Universe = every coin the bot already touches, plus liquid majors for breadth.
COINS = sorted({c for p in PAIRS for c in p} | {
    "BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK", "ARB", "OP", "SUI", "APT",
    "INJ", "TIA", "SEI", "PEPE", "WIF", "ONDO", "ENA", "JUP", "PYTH", "WLD",
})
WINDOW_DAYS = 180
VOLZ_WIN = 168
MOVE_BUCKETS = [0.05, 0.10, 0.20]
VOLZ_MIN = 3.0
HORIZONS = [1, 4, 24]
ROUND_TRIP_COST = 2.0 * (0.00045 + 3e-4)   # per unit notional, taker+taker


def candles(coin, s, e, tries: int = 4):
    for k in range(tries):
        try:
            r = requests.post(API, json={"type": "candleSnapshot",
                "req": {"coin": coin, "interval": "1h", "startTime": s, "endTime": e}},
                timeout=40).json()
            if isinstance(r, list) and r:
                return r
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.8 * (k + 1))
    print(f"! {coin}: no data", file=sys.stderr)
    return []


def tstat(r):
    r = np.asarray(r, float)
    if len(r) < 2 or r.std(ddof=1) == 0:
        return float("nan")
    return float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r))))


def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=WINDOW_DAYS)
    s_ms, e_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    print(f"Fetching {len(COINS)} coins x {WINDOW_DAYS}d...")

    events = []  # (coin, time, signed_fwd_by_horizon, abs_move, volz)
    n_ok = 0
    for k, coin in enumerate(COINS):
        bars = sorted(candles(coin, s_ms, e_ms), key=lambda b: b["t"])
        time.sleep(0.25)
        if len(bars) < VOLZ_WIN + max(HORIZONS) + 10:
            print(f"  (skip {coin}: only {len(bars)} bars)", file=sys.stderr)
            continue
        n_ok += 1
        C = np.array([float(b["c"]) for b in bars])
        V = np.array([float(b.get("v", 0)) for b in bars])
        T = [datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc) for b in bars]
        ret = np.zeros_like(C)
        ret[1:] = C[1:] / C[:-1] - 1.0
        for i in range(VOLZ_WIN, len(C) - max(HORIZONS) - 1):
            w = V[i - VOLZ_WIN:i]
            sd = w.std()
            if sd <= 0:
                continue
            vz = (V[i] - w.mean()) / sd
            if vz < VOLZ_MIN or abs(ret[i]) < 0.05:
                continue
            sign = np.sign(ret[i])
            fwd = {}
            for h in HORIZONS:
                fwd[h] = sign * (C[i + h] / C[i] - 1.0)
            events.append((coin, T[i], fwd, abs(ret[i]), vz))

    events.sort(key=lambda e: e[1])
    print(f"coins usable: {n_ok}; shock events (|ret|>=10%, vol_z>={VOLZ_MIN}): {len(events)}")
    print(f"round-trip cost hurdle = {ROUND_TRIP_COST*100:.3f}% per event\n")

    if not events:
        return

    print("CONTINUATION of shock direction (gross %, + = price kept going):")
    hdr = "  bucket        n  " + "".join(f"{'t+%dh' % h:>22}" for h in HORIZONS)
    print(hdr)
    for lo in MOVE_BUCKETS:
        hi = 10.0 if lo == MOVE_BUCKETS[-1] else next(
            x for x in MOVE_BUCKETS if x > lo)
        sel = [e for e in events if lo <= e[3] < hi]
        if len(sel) < 3:
            continue
        cells = []
        for h in HORIZONS:
            r = [e[2][h] for e in sel]
            cells.append(f"{np.mean(r)*100:+6.2f}% t={tstat(r):+5.2f}")
        print(f"  {lo:.0%}-{hi if hi<1 else 1:.0%}".ljust(14)
              + f"{len(sel):>4}  " + "".join(f"{c:>22}" for c in cells))

    print("\nNET of round-trip cost (continuation):")
    for lo in MOVE_BUCKETS:
        hi = 10.0 if lo == MOVE_BUCKETS[-1] else next(
            x for x in MOVE_BUCKETS if x > lo)
        sel = [e for e in events if lo <= e[3] < hi]
        if len(sel) < 3:
            continue
        cells = []
        for h in HORIZONS:
            r = np.array([e[2][h] for e in sel]) - ROUND_TRIP_COST
            cells.append(f"{r.mean()*100:+6.2f}% t={tstat(r):+5.2f}")
        print(f"  {lo:.0%}-{hi if hi<1 else 1:.0%}".ljust(14)
              + f"{len(sel):>4}  " + "".join(f"{c:>22}" for c in cells))

    print("\nFADE of shock direction (net %, + = reverted, costs applied):")
    for lo in MOVE_BUCKETS:
        hi = 10.0 if lo == MOVE_BUCKETS[-1] else next(
            x for x in MOVE_BUCKETS if x > lo)
        sel = [e for e in events if lo <= e[3] < hi]
        if len(sel) < 3:
            continue
        cells = []
        for h in HORIZONS:
            r = -np.array([e[2][h] for e in sel]) - ROUND_TRIP_COST
            cells.append(f"{r.mean()*100:+6.2f}% t={tstat(r):+5.2f}")
        print(f"  {lo:.0%}-{hi if hi<1 else 1:.0%}".ljust(14)
              + f"{len(sel):>4}  " + "".join(f"{c:>22}" for c in cells))

    print("\nBiggest 10 shocks on the tape:")
    for coin, t, fwd, mv, vz in sorted(events, key=lambda e: -e[3])[:10]:
        print(f"  {coin:<8} {t:%Y-%m-%d %H:%M}  move={mv*100:+7.1f}%  vol_z={vz:+5.1f}  "
              + "  ".join(f"t+{h}h={fwd[h]*100:+6.1f}%" for h in HORIZONS))


if __name__ == "__main__":
    main()
