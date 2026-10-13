"""Temporary entry exclusions for unhealthy pairs and repeatedly losing assets."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .journal import TRADES_FILE
from .paths import STATE_DIR


PAIR_HEALTH_FILE = STATE_DIR / "pair_health.json"


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _leg_net_pnl(row: dict) -> tuple[tuple[str, float], tuple[str, float]]:
    """Allocate the pair's recorded net P&L to its two legs.

    Gross directional P&L is exact from journal prices/notionals. The pair's
    total cost/funding drag is allocated in proportion to entry leg notional,
    so the two attributed values sum exactly to recorded pair P&L.
    """
    coin_a, coin_b = row["pair"].split("/", 1)
    leg_a, leg_b = float(row["leg_a_usd"]), float(row["leg_b_usd"])
    sign_a = 1.0 if row["direction"] == "LONG_A_SHORT_B" else -1.0
    sign_b = -sign_a
    gross_a = leg_a * sign_a * (
        float(row["exit_price_a"]) / float(row["entry_price_a"]) - 1.0
    )
    gross_b = leg_b * sign_b * (
        float(row["exit_price_b"]) / float(row["entry_price_b"]) - 1.0
    )
    effective_drag = gross_a + gross_b - float(row["pnl_usd"])
    total = leg_a + leg_b
    return (
        (coin_a, gross_a - effective_drag * leg_a / total),
        (coin_b, gross_b - effective_drag * leg_b / total),
    )


def asset_exclusions(
    raw_cfg: dict,
    now: datetime | None = None,
    trades_file: Path | None = None,
) -> dict[str, str]:
    now = now or datetime.now(timezone.utc)
    policy = (raw_cfg.get("entry_exclusions", {}) or {})
    blocked: dict[str, str] = {}

    for coin, item in (policy.get("manual_assets", {}) or {}).items():
        until = _dt(str(item["until_utc"]))
        if now < until:
            blocked[coin] = (
                f"manual cooldown until {until.isoformat()}: "
                f"{item.get('reason', 'manual exclusion')}"
            )

    auto = policy.get("performance", {}) or {}
    if not auto.get("enabled", False):
        return blocked
    path = trades_file or TRADES_FILE
    if not path.exists():
        return blocked
    lookback = now - timedelta(days=float(auto.get("lookback_days", 30)))
    history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    with path.open() as handle:
        for row in csv.DictReader(handle):
            exited = _dt(row["exit_time_iso"])
            if exited < lookback:
                continue
            for coin, pnl in _leg_net_pnl(row):
                history[coin].append((exited, pnl))

    minimum = int(auto.get("minimum_closed_legs", 3))
    streak = int(auto.get("consecutive_losing_legs", 3))
    cooldown = timedelta(hours=float(auto.get("cooldown_hours", 168)))
    for coin, observations in history.items():
        observations.sort()
        if len(observations) < minimum:
            continue
        recent = observations[-streak:]
        if len(recent) == streak and all(pnl < 0 for _, pnl in recent):
            until = recent[-1][0] + cooldown
            if now < until:
                blocked.setdefault(
                    coin,
                    f"{streak} consecutive losing legs; cooldown until {until.isoformat()}",
                )
    return blocked


def pair_health_blocks(raw_cfg: dict, now: datetime | None = None) -> dict[str, str]:
    now = now or datetime.now(timezone.utc)
    policy = (raw_cfg.get("entry_exclusions", {}) or {}).get("pair_health", {}) or {}
    if not policy.get("enabled", False) or not PAIR_HEALTH_FILE.exists():
        return {}
    data = json.loads(PAIR_HEALTH_FILE.read_text())
    last_run = _dt(data["last_run"])
    if now - last_run > timedelta(hours=float(policy.get("maximum_age_hours", 192))):
        return {}
    blocked_statuses = set(policy.get("blocked_statuses", ["red"]))
    return {
        pair: f"pair health is {result['status']} as of {last_run.isoformat()}"
        for pair, result in data.get("pairs", {}).items()
        if result.get("status") in blocked_statuses
    }


def entry_block_reason(
    pair: str,
    assets: dict[str, str],
    unhealthy_pairs: dict[str, str],
) -> str | None:
    if pair in unhealthy_pairs:
        return unhealthy_pairs[pair]
    coin_a, coin_b = pair.split("/", 1)
    reasons = [f"{coin}: {assets[coin]}" for coin in (coin_a, coin_b) if coin in assets]
    return "; ".join(reasons) if reasons else None
