#!/usr/bin/env python3
"""
What preceded the RSR +49% hourly spike (2026-10-12 04:00 UTC)?

Looks for observable precursors that a scanner could have used:
hourly return, volume vs a rolling baseline, and funding rate.

Run: .venv/bin/python scripts/experiments/rsr_precursor.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import requests

API = "https://api.hyperliquid.xyz/info"
COIN = "RSR"
EVENT = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)


def candles(coin, s, e):
    r = requests.post(API, json={"type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1h", "startTime": s, "endTime": e}},
        timeout=40).json()
    return r if isinstance(r, list) else []


def funding(coin, s, e):
    r = requests.post(API, json={"type": "fundingHistory",
        "coin": coin, "startTime": s, "endTime": e}, timeout=40).json()
    return r if isinstance(r, list) else []


def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=60)
    bars = candles(COIN, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
    bars.sort(key=lambda b: b["t"])
    T = [datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc) for b in bars]
    C = np.array([float(b["c"]) for b in bars])
    H = np.array([float(b["h"]) for b in bars])
    V = np.array([float(b.get("v", 0)) for b in bars])
    ret = np.zeros_like(C)
    ret[1:] = C[1:] / C[:-1] - 1.0

    # rolling baseline (168h) for volume
    vol_z = np.full_like(V, np.nan)
    for i in range(168, len(V)):
        w = V[i - 168:i]
        s = w.std()
        if s > 0:
            vol_z[i] = (V[i] - w.mean()) / s

    print(f"=== {COIN}: largest hourly returns (last 60d) ===")
    order = np.argsort(ret)[::-1]
    for i in list(order[:6]) + list(order[-3:]):
        print(f"  {T[i]:%m-%d %H:%M}  ret={ret[i]*100:+7.2f}%  vol_z={vol_z[i]:+.2f}  vol={V[i]:,.0f}")

    ev_i = min(range(len(T)), key=lambda i: abs((T[i] - EVENT).total_seconds()))
    print(f"\n=== window around event ({COIN}, event row = {T[ev_i]:%m-%d %H:%M} UTC) ===")
    print(f"{'time':>14} {'close':>11} {'ret%':>7} {'high%':>7} {'vol':>14} {'vol_z':>7}")
    for i in range(ev_i - 12, min(len(T), ev_i + 5)):
        print(f"{T[i]:%m-%d %H:%M} {C[i]:>11.6f} {ret[i]*100:>+7.2f} "
              f"{(H[i]/C[i-1]-1)*100:>+7.2f} {V[i]:>14,.0f} {vol_z[i]:>+7.2f}")

    # funding
    f = funding(COIN, int((EVENT - timedelta(days=4)).timestamp() * 1000),
                int((EVENT + timedelta(days=2)).timestamp() * 1000))
    print(f"\n=== {COIN} funding around the event (hourly rate) ===")
    if f:
        f.sort(key=lambda x: int(x["time"]))
        for row in f:
            t = datetime.fromtimestamp(int(row["time"]) / 1000, tz=timezone.utc)
            if abs((t - EVENT).total_seconds()) < 2 * 86400:
                print(f"  {t:%m-%d %H:%M}  rate={float(row['fundingRate'])*100:+.4f}%/h  px={row.get('premium')}")
    else:
        print("  (no funding rows)")

    # how unusual was the 24h move leading in?
    j = ev_i - 24
    if j >= 0:
        lead = C[ev_i] / C[j] - 1
        print(f"\nRSR 24h move up to the event: {lead*100:+.1f}%  "
              f"(event hour itself: {ret[ev_i]*100:+.1f}%)")


if __name__ == "__main__":
    main()
