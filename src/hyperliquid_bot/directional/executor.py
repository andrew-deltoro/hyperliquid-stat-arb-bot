"""
Minimal HL executor for single-leg perp orders.

Reuses the live SDK pattern from the pairs executor but is stripped down:
  - One leg at a time (no rollback logic)
  - Market orders with slippage tolerance
  - Optional stop-loss trigger order on the exchange
"""
from __future__ import annotations
import os
from datetime import datetime, timezone

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants


class DirectionalExecutor:
    def __init__(self, cfg, log_fn=print):
        self.cfg = cfg
        self.log_fn = log_fn

        ex_cfg = cfg.raw.get("executor", {})
        network = ex_cfg.get("network", "mainnet")
        self.base_url = (constants.MAINNET_API_URL if network == "mainnet"
                         else constants.TESTNET_API_URL)

        pk = os.getenv(ex_cfg.get("private_key_env", "HL_PRIVATE_KEY_MAINNET"))
        wallet = os.getenv(ex_cfg.get("account_address_env",
                                       "HL_ACCOUNT_ADDRESS_MAINNET"))
        if not pk or not wallet:
            raise RuntimeError("Missing HL credentials")
        self.wallet = wallet
        account = Account.from_key(pk)
        self.exchange = Exchange(account, self.base_url,
                                 account_address=wallet, perp_dexs=[""])
        self.info = Info(self.base_url, skip_ws=True, perp_dexs=[""])
        self.slippage = float(ex_cfg.get("slippage_tolerance", 0.01))

        self.log_fn(f"[DIR-EXECUTOR] network={network} "
                    f"account={wallet[:10]}...")

    def _round_sz(self, coin: str, sz: float) -> float:
        meta = self.info.meta()
        for a in meta.get("universe", []):
            if a["name"] == coin:
                dec = int(a.get("szDecimals", 2))
                return round(sz, dec)
        return round(sz, 2)

    def market_open(self, coin: str, is_buy: bool, size: float,
                    ref_px: float) -> dict:
        size = self._round_sz(coin, size)
        if size <= 0:
            return {"status": "error", "error": "size rounds to zero"}
        try:
            r = self.exchange.market_open(coin, is_buy, size,
                                           ref_px, self.slippage)
        except Exception as e:
            return {"status": "error", "error": str(e)}
        return self._parse(r)

    def market_close(self, coin: str, size: float, ref_px: float) -> dict:
        size = self._round_sz(coin, size)
        if size <= 0:
            return {"status": "error", "error": "size rounds to zero"}
        try:
            r = self.exchange.market_close(coin, size, None, ref_px,
                                            self.slippage)
        except Exception as e:
            return {"status": "error", "error": str(e)}
        return self._parse(r)

    @staticmethod
    def _parse(result) -> dict:
        if not isinstance(result, dict):
            return {"status": "error", "error": "bad response"}
        if result.get("status") != "ok":
            return {"status": "error",
                    "error": f"top: {result.get('status')}"}
        statuses = (result.get("response", {})
                    .get("data", {}).get("statuses", []))
        if not statuses:
            return {"status": "error", "error": "no statuses"}
        s0 = statuses[0]
        if "error" in s0:
            return {"status": "error", "error": s0["error"]}
        if "filled" in s0:
            return {"status": "filled",
                    "oid": s0["filled"].get("oid"),
                    "avg_px": s0["filled"].get("avgPx"),
                    "total_sz": s0["filled"].get("totalSz")}
        if "resting" in s0:
            return {"status": "resting",
                    "oid": s0["resting"].get("oid")}
        return {"status": "error", "error": f"unknown: {s0}"}

    def get_mid(self, coin: str) -> float:
        try:
            mids = self.info.all_mids()
            return float(mids.get(coin, 0))
        except Exception:
            return 0.0
