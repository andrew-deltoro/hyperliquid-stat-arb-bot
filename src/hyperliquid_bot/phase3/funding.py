"""Account funding payments and trading fees for a position's holding window.

Both are account-level cash flows that Hyperliquid reports by coin and time,
so they share the same window-query pattern. Fees are read back from the
exchange rather than assumed: the exchange tells us, per fill, whether it was
maker or taker and what it actually charged, which lets us measure realized
cost instead of modelling it.
"""
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


# ---------------- fee realization ----------------
#
# We model cost as taker-fee + a fixed slippage allowance. Both are guesses.
# The exchange reports the truth per fill: `fee` (quote units) and `crossed`
# (True = taker). Capturing both lets us replace the model with measurement
# and see maker/taker mix per leg, which is what decides whether any edge
# survives contact with costs.

FEE_BY_COIN_COLUMNS = ["pair", "closed_iso", "coin", "leg", "fees_usd",
                       "maker_fills", "taker_fills", "notional_usd",
                       "realized_fee_pct", "effective_fee_pct"]

# userFills returns a bounded recent window (~2000 rows). Truncation is only
# possible when we receive a full page; a short page is the complete history.
_FILLS_CAP = 2000


def fees_for_position(info, wallet: str, pos: dict) -> dict:
    """Realized fees for both legs across the position's holding window.

    Returns {"total": usd, "by_coin": {coin: usd}, "maker_fills": n,
             "taker_fills": n, "notional": usd, "source": str}.

    `notional` sums |px*sz| over the window's fills, so
    total/notional is the realized one-way fee rate for this trade — the
    measured counterpart to the configured fee_pct + slippage_bps model.
    """
    out = {"total": 0.0, "by_coin": {pos["coin_a"]: 0.0, pos["coin_b"]: 0.0},
           "maker_fills": 0, "taker_fills": 0, "notional": 0.0,
           "source": "unavailable"}
    if info is None or not wallet:
        return out

    start_iso = pos.get("entry_time_iso")
    end_iso = pos.get("exit_time_iso") or datetime.now(timezone.utc).isoformat()
    if not start_iso:
        return out
    start_ms, end_ms = _ms(start_iso), _ms(end_iso)
    want = {pos["coin_a"], pos["coin_b"]}

    # The userFills endpoint is a bounded recent window, so a long hold can
    # scroll out of it. Detect that via a full page rather than silently
    # reporting a partial fee total as if it were complete.
    oldest = None
    try:
        fills = info.user_fills(wallet)
    except Exception:
        out["source"] = "query_failed"
        return out
    if not fills:
        out["source"] = "no_fills"
        return out

    for f in fills:
        t = int(f.get("time", 0) or 0)
        if oldest is None or t < oldest:
            oldest = t
        if t < start_ms or t > end_ms:
            continue
        coin = f.get("coin")
        if coin not in want:
            continue
        fee = float(f.get("fee", 0.0) or 0.0)
        px, sz = float(f.get("px", 0.0) or 0.0), float(f.get("sz", 0.0) or 0.0)
        crossed = bool(f.get("crossed", False))
        out["by_coin"][coin] = out["by_coin"].get(coin, 0.0) + fee
        out["total"] += fee
        out["notional"] += abs(px * sz)
        if crossed:
            out["taker_fills"] += 1
        else:
            out["maker_fills"] += 1

    truncated = (
        len(fills) >= _FILLS_CAP
        and oldest is not None
        and oldest > start_ms
    )
    if truncated:
        # Window truncated: fills before `oldest` are unavailable.
        out["source"] = "partial_window"
    elif out["maker_fills"] == 0 and out["taker_fills"] == 0:
        out["source"] = "no_fills_in_window"
    else:
        out["source"] = "ok"
    return out


def log_fees(info, wallet: str, pos: dict, fees_path) -> dict:
    """Append per-leg realized fees to the fee journal. Never raises."""
    snap = fees_for_position(info, wallet, pos)
    try:
        from .journal import _append_row, _ensure_file
        notional = snap["notional"]
        rate = (snap["total"] / notional) if notional > 0 else 0.0
        for leg in ("a", "b"):
            coin = pos[f"coin_{leg}"]
            _append_row(fees_path, FEE_BY_COIN_COLUMNS, {
                "pair": pos["pair"],
                "closed_iso": pos.get("exit_time_iso", ""),
                "coin": coin,
                "leg": leg,
                "fees_usd": f"{snap['by_coin'].get(coin, 0.0):.6f}",
                "maker_fills": str(snap["maker_fills"]),
                "taker_fills": str(snap["taker_fills"]),
                "notional_usd": f"{notional:.2f}",
                "realized_fee_pct": f"{rate:.6f}",
                "effective_fee_pct": f"{rate:.6f}",
            })
    except Exception:
        pass
    return snap
