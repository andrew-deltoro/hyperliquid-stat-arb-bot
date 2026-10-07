"""
Minimal HL executor for single-leg perp orders.

Reuses the live SDK pattern from the pairs executor but is stripped down:
  - One leg at a time (no rollback logic)
  - IOC orders with slippage tolerance; closes are reduce-only for this
    strategy's own quantity (the account nets coins across strategies)
  - Reduce-only stop-loss trigger order resting on the exchange
"""
from __future__ import annotations
import math
import os

from ..account import resolve_account_targets


class DirectionalExecutor:
    def __init__(self, cfg, log_fn=print, exchange=None, info=None, wallet=None):
        self.cfg = cfg
        self.log_fn = log_fn

        ex_cfg = cfg.raw.get("executor", {})
        network = ex_cfg.get("network", "mainnet")
        self.slippage = float(ex_cfg.get("slippage_tolerance", 0.01))

        if exchange is not None and info is not None:
            self.exchange, self.info, self.wallet = exchange, info, wallet or ""
            self.wallet_address = self.wallet
            self.master_address = wallet or ""
            self.vault_address = None
            return

        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants

        self.base_url = (constants.MAINNET_API_URL if network == "mainnet"
                         else constants.TESTNET_API_URL)
        pk = os.getenv(ex_cfg.get("private_key_env", "HL_PRIVATE_KEY_MAINNET"))
        if not pk:
            raise RuntimeError("Missing HL credentials")
        targets = resolve_account_targets(ex_cfg)
        self.wallet = targets.trading_address
        self.wallet_address = targets.trading_address
        self.master_address = targets.master_address
        self.vault_address = targets.vault_address
        account = Account.from_key(pk)
        self.exchange = Exchange(
            account, self.base_url,
            account_address=targets.master_address,
            vault_address=targets.vault_address,
            perp_dexs=[""],
        )
        self.info = Info(self.base_url, skip_ws=True, perp_dexs=[""])

        acct_note = (
            f"subaccount={self.wallet[:10]}... "
            f"(master={self.master_address[:10]}...)"
            if self.vault_address else
            f"account={self.wallet[:10]}..."
        )
        self.log_fn(f"[DIR-EXECUTOR] network={network} {acct_note}")

    def _round_sz(self, coin: str, sz: float) -> float:
        asset = self.info.coin_to_asset.get(coin)
        if asset is None:
            raise ValueError(f"unknown asset {coin}")
        dec = int(self.info.asset_to_sz_decimals[asset])
        scale = 10 ** dec
        return round(math.floor(sz * scale + 1e-9) / scale, dec)

    def get_position(self, coin: str) -> float | None:
        try:
            state = self.info.user_state(self.wallet)
        except Exception as e:
            self.log_fn(f"[DIR-EXECUTOR] position query failed: {e}")
            return None
        for p in state.get("assetPositions", []):
            pos = p.get("position", {})
            if pos.get("coin") == coin:
                return float(pos.get("szi", 0))
        return 0.0

    def _ioc(self, coin: str, is_buy: bool, size: float, ref_px: float,
             reduce_only: bool) -> dict:
        try:
            size = self._round_sz(coin, size)
        except Exception as e:
            return {"status": "error", "error": str(e)}
        if size <= 0:
            return {"status": "error", "error": "size rounds to zero"}
        try:
            px = self.exchange._slippage_price(coin, is_buy, self.slippage, ref_px)
            r = self.exchange.order(coin, is_buy, size, px,
                                    {"limit": {"tif": "Ioc"}},
                                    reduce_only=reduce_only)
        except Exception as e:
            return {"status": "error", "error": str(e)}
        return self._parse(r)

    def market_open(self, coin: str, is_buy: bool, size: float,
                    ref_px: float) -> dict:
        return self._ioc(coin, is_buy, size, ref_px, reduce_only=False)

    def reduce(self, coin: str, is_buy: bool, size: float, ref_px: float) -> dict:
        return self._ioc(coin, is_buy, size, ref_px, reduce_only=True)

    def place_stop(self, coin: str, is_buy: bool, size: float,
                   trigger_px: float) -> dict:
        """Reduce-only stop-market trigger order resting on the exchange."""
        try:
            size = self._round_sz(coin, size)
            trig = self.exchange._slippage_price(coin, is_buy, 0.0, trigger_px)
            limit = self.exchange._slippage_price(coin, is_buy, self.slippage, trigger_px)
            r = self.exchange.order(
                coin, is_buy, size, limit,
                {"trigger": {"triggerPx": trig, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True,
            )
        except Exception as e:
            return {"status": "error", "error": str(e)}
        return self._parse(r)

    def cancel(self, coin: str, oid) -> bool:
        try:
            r = self.exchange.cancel(coin, int(oid))
        except Exception as e:
            self.log_fn(f"[DIR-EXECUTOR] cancel {coin} oid={oid} failed: {e}")
            return False
        return isinstance(r, dict) and r.get("status") == "ok"

    def cancel_stops(self, coin: str) -> bool:
        """Cancel every reduce-only trigger order resting on `coin`."""
        try:
            orders = self.info.frontend_open_orders(self.wallet)
        except Exception as e:
            self.log_fn(f"[DIR-EXECUTOR] open-order query failed: {e}")
            return False
        ok = True
        for o in orders:
            if o.get("coin") == coin and o.get("isTrigger") and o.get("reduceOnly"):
                ok &= self.cancel(coin, o["oid"])
        return ok

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
        if s0 == "waitingForTrigger" or (isinstance(s0, dict) and "waitingForTrigger" in s0):
            return {"status": "resting", "oid": None}
        if not isinstance(s0, dict):
            return {"status": "error", "error": f"unknown: {s0}"}
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
