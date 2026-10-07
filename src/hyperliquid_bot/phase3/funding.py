"""Account funding payments attributed to a position's holding window."""
from __future__ import annotations

from datetime import datetime, timezone

# HL userFunding pages are capped (~500). Walk startTime forward when full.
_PAGE = 500
_MAX_PAGES = 40


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _iter_user_funding(info, wallet: str, start_ms: int, end_ms: int):
    cursor = start_ms
    for _ in range(_MAX_PAGES):
        try:
            hist = info.user_funding_history(wallet, cursor, end_ms)
        except Exception:
            return
        if not hist:
            return
        for row in hist:
            yield row
        if len(hist) < _PAGE:
            return
        last_t = int(hist[-1].get("time", 0))
        if last_t <= cursor:
            return
        cursor = last_t + 1
        if cursor > end_ms:
            return


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
    for row in _iter_user_funding(info, wallet, start_ms, end_ms):
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
