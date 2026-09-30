"""Funding rate query and scoring for Hyperliquid perps."""
from __future__ import annotations

import requests

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
_CACHE = {"data": None, "ts": 0}


def get_funding_rates() -> dict[str, float]:
    """Return {coin: hourly_funding_rate} for all perps."""
    import time as _time
    now = _time.time()
    if _CACHE["data"] is not None and now - _CACHE["ts"] < 30:
        return _CACHE["data"]

    r = requests.post(
        HL_INFO_URL,
        json={"type": "metaAndAssetCtxs"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    universe = data[0]["universe"]
    ctxs = data[1]

    rates: dict[str, float] = {}
    for u, c in zip(universe, ctxs):
        name = u.get("name")
        funding = c.get("funding")
        if name and funding is not None:
            try:
                rates[name] = float(funding)
            except (TypeError, ValueError):
                continue

    _CACHE["data"] = rates
    _CACHE["ts"] = now
    return rates


def expected_funding_pnl(
    coin_long: str,
    coin_short: str,
    notional_per_leg: float,
    hold_hours: float,
) -> float:
    """Expected funding PnL in USD over hold_hours.

    Long leg pays funding if rate positive, receives if negative.
    Short leg receives funding if rate positive, pays if negative.
    """
    rates = get_funding_rates()
    fr_long = rates.get(coin_long, 0.0)
    fr_short = rates.get(coin_short, 0.0)

    # Long pays fr_long per hour; short receives fr_short per hour
    long_cost = notional_per_leg * fr_long * hold_hours
    short_gain = notional_per_leg * fr_short * hold_hours
    return short_gain - long_cost
