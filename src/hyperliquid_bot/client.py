from __future__ import annotations

import logging
from typing import Any

from hyperliquid_bot.types import BotConfig, Network, RiskLimits

logger = logging.getLogger(__name__)


class HyperliquidClient:
    """
    Thin wrapper around Hyperliquid SDK for info + exchange operations.
    Uses testnet by default; set HL_PRIVATE_KEY in environment for live trading.
    """

    MAINNET_URL = "https://api.hyperliquid.xyz"
    TESTNET_URL = "https://api.hyperliquid-testnet.xyz"

    def __init__(self, config: BotConfig, private_key: str | None = None) -> None:
        self.config = config
        self.private_key = private_key
        self.base_url = (
            self.TESTNET_URL if config.network == Network.TESTNET else self.MAINNET_URL
        )
        self._info = None
        self._exchange = None

    def _ensure_clients(self) -> None:
        if self._info is not None:
            return
        try:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants

            url = (
                constants.TESTNET_API_URL
                if self.config.network == Network.TESTNET
                else constants.MAINNET_API_URL
            )
            self._info = Info(url, skip_ws=True)
            if self.private_key:
                from eth_account import Account
                from hyperliquid.exchange import Exchange

                wallet = Account.from_key(self.private_key)
                self._exchange = Exchange(wallet, url)
        except ImportError as exc:
            logger.warning("Hyperliquid SDK not fully available: %s", exc)

    def get_mid_price(self, coin: str | None = None) -> float:
        coin = coin or self.config.coin
        self._ensure_clients()
        if not self._info:
            return 0.0
        meta = self._info.all_mids()
        return float(meta.get(coin, 0.0))

    def get_l2_book(self, coin: str | None = None) -> dict[str, Any]:
        coin = coin or self.config.coin
        self._ensure_clients()
        if not self._info:
            return {"bids": [], "asks": []}
        return self._info.l2_snapshot(coin)

    def get_candles(self, coin: str, interval: str = "1m", lookback: int = 100) -> list[dict]:
        self._ensure_clients()
        if not self._info:
            return []
        import time

        end = int(time.time() * 1000)
        start = end - lookback * 60_000
        return self._info.candles_snapshot(coin, interval, start, end)

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        self._ensure_clients()
        if not self._exchange:
            logger.info("Dry-run order: %s", order)
            return {"status": "dry_run", "order": order}
        return self._exchange.order(**order)

    def cancel_all(self, coin: str | None = None) -> None:
        coin = coin or self.config.coin
        self._ensure_clients()
        if self._exchange:
            self._exchange.cancel_all(coin)
