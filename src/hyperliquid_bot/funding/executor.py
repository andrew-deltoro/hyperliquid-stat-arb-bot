"""
Funding harvester executor.

Opens/closes single-coin delta-neutral positions:
  - Long spot leg (e.g. PURR/USDC)
  - Short perp leg (e.g. PURR)
Both legs atomic: if either fails, unwind the other.
"""
from __future__ import annotations
import os, sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/home/hlbot/hyperliquid-stat-arb-bot")
sys.path.insert(0, str(REPO))

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account

from dotenv import load_dotenv
load_dotenv(REPO / ".env")


def _round_sig(x: float, sig: int = 5) -> float:
    """Round to `sig` significant digits (HL requires this for order sizes)."""
    if x == 0:
        return 0.0
    from math import log10, floor
    return round(x, -int(floor(log10(abs(x)))) + (sig - 1))


class FundingExecutor:
    def __init__(self, network: str = "mainnet", dry_run: bool = False):
        self.network = network
        self.dry_run = dry_run
        base = constants.MAINNET_API_URL if network == "mainnet" else constants.TESTNET_API_URL
        pk = os.getenv("HL_PRIVATE_KEY_MAINNET" if network == "mainnet" else "HL_PRIVATE_KEY")
        wallet = os.getenv("HL_ACCOUNT_ADDRESS_MAINNET" if network == "mainnet" else "HL_ACCOUNT_ADDRESS")
        if not pk or not wallet:
            raise RuntimeError(f"Missing env vars for {network}")
        self.wallet = wallet
        self.exchange = Exchange(
            Account.from_key(pk), base, account_address=wallet,
            perp_dexs=[""],
        )
        self.info = Info(base, skip_ws=True, perp_dexs=[""])
        self._spot_meta = None

    # --- metadata helpers ---
    def _get_spot_meta(self):
        if self._spot_meta is None:
            self._spot_meta = self.info.spot_meta()
        return self._spot_meta

    def _spot_pair_name(self, coin: str) -> str | None:
        """Find the coin/USDC pair name for a given base coin."""
        meta = self._get_spot_meta()
        for pair in meta.get("universe", []):
            name = pair.get("name", "")
            if name == f"{coin}/USDC":
                return name
        return None

    def _spot_balance(self, coin: str = "USDC") -> float:
        st = self.info.spot_user_state(self.wallet)
        for b in st.get("balances", []):
            if b.get("coin") == coin:
                return float(b.get("total", 0))
        return 0.0

    # --- funding rebalancing ---
    def ensure_spot_usdc(self, needed_usd: float) -> dict:
        """Ensure spot USDC pool has at least needed_usd. Transfer from perp if needed."""
        have = self._spot_balance("USDC")
        if have >= needed_usd:
            return {"action": "noop", "have": have, "need": needed_usd}
        need_transfer = needed_usd - have + 5.0  # 5 USDC buffer
        if self.dry_run:
            return {"action": "would_transfer_to_spot", "amount": round(need_transfer, 2)}
        # to_perp=False means "transfer to spot"
        result = self.exchange.usd_class_transfer(round(need_transfer, 2), to_perp=False)
        return {"action": "transferred_to_spot", "amount": round(need_transfer, 2), "raw": result}

    def ensure_perp_usdc(self, needed_usd: float) -> dict:
        """Ensure perp USDC pool has at least needed_usd (move from spot if needed)."""
        st = self.info.user_state(self.wallet)
        have = float(st.get("marginSummary", {}).get("accountValue", 0))
        if have >= needed_usd:
            return {"action": "noop", "have": have, "need": needed_usd}
        need_transfer = needed_usd - have + 5.0
        if self.dry_run:
            return {"action": "would_transfer_to_perp", "amount": round(need_transfer, 2)}
        result = self.exchange.usd_class_transfer(round(need_transfer, 2), to_perp=True)
        return {"action": "transferred_to_perp", "amount": round(need_transfer, 2), "raw": result}

    # --- spot order ---
    def _place_spot_order(self, spot_pair: str, is_buy: bool, size: float,
                          slippage: float = 0.02) -> dict:
        """Place a market order on a spot pair."""
        mids = self.info.all_mids()
        px = float(mids.get(spot_pair, 0))
        if px <= 0:
            raise RuntimeError(f"No mid price for {spot_pair}")
        size = _round_sig(size, 5)
        if self.dry_run:
            return {
                "status": "simulated", "coin": spot_pair, "is_buy": is_buy,
                "size": size, "px": px, "notional": round(size * px, 2),
            }
        result = self.exchange.market_open(spot_pair, is_buy, size, px, slippage)
        return {"status": "submitted", "coin": spot_pair, "is_buy": is_buy,
                "size": size, "px": px, "raw": result}

    # --- perp order ---
    def _place_perp_order(self, coin: str, is_buy: bool, size: float,
                          slippage: float = 0.02) -> dict:
        """Place a market order on a perp market."""
        mids = self.info.all_mids()
        px = float(mids.get(coin, 0))
        if px <= 0:
            raise RuntimeError(f"No mid price for perp {coin}")
        size = _round_sig(size, 5)
        if self.dry_run:
            return {
                "status": "simulated", "coin": coin, "is_buy": is_buy,
                "size": size, "px": px, "notional": round(size * px, 2),
            }
        result = self.exchange.market_open(coin, is_buy, size, px, slippage)
        return {"status": "submitted", "coin": coin, "is_buy": is_buy,
                "size": size, "px": px, "raw": result}

    # --- public: open a funding pair ---
    def open_funding_pair(self, coin: str, notional_usd: float) -> dict:
        """
        Open a delta-neutral pair: long spot + short perp.
        Returns dict with status, per-leg fills, and any unwind info.
        """
        spot_pair = self._spot_pair_name(coin)
        if spot_pair is None:
            return {"status": "error", "reason": f"No spot pair {coin}/USDC"}

        # 1. Ensure spot USDC available
        spot_fund = self.ensure_spot_usdc(notional_usd)
        # 2. Ensure perp margin (rarely needed if we have main pool)
        perp_fund = self.ensure_perp_usdc(notional_usd * 0.5)

        # 3. Get mid prices to size both legs
        mids = self.info.all_mids()
        spot_px = float(mids.get(spot_pair, 0))
        perp_px = float(mids.get(coin, 0))
        if spot_px <= 0 or perp_px <= 0:
            return {"status": "error", "reason": "missing mid price"}

        # 4. Compute sizes
        spot_size = _round_sig(notional_usd / spot_px, 5)
        perp_size = _round_sig(notional_usd / perp_px, 5)

        # 5. Place spot long first
        try:
            spot_fill = self._place_spot_order(spot_pair, is_buy=True, size=spot_size)
        except Exception as e:
            return {"status": "error", "reason": f"spot order failed: {e}",
                    "funding": {"spot": spot_fund, "perp": perp_fund}}

        # 6. Place perp short
        try:
            perp_fill = self._place_perp_order(coin, is_buy=False, size=perp_size)
        except Exception as e:
            # Unwind spot
            try:
                unwind = self._place_spot_order(spot_pair, is_buy=False, size=spot_size)
            except Exception as ue:
                unwind = {"error": str(ue)}
            return {"status": "unwound",
                    "reason": f"perp failed: {e}",
                    "spot_fill": spot_fill, "unwind": unwind,
                    "funding": {"spot": spot_fund, "perp": perp_fund}}

        return {
            "status": "ok",
            "coin": coin,
            "spot_pair": spot_pair,
            "spot_fill": spot_fill,
            "perp_fill": perp_fill,
            "funding": {"spot": spot_fund, "perp": perp_fund},
        }

    # --- public: close a funding pair ---
    def close_funding_pair(self, coin: str, spot_size: float, perp_size: float) -> dict:
        """Close both legs. Reverse of open."""
        spot_pair = self._spot_pair_name(coin)
        if spot_pair is None:
            return {"status": "error", "reason": f"No spot pair {coin}/USDC"}
        try:
            spot_fill = self._place_spot_order(spot_pair, is_buy=False, size=spot_size)
            perp_fill = self._place_perp_order(coin, is_buy=True, size=perp_size)
            return {"status": "ok", "spot_fill": spot_fill, "perp_fill": perp_fill}
        except Exception as e:
            return {"status": "error", "reason": str(e)}
