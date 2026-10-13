#!/usr/bin/env python3
"""
Can we detect a large move BEFORE it prints? The genuinely useful question.

Two candidate precursors, both computable in real time from data we already
fetch, tested across the full universe over 180d:

  1. COILING   : elevated volume (vol-z high) but QUIET price (|ret| small)
                 -> does a big |move| follow within the next H hours?
  2. FUNDING   : |funding| extreme at time t
                 -> does a big |move| follow?

For each precursor we compare P(big move within H) against the base rate.
A scanner is only worth building if the lift is large and stable; otherwise
the honest answer is "these events are not forecastable from our tape."

Run: .venv/bin/python scripts/experiments/pump_precursor_study.py
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
    "xyz:AMD", "xyz:GOLD", "xyz:SILVER",
]
WINDOW_DAYS = 180
VOLZ_WIN = 168
BIG_MOVE = 0.10          # what we are trying to predict
HORIZON = 6              # hours ahead
VOLZ_HI = 3.0            # coiling: volume elevated
QUIET_PRICE = 0.02       # coiling: but price still quiet


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


def funding(coin, s, e, tries=3):
    for k in range(tries):
        try:
            r = requests.post(API, json={"type": "fundingHistory",
                "coin": coin, "startTime": s, "endTime": e}, timeout=40).json()
            if isinstance(r, list):
                return {int(x["time"]): float(x["fundingRate"]) for x in r}
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.8 * (k + 1))
    return {}


def wilson(k, n):
    if n == 0:
        return (0.0, 0.0)
    z = 1.96
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=WINDOW_DAYS)
    s_ms, e_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    base_hits = base_n = 0
    coil_hits = coil_n = 0
    fund_hits = fund_n = 0
    fund_thr_vals = []
    usable = 0

    for coin in UNIVERSE:
        bars = sorted(candles(coin, s_ms, e_ms), key=lambda b: b["t"])
        time.sleep(0.2)
        if len(bars) < VOLZ_WIN + HORIZON + 20:
            continue
        usable += 1
        T = [int(b["t"]) for b in bars]
        C = np.array([float(b["c"]) for b in bars])
        V = np.array([float(b.get("v", 0)) for b in bars])
        ret = np.zeros_like(C)
        ret[1:] = C[1:] / C[:-1] - 1.0
        fr = funding(coin, s_ms, e_ms)
        time.sleep(0.15)

        for i in range(VOLZ_WIN, len(C) - HORIZON - 1):
            w = V[i - VOLZ_WIN:i]
            sd = w.std()
            if sd <= 0:
                continue
            fwd_max = 0.0
            for j in range(i + 1, i + HORIZON + 1):
                fwd_max = max(fwd_max, abs(C[j] / C[i] - 1.0))
            hit = fwd_max >= BIG_MOVE

            base_n += 1
            base_hits += hit

            vol_z = (V[i] - w.mean()) / sd
            if vol_z >= VOLZ_HI and abs(ret[i]) < QUIET_PRICE:
                coil_n += 1
                coil_hits += hit

            rate = fr.get(T[i] - (T[i] % 3_600_000))
            if rate is not None:
                fund_thr_vals.append(abs(rate))
                # threshold computed on the fly below; placeholder count
                if abs(rate) >= 0.001:   # >0.1%/h
                    fund_n += 1
                    fund_hits += hit

    print(f"usable coins: {usable}")
    print(f"goal: |move| >= {BIG_MOVE:.0%} within {HORIZON}h\n")
    b = base_hits / max(base_n, 1)
    lo, hi = wilson(base_hits, base_n)
    print(f"BASE RATE           n={base_n:>7}  P={b:.3%}  95%CI=[{lo:.3%},{hi:.3%}]")

    if coil_n:
        p = coil_hits / coil_n
        lo, hi = wilson(coil_hits, coil_n)
        print(f"COILING (volz>={VOLZ_HI}, quiet price)  n={coil_n:>7}  P={p:.3%}  "
              f"95%CI=[{lo:.3%},{hi:.3%}]  lift={p/max(b,1e-9):.2f}x")

    if fund_n:
        p = fund_hits / fund_n
        lo, hi = wilson(fund_hits, fund_n)
        print(f"FUNDING |rate|>=0.1%/h  n={fund_n:>7}  P={p:.3%}  "
              f"95%CI=[{lo:.3%},{hi:.3%}]  lift={p/max(b,1e-9):.2f}x")

    if fund_thr_vals:
        fv = np.array(fund_thr_vals)
        print(f"\nfunding |rate| distribution (per hour): "
              f"p50={np.percentile(fv,50)*100:.4f}%  p90={np.percentile(fv,90)*100:.4f}%  "
              f"p99={np.percentile(fv,99)*100:.4f}%  max={fv.max()*100:.3f}%")

    print("\nInterpretation: a scanner is only worth building if COILING or "
          "FUNDING lift is well above 1.0 with a CI that excludes it.")


if __name__ == "__main__":
    main()
