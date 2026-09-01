from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from hyperliquid_bot.types import BotConfig, OrderIntent, PositionSnapshot, Signal

logger = logging.getLogger(__name__)


class Strategy(ABC):
    """Base class for all Hyperliquid trading strategies."""

    name: str = "base"
    description: str = ""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._state: dict[str, Any] = {}

    @abstractmethod
    def on_tick(self, market: dict[str, Any]) -> Signal:
        """Evaluate market snapshot and return trading signal."""

    @abstractmethod
    def build_orders(self, signal: Signal, market: dict[str, Any]) -> list[OrderIntent]:
        """Translate signal into concrete order intents."""

    def on_fill(self, fill: dict[str, Any]) -> None:
        """Optional hook when an order fills."""

    def validate_risk(self, position: PositionSnapshot | None) -> bool:
        risk = self.config.risk
        if risk.kill_switch:
            logger.warning("Kill switch active — blocking new orders")
            return False
        if position and abs(position.size * position.entry_price) > risk.max_position_usd:
            logger.warning("Max position exceeded")
            return False
        return True

    def summary(self) -> dict[str, Any]:
        return {
            "strategy": self.name,
            "coin": self.config.coin,
            "network": self.config.network.value,
            "params": self.config.params,
        }
