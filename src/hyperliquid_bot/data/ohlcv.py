"""Fetch OHLCV candles from Hyperliquid's public API."""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

HL_INFO_URL = "https://api.hyperliquid.xyz/info"


@dataclass
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float


def fetch_candles(
    coin: str,
    interval: str = "1h",
    lookback_hours: int = 720,
) -> list[Candle]:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - lookback_hours * 3600 * 1000

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    r = requests.post(HL_INFO_URL, json=payload, timeout=30)
    r.raise_for_status()
    raw = r.json()

    candles = [
        Candle(t=int(c["t"]), o=float(c["o"]), h=float(c["h"]),
               l=float(c["l"]), c=float(c["c"]), v=float(c["v"]))
        for c in raw
    ]
    candles.sort(key=lambda x: x.t)
    return candles


def aligned_close_prices(
    coin_a: str,
    coin_b: str,
    interval: str = "1h",
    lookback_hours: int = 720,
) -> tuple[list[int], list[float], list[float]]:
    a = fetch_candles(coin_a, interval, lookback_hours)
    b = fetch_candles(coin_b, interval, lookback_hours)

    a_by_t = {c.t: c.c for c in a}
    b_by_t = {c.t: c.c for c in b}
    shared = sorted(set(a_by_t) & set(b_by_t))

    ts = shared
    ca = [a_by_t[t] for t in ts]
    cb = [b_by_t[t] for t in ts]
    return ts, ca, cb