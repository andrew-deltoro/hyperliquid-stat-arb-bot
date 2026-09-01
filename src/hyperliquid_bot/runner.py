from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from hyperliquid_bot.client import HyperliquidClient
from hyperliquid_bot.risk import RiskManager
from hyperliquid_bot.strategy_base import Strategy
from hyperliquid_bot.types import BotConfig, Network, PositionSnapshot, RiskLimits

logger = logging.getLogger(__name__)


class BotRunner:
    """Async runner loop for any Hyperliquid strategy."""

    def __init__(
        self,
        strategy: Strategy,
        config: BotConfig | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        self.strategy = strategy
        self.config = config or strategy.config
        self.poll_interval = poll_interval
        self.client = HyperliquidClient(
            self.config, private_key=os.getenv("HL_PRIVATE_KEY")
        )
        self.risk = RiskManager(self.config.risk)
        self._running = False

    def _market_snapshot(self) -> dict[str, Any]:
        coin = self.config.coin
        mid = self.client.get_mid_price(coin)
        book = self.client.get_l2_book(coin)
        candles = self.client.get_candles(coin, interval="1m", lookback=60)
        return {"coin": coin, "mid": mid, "book": book, "candles": candles}

    async def tick_once(self) -> dict[str, Any]:
        market = self._market_snapshot()
        signal = self.strategy.on_tick(market)
        position = PositionSnapshot(
            coin=self.config.coin, size=0.0, entry_price=market["mid"],
            unrealized_pnl=0.0, leverage=1.0,
        )
        ok, reason = self.risk.can_trade(position)
        result: dict[str, Any] = {
            "signal": signal.action,
            "confidence": signal.confidence,
            "reason": signal.reason,
            "risk": reason,
            "orders": [],
        }
        if not ok:
            return result
        if not self.strategy.validate_risk(position):
            result["risk"] = "strategy_risk_block"
            return result
        orders = self.strategy.build_orders(signal, market)
        for order in orders:
            payload = {
                "coin": order.coin,
                "is_buy": order.side.value == "buy",
                "sz": order.size,
                "limit_px": order.price,
                "reduce_only": order.reduce_only,
            }
            resp = self.client.place_order(payload)
            result["orders"].append(resp)
        return result

    async def run(self) -> None:
        self._running = True
        logger.info("Starting %s on %s/%s", self.strategy.name, self.config.coin, self.config.network.value)
        while self._running:
            try:
                result = await self.tick_once()
                logger.debug("Tick: %s", result)
            except Exception:
                logger.exception("Tick failed")
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False


def default_config(bot_id: str, coin: str = "ETH", **params: Any) -> BotConfig:
    return BotConfig(
        bot_id=bot_id,
        coin=coin,
        network=Network(os.getenv("HL_NETWORK", "testnet")),
        risk=RiskLimits(
            max_position_usd=float(os.getenv("HL_MAX_POSITION_USD", "10000")),
            max_daily_loss_usd=float(os.getenv("HL_MAX_DAILY_LOSS_USD", "500")),
            max_leverage=float(os.getenv("HL_MAX_LEVERAGE", "5")),
        ),
        params=params,
    )
