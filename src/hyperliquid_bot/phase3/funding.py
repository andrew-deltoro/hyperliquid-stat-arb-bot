"""Account funding payments attributed to a position's holding window."""
from __future__ import annotations

from datetime import datetime, timezone


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def funding_for_coins(info, wallet: str, coins: list[str],
                      start_iso: str, end_iso: str) -> dict[str, float]:
    """
    Sum USDC funding payments for each coin in [start, end].

    Sign convention matches Hyperliquid: positive usdc = received.
    Attribution is by coin and time window only — if another strategy held
    the same coin in the window, its funding is included too.
    """
    start_ms, end_ms = _ms(start_iso), _ms(end_iso)
    want = set(coins)
    out = {c: 0.0 for c in coins}
    try:
        hist = info.user_funding_history(wallet, start_ms, end_ms)
    except Exception:
        return out
    for row in hist or []:
        delta = row.get("delta") or {}
        if delta.get("type") != "funding":
            continue
        coin = delta.get("coin")
        if coin not in want:
            continue
        t = int(row.get("time", 0))
        if t < start_ms or t > end_ms:
            continue
        out[coin] = out.get(coin, 0.0) + float(delta.get("usdc", 0))
    return out


def position_funding_usd(info, wallet: str, pos: dict) -> float:
    """Total funding USDC for both legs while the position was open."""
    if info is None or not wallet:
        return 0.0
    start = pos.get("entry_time_iso")
    end = pos.get("exit_time_iso") or datetime.now(timezone.utc).isoformat()
    if not start:
        return 0.0
    by_coin = funding_for_coins(
        info, wallet, [pos["coin_a"], pos["coin_b"]], start, end,
    )
    return float(sum(by_coin.values()))
