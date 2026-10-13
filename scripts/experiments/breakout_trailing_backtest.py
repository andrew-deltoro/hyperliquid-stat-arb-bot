#!/usr/bin/env python3
"""
Can we TRADE the breakout? Event study with a trailing stop, not a fixed hold.

The momentum_event_study used fixed horizons (t+1h/4h/24h), which is not how
anyone trades a breakout. Here we emulate the actual proposal:

  trigger : 1h |ret| >= MOVE and vol-z >= VOLZ  (same detector, real time)
  entry   : next bar close, in the shock direction
  exit    : trailing stop of TRAIL from the best price since entry,
            or TIME_STOP bars, whichever first
  cost    : round trip applied

Swept over MOVE x TRAIL. If breakouts are tradeable, some pocket should be
positive net of cost and survive on the test half. If nothing is, the honest
answer is no.

Run: .venv/bin/python scripts/experiments/breakout_trailing_backtest.py
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

API = "https://api.hyperliquid.xyz/info"
UNIVERSE = [
    "BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK", "ARB", "OP", "SUI", "APT",
    "INJ", "TIA", "SEI", "WLD", "AAVE", "COMP", "UNI", "MKR", "CRV", "SNX",
    "DYDX", "RSR", "UMA", "PYTH", "LTC", "ETC", "NEO", "MEME", "GMT", "ORDI",
    "TAO", "FET", "RENDER", "xyz:NVDA", "xyz:PLTR", "xyz:MU", "xyz:TSM",
    "xyz:AMD",
]
WINDOW_DAYS = 180
VOLZ_WIN = 168
COST = 2.0 * (0.00045 + 3e-4)
MOVES = [0.05, 0.10]
TRAILS = [0.05, 0.10, 0.20]
TIME_STOP = 48


def candles(coin, s, e, tries=4):
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
    return []


def tstat(r):
    r = np.asarray(r, float)
    if len(r) < 2 or r.std(ddof=1) == 0:
        return float("nan")
    return float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r))))


def trade(bars, i, direction, trail, volz_win=VOLZ_WIN):
    """Trailing-stop exit on the SAME asset. Returns net return or None."""
    C = [float(b["c"]) for b in bars]
    H = [float(b["h"]) for b in bars]
    L = [float(b["l"]) for b in bars]
    entry = C[i + 1]
    if entry <= 0:
        return None
    # track best price favourable to the position
    if direction > 0:
        best = H[i + 1]
        for j in range(i + 2, min(len(C), i + 1 + TIME_STOP)):
            if L[j] <= best * (1 - trail):
                return (best * (1 - trail)) / entry - 1 - COST
            best = max(best, H[j])
        return C[min(len(C) - 1, i + TIME_STOP)] / entry - 1 - COST
    best = L[i + 1]
    for j in range(i + 2, min(len(C), i + 1 + TIME_STOP)):
        if H[j] >= best * (1 + trail):
            return -(best * (1 + trail)) / entry + 1 - COST
        best = min(best, L[j])
    return -(C[min(len(C) - 1, i + TIME_STOP)]) / entry + 1 - COST


def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=WINDOW_DAYS)
    s_ms, e_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    # collect per (move, trail) trade lists with timestamps for the split
    series = {(m, t): [] for m in MOVES for t in TRAILS}
    mid_ms = s_ms + (e_ms - s_ms) / 2

    for coin in UNIVERSE:
        bars = sorted(candles(coin, s_ms, e_ms), key=lambda b: b["t"])
        time.sleep(0.2)
        if len(bars) < VOLZ_WIN + TIME_STOP + 5:
            continue
        C = np.array([float(b["c"]) for b in bars])
        V = np.array([float(b["v"]) for b in bars])
        ret = np.zeros_like(C)
        ret[1:] = C[1:] / C[:-1] - 1.0
        for i in range(VOLZ_WIN, len(C) - TIME_STOP - 2):
            w = V[i - VOLZ_WIN:i]
            sd = w.std()
            if sd <= 0:
                continue
            if (V[i] - w.mean()) / sd < 3.0:
                continue
            mv = abs(ret[i])
            for m in MOVES:
                if mv < m:
                    continue
                d = int(np.sign(ret[i]))
                for t in TRAILS:
                    r = trade(bars, i, d, t)
                    if r is not None:
                        series[(m, t)].append((bars[i]["t"], r))

    print(f"trailing-stop breakout emulation ({WINDOW_DAYS}d, cost {COST*100:.2f}%)\n")
    print(f"{'move':>6}{'trail':>7}{'n':>6}{'mean':>10}{'t':>7}"
          f"{'scatter':>10}{'test':>10}{'test_t':>8}")
    for m in MOVES:
        for t in TRAILS:
            ev = series[(m, t)]
            if len(ev) < 5:
                continue
            r = np.array([x[1] for x in ev])
            sc = np.array([x[1] for x in ev if x[0] < mid_ms])
            te = np.array([x[1] for x in ev if x[0] >= mid_ms])
            scm = f"{sc.mean():+.4f}" if len(sc) else "  --  "
            tem = f"{te.mean():+.4f}" if len(te) else "  --  "
            tt = f"{tstat(te):+.2f}" if len(te) > 1 else "  --  "
            print(f"{m:>6.0%}{t:>7.0%}{len(r):>6}{r.mean():>+10.4f}"
                  f"{tstat(r):>+7.2f}{scm:>10}{tem:>10}{tt:>8}")

    print("\nIf no row has a positive test-half mean with |t|>2, breakouts are "
          "not tradeable with this detector.")


if __name__ == "__main__":
    main()
