from __future__ import annotations

import logging
from datetime import date
from typing import Any

from hyperliquid_bot.types import PositionSnapshot, RiskLimits

logger = logging.getLogger(__name__)


class RiskManager:
    """Centralized risk rails for live Hyperliquid trading."""

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self._daily_pnl: float = 0.0
        self._day: date = date.today()
        self._open_orders: int = 0

    def reset_if_new_day(self) -> None:
        today = date.today()
        if today != self._day:
            self._daily_pnl = 0.0
            self._day = today

    def record_pnl(self, delta: float) -> None:
        self.reset_if_new_day()
        self._daily_pnl += delta

    def can_trade(self, position: PositionSnapshot | None = None) -> tuple[bool, str]:
        self.reset_if_new_day()
        if self.limits.kill_switch:
            return False, "kill_switch_active"
        if self._daily_pnl <= -self.limits.max_daily_loss_usd:
            return False, "daily_loss_limit"
        if position:
            notional = abs(position.size * position.entry_price)
            if notional > self.limits.max_position_usd:
                return False, "max_position"
            if position.leverage > self.limits.max_leverage:
                return False, "max_leverage"
        return True, "ok"

    def status(self) -> dict[str, Any]:
        return {
            "daily_pnl": self._daily_pnl,
            "kill_switch": self.limits.kill_switch,
            "limits": {
                "max_position_usd": self.limits.max_position_usd,
                "max_daily_loss_usd": self.limits.max_daily_loss_usd,
                "max_leverage": self.limits.max_leverage,
            },
        }
