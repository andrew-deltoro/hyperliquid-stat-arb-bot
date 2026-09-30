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


_INTERVAL_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}


def _interval_to_ms(interval: str) -> int:
    """Convert '1h', '15m', '1d' to milliseconds."""
    unit = interval[-1]
    value = int(interval[:-1])
    return value * _INTERVAL_MS[unit]


def _fetch_chunk(coin: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
    """Single request to Hyperliquid. Returns up to ~5000 candles."""
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


def fetch_candles(
    coin: str,
    interval: str = "1h",
    lookback_hours: int = 720,
) -> list[Candle]:
    """Fetch OHLCV candles, paginating to bypass the ~5000-bar API cap.

    Uses a conservative 4000-candle chunk to leave headroom. Sleeps briefly
    between chunks to avoid rate-limiting.
    """
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - lookback_hours * 3600 * 1000

    interval_ms = _interval_to_ms(interval)
    chunk_ms = 4000 * interval_ms

    all_candles: dict[int, Candle] = {}
    cursor = start_ms
    last_seen_t = 0

    while cursor < end_ms:
        chunk_end = min(cursor + chunk_ms, end_ms)
        batch = _fetch_chunk(coin, interval, cursor, chunk_end)

        if not batch:
            # No data in this window; advance cursor to chunk_end
            if chunk_end >= end_ms:
                break
            cursor = chunk_end
            time.sleep(0.1)
            continue

        new_count = 0
        for c in batch:
            if c.t not in all_candles:
                all_candles[c.t] = c
                new_count += 1

        last_t = batch[-1].t
        # Guard against infinite loop if API returns the same batch
        if last_t <= cursor and new_count == 0:
            break

        # Advance to just past the last candle we got
        cursor = last_t + interval_ms

        # If the API returned very few candles, we've likely hit the end
        if len(batch) < 10:
            break

        time.sleep(0.1)  # be polite to the API

    return sorted(all_candles.values(), key=lambda x: x.t)


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