"""Fetch OHLCV candles from Hyperliquid's public API."""
from __future__ import annotations

import time
from dataclasses import dataclass

import requests

MAINNET_INFO_URL = "https://api.hyperliquid.xyz/info"
TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"
HL_INFO_URL = MAINNET_INFO_URL


def configure(network: str) -> None:
    """Select the info endpoint matching the executor network."""
    global HL_INFO_URL
    HL_INFO_URL = TESTNET_INFO_URL if network == "testnet" else MAINNET_INFO_URL


class DataQualityError(ValueError):
    pass


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


def interval_hours(interval: str) -> float:
    return _interval_to_ms(interval) / 3_600_000


def _post(payload: dict, retries: int = 4) -> list:
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            r = requests.post(HL_INFO_URL, json=payload, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = status is None or status == 429 or status >= 500
            if attempt == retries or not retryable:
                raise
            time.sleep(delay)
            delay *= 2
    return []


def _fetch_chunk(coin: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
    """Single request to Hyperliquid. Returns up to ~5000 candles."""
    raw = _post({
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    })
    candles = [
        Candle(t=int(c["t"]), o=float(c["o"]), h=float(c["h"]),
               l=float(c["l"]), c=float(c["c"]), v=float(c["v"]))
        for c in raw
    ]
    candles.sort(key=lambda x: x.t)
    return candles


def closed_only(candles: list[Candle], interval_ms: int,
                now_ms: int | None = None) -> list[Candle]:
    """Drop any bar whose period hasn't ended yet."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return [c for c in candles if c.t + interval_ms <= now_ms]


def fetch_candles(
    coin: str,
    interval: str = "1h",
    lookback_hours: int = 720,
) -> list[Candle]:
    """Fetch closed OHLCV candles, paginating to bypass the ~5000-bar API cap.

    Uses a conservative 4000-candle chunk to leave headroom. Sleeps briefly
    between chunks to avoid rate-limiting.
    """
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - lookback_hours * 3600 * 1000

    interval_ms = _interval_to_ms(interval)
    chunk_ms = 4000 * interval_ms

    all_candles: dict[int, Candle] = {}
    cursor = start_ms

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

    candles = sorted(all_candles.values(), key=lambda x: x.t)
    return closed_only(candles, interval_ms, now_ms=end_ms)


def check_series(ts: list[int], interval_ms: int, now_ms: int | None = None,
                 max_missing_frac: float = 0.02, max_gap_bars: int = 3,
                 max_staleness_bars: float = 2.0) -> None:
    """Raise DataQualityError if the aligned series is stale or gappy."""
    if len(ts) < 2:
        raise DataQualityError("fewer than 2 aligned bars")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    # A bar is stamped at its open; the newest closed bar ends at t + interval.
    staleness = (now_ms - (ts[-1] + interval_ms)) / interval_ms
    if staleness > max_staleness_bars:
        raise DataQualityError(f"last bar is {staleness:.1f} bars old")
    gaps = [(b - a) // interval_ms for a, b in zip(ts, ts[1:])]
    if max(gaps) > max_gap_bars:
        raise DataQualityError(f"gap of {max(gaps)} bars in aligned series")
    expected = (ts[-1] - ts[0]) // interval_ms + 1
    missing = 1 - len(ts) / expected
    if missing > max_missing_frac:
        raise DataQualityError(f"{missing:.1%} of bars missing")


def aligned_close_prices(
    coin_a: str,
    coin_b: str,
    interval: str = "1h",
    lookback_hours: int = 720,
    validate: bool = True,
) -> tuple[list[int], list[float], list[float]]:
    a = fetch_candles(coin_a, interval, lookback_hours)
    b = fetch_candles(coin_b, interval, lookback_hours)

    a_by_t = {c.t: c.c for c in a}
    b_by_t = {c.t: c.c for c in b}
    shared = sorted(set(a_by_t) & set(b_by_t))

    if validate:
        try:
            check_series(shared, _interval_to_ms(interval))
        except DataQualityError as e:
            raise DataQualityError(f"{coin_a}/{coin_b}: {e}") from None

    ts = shared
    ca = [a_by_t[t] for t in ts]
    cb = [b_by_t[t] for t in ts]
    return ts, ca, cb
