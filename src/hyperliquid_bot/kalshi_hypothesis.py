"""Score for the locked Kalshi 15-minute OFI hypothesis.

A contract contributes only when the first-minute book imbalance has a
sign and the Kalshi market has an official yes/no result. The result is
settlement minus the ask that was on the book at the decision, minus the
one-contract taker fee. A hit rate is not computed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def taker_fee(price: float, contracts: int = 1, multiplier: float = 1.0, rate: float = 0.07) -> float:
    """Kalshi quadratic taker fee, rounded up to the next cent.

    Matches the one-contract column of the July 2026 fee schedule:
    one contract at 50 cents is 2 cents, one contract at 10 cents is 1 cent.
    """
    if contracts < 1:
        raise ValueError("contracts must be positive")
    if not 0.0 < price < 1.0:
        raise ValueError("price must be inside (0, 1)")
    raw = multiplier * rate * contracts * price * (1.0 - price)
    return math.ceil(raw * 100.0 - 1e-9) / 100.0


def contract_pnl(side: str, price: float, result: str, fee: float) -> float:
    """Dollars per contract. Win pays 1. Loss pays 0. Fee is already positive."""
    if side not in ("yes", "no"):
        raise ValueError("side must be yes or no")
    if result not in ("yes", "no"):
        raise ValueError("result must be yes or no")
    win = side == result
    return (1.0 if win else 0.0) - price - fee


@dataclass(frozen=True)
class Scored:
    pnl: float
    close_time: str


def score_joined(decisions: list[dict], settlements: dict[str, dict], test_series: str) -> list[Scored]:
    """Join decision and settlement rows. Skip flats, other series, and misses."""
    out: list[Scored] = []
    for dec in decisions:
        if dec.get("type") != "decision":
            continue
        if dec.get("series") != test_series:
            continue
        if dec.get("side") not in ("yes", "no"):
            continue
        sett = settlements.get(dec.get("ticker"))
        if not sett or sett.get("result") not in ("yes", "no"):
            continue
        price = float(dec["price"])
        fee = taker_fee(
            price,
            contracts=int(dec.get("contracts", 1)),
            multiplier=float(dec.get("fee_multiplier", 1.0)),
            rate=float(dec.get("fee_rate", 0.07)),
        )
        out.append(Scored(
            pnl=contract_pnl(dec["side"], price, sett["result"], fee),
            close_time=str(dec["close_time"]),
        ))
    out.sort(key=lambda row: row.close_time)
    return out


def walk_forward(rows: list[Scored], minimum: int, scatter_frac: float = 0.70) -> dict:
    """Split by time. Refuse a verdict until `minimum` scored contracts exist."""
    if len(rows) < minimum:
        return {
            "ready": False,
            "scored": len(rows),
            "minimum": minimum,
            "remaining": minimum - len(rows),
        }
    cut = int(len(rows) * scatter_frac)
    # Keep at least one contract in the holdout.
    cut = min(max(cut, 1), len(rows) - 1)
    scatter = rows[:cut]
    holdout = rows[cut:]

    def mean(part: list[Scored]) -> float:
        return sum(row.pnl for row in part) / len(part)

    scatter_mean = mean(scatter)
    return {
        "ready": True,
        "scored": len(rows),
        "scatter_n": len(scatter),
        "holdout_n": len(holdout),
        "scatter_mean_pnl": scatter_mean,
        "holdout_mean_pnl": mean(holdout),
        "kill": scatter_mean <= 0.0,
    }
