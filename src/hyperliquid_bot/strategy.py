"""Statistical arbitrage pairs trading."""
from __future__ import annotations
from typing import Any
import numpy as np
from hyperliquid_bot.strategy_base import Strategy
from hyperliquid_bot.types import OrderIntent, Side, Signal

class StrategyImpl(Strategy):
    name = "stat-arb"
    description = "Cointegrated pair spread mean reversion"

    def on_tick(self, market: dict[str, Any]) -> Signal:
        pair_mid = float(self.config.params.get("pair_mid", market["mid"] * 0.04))
        ratio = market["mid"] / pair_mid if pair_mid else 1
        hist = self._state.setdefault("ratios", [])
        hist.append(ratio)
        if len(hist) > 100:
            hist.pop(0)
        if len(hist) < 30:
            return Signal("hold", 0.2, "collecting spread history")
        z = (ratio - np.mean(hist)) / (np.std(hist) + 1e-9)
        if z > 2:
            return Signal("short", 0.85, f"spread rich z={z:.2f}")
        if z < -2:
            return Signal("long", 0.85, f"spread cheap z={z:.2f}")
        return Signal("hold", 0.5, "spread fair")

    def build_orders(self, signal: Signal, market: dict[str, Any]) -> list[OrderIntent]:
        if signal.action == "long":
            return [OrderIntent(self.config.coin, Side.BUY, self.config.size, None, tag="arb_pair_long")]
        if signal.action == "short":
            return [OrderIntent(self.config.coin, Side.SELL, self.config.size, None, tag="arb_pair_short")]
        return []
