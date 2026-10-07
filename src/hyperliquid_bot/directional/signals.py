"""
Directional perp signal generation.

Signal (validated in v4 backtest, mean OOS Sharpe 0.85 on BTC+SOL+AVAX):
  LONG  when close > MA(200) AND 5d return > +1% AND volume > 1.5x avg(14d)
  SHORT when close < MA(200) AND 5d return < -1% AND volume > 1.5x avg(14d)

Position sizing: dollar risk per trade = 2% of equity, using 2xATR stop.
Leverage applied to notional independently of stop distance.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

import numpy as np
import requests


HL_URL = "https://api.hyperliquid.xyz/info"

# Signal parameters (matching v4 walk-forward winner)
TREND_MA_DAYS = 200
MOMENTUM_DAYS = 5
VOL_SPIKE_THRESH = 1.5
MOM_THRESH = 0.01          # +1% / -1%

# Sizing
ATR_WINDOW = 14
STOP_ATR_MULT = 2.0
TARGET_ATR_MULT = 4.0
RISK_PCT = 0.02            # 2% of equity at risk per trade
TIME_STOP_DAYS = 30


@dataclass
class Signal:
    coin: str
    ts: float
    close: float
    ma: float
    momentum: float
    vol_ratio: float
    atr: float
    trend: str               # "up" / "down" / "flat"
    action: str              # "OPEN_LONG" / "OPEN_SHORT" / "SKIP"
    direction: str           # "LONG" / "SHORT" / "NONE"
    stop_price: float
    target_price: float
    suggested_notional: float  # at 1x


def post(payload: dict, timeout: int = 20):
    try:
        r = requests.post(HL_URL, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def fetch_daily_candles(coin: str, days: int = 250) -> Optional[list]:
    import time
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    return post({"type": "candleSnapshot",
                 "req": {"coin": coin, "interval": "1d",
                         "startTime": start_ms, "endTime": end_ms}})


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
         window: int = 14) -> float:
    if len(closes) < window + 1:
        return float("nan")
    tr = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - closes[:-1]),
        np.abs(lows[1:] - closes[:-1]),
    ])
    return float(tr[-window:].mean())


def generate_signal(coin: str, equity: float,
                     leverage: float = 1.0) -> Signal | None:
    candles = fetch_daily_candles(coin, days=250)
    if not candles or len(candles) < TREND_MA_DAYS + 10:
        return None

    closes = np.array([float(c["c"]) for c in candles])
    highs = np.array([float(c["h"]) for c in candles])
    lows = np.array([float(c["l"]) for c in candles])
    volumes = np.array([float(c["v"]) for c in candles])

    if len(closes) < TREND_MA_DAYS + 20:
        return None

    ma = float(closes[-TREND_MA_DAYS:].mean())
    close = float(closes[-1])
    mom = float(closes[-1] / closes[-1 - MOMENTUM_DAYS] - 1)
    vol_avg = float(volumes[-15:-1].mean())
    vol_ratio = float(volumes[-1] / vol_avg) if vol_avg > 0 else 0.0
    a = _atr(highs, lows, closes, ATR_WINDOW)

    trend = "up" if close > ma else ("down" if close < ma else "flat")

    # Decision
    long_ok = (close > ma) and (mom > MOM_THRESH) and (vol_ratio > VOL_SPIKE_THRESH)
    short_ok = (close < ma) and (mom < -MOM_THRESH) and (vol_ratio > VOL_SPIKE_THRESH)

    if long_ok:
        direction = "LONG"
        stop_price = close - STOP_ATR_MULT * a
        target_price = close + TARGET_ATR_MULT * a
        action = "OPEN_LONG"
    elif short_ok:
        direction = "SHORT"
        stop_price = close + STOP_ATR_MULT * a
        target_price = close - TARGET_ATR_MULT * a
        action = "OPEN_SHORT"
    else:
        direction = "NONE"
        stop_price = 0.0
        target_price = 0.0
        action = "SKIP"

    # ATR-based sizing: dollar risk at stop = RISK_PCT * equity
    stop_frac = STOP_ATR_MULT * a / close if close > 0 else 1.0
    base_notional = (RISK_PCT * equity) / stop_frac if stop_frac > 0 else 0.0
    suggested_notional = base_notional * leverage

    import time
    return Signal(
        coin=coin, ts=time.time(), close=close, ma=ma,
        momentum=mom, vol_ratio=vol_ratio, atr=a,
        trend=trend, action=action, direction=direction,
        stop_price=stop_price, target_price=target_price,
        suggested_notional=suggested_notional,
    )
