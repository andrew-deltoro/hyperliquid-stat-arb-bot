"""
Funding harvester executor.

Opens/closes single-coin delta-neutral positions:
  - Long spot leg (e.g. PURR/USDC)
  - Short perp leg (e.g. PURR)
Both legs atomic: if either fails, unwind the other.

Defaults to dry-run. Moving USDC between the spot and perp pools changes
the margin available to the other bots on this account, so transfers need
allow_transfers=True.
"""
from __future__ import annotations
import math
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account

from dotenv import load_dotenv
load_dotenv(REPO / ".env")


def parse_order_result(result) -> dict:
    """Normalize an SDK order response to {status, total_sz, avg_px, error}."""
    if not isinstance(result, dict) or result.get("status") != "ok":
        return {"status": "error", "error": f"top-level: {result!r}"}
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    s0 = statuses[0] if statuses else {}
    if isinstance(s0, dict) and "filled" in s0:
        f = s0["filled"]
        return {"status": "filled", "total_sz": float(f.get("totalSz", 0)),
                "avg_px": float(f.get("avgPx", 0)), "oid": f.get("oid")}
    if isinstance(s0, dict) and "error" in s0:
        return {"status": "error", "error": s0["error"]}
    return {"status": "error", "error": f"unexpected status: {s0!r}"}


class FundingExecutor:
    def __init__(self, network: str = "mainnet", dry_run: bool = True,
                 allow_transfers: bool = False):
        self.network = network
        self.dry_run = dry_run
        self.allow_transfers = allow_transfers
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

    def _spot_pair(self, coin: str) -> dict | None:
        """Spot universe entry for coin/USDC."""
        meta = self._get_spot_meta()
        tokens = {t["index"]: t["name"] for t in meta.get("tokens", [])}
        for pair in meta.get("universe", []):
            base_idx, quote_idx = pair.get("tokens", [None, None])[:2]
            if tokens.get(base_idx) == coin and tokens.get(quote_idx) == "USDC":
                return pair
        return None

    def _spot_pair_name(self, coin: str) -> str | None:
        pair = self._spot_pair(coin)
        return pair.get("name") if pair else None

    def _mid(self, name: str, spot_index: int | None = None) -> float:
        # all_mids keys most spot pairs as "@<index>" rather than "BASE/USDC";
        # verify against the live response for each coin traded.
        mids = self.info.all_mids()
        px = mids.get(name)
        if px is None and spot_index is not None:
            px = mids.get(f"@{spot_index}")
        return float(px or 0)

    def _round_sz(self, name: str, size: float) -> float:
        asset = self.info.coin_to_asset.get(name)
        if asset is None:
            raise ValueError(f"unknown asset {name}")
        dec = int(self.info.asset_to_sz_decimals[asset])
        scale = 10 ** dec
        return round(math.floor(size * scale + 1e-9) / scale, dec)

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
        if self.dry_run or not self.allow_transfers:
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
        if self.dry_run or not self.allow_transfers:
            return {"action": "would_transfer_to_perp", "amount": round(need_transfer, 2)}
        result = self.exchange.usd_class_transfer(round(need_transfer, 2), to_perp=True)
        return {"action": "transferred_to_perp", "amount": round(need_transfer, 2), "raw": result}

    # --- orders ---
    def _place(self, name: str, is_buy: bool, size: float, px: float,
               slippage: float = 0.02, reduce_only: bool = False) -> dict:
        if px <= 0:
            return {"status": "error", "error": f"no mid price for {name}"}
        try:
            size = self._round_sz(name, size)
        except ValueError as e:
            return {"status": "error", "error": str(e)}
        if size <= 0:
            return {"status": "error", "error": "size rounds to zero"}
        if self.dry_run:
            return {"status": "filled", "simulated": True, "coin": name,
                    "is_buy": is_buy, "total_sz": size, "avg_px": px}
        try:
            limit = self.exchange._slippage_price(name, is_buy, slippage, px)
            raw = self.exchange.order(name, is_buy, size, limit,
                                      {"limit": {"tif": "Ioc"}},
                                      reduce_only=reduce_only)
        except Exception as e:
            return {"status": "error", "error": str(e)}
        out = parse_order_result(raw)
        out.update({"coin": name, "is_buy": is_buy})
        return out

    # --- public: open a funding pair ---
    def open_funding_pair(self, coin: str, notional_usd: float) -> dict:
        """
        Open a delta-neutral pair: long spot + short perp.
        Returns dict with status, per-leg fills, and any unwind info.
        """
        pair = self._spot_pair(coin)
        if pair is None:
            return {"status": "error", "reason": f"No spot pair {coin}/USDC"}
        spot_name = pair["name"]

        spot_fund = self.ensure_spot_usdc(notional_usd)
        perp_fund = self.ensure_perp_usdc(notional_usd * 0.5)
        funding = {"spot": spot_fund, "perp": perp_fund}

        spot_px = self._mid(spot_name, pair.get("index"))
        perp_px = self._mid(coin)
        if spot_px <= 0 or perp_px <= 0:
            return {"status": "error", "reason": "missing mid price", "funding": funding}

        spot_fill = self._place(spot_name, True, notional_usd / spot_px, spot_px)
        if spot_fill["status"] != "filled":
            return {"status": "error", "reason": f"spot order failed: {spot_fill.get('error')}",
                    "funding": funding}

        # Hedge exactly what the spot leg filled.
        perp_fill = self._place(coin, False, spot_fill["total_sz"], perp_px)
        perp_sz = perp_fill.get("total_sz", 0.0) if perp_fill["status"] == "filled" else 0.0
        if perp_sz < spot_fill["total_sz"] * 0.95:
            excess = spot_fill["total_sz"] - perp_sz
            unwind = self._place(spot_name, False, excess, spot_px)
            return {"status": "unwound",
                    "reason": f"perp leg {perp_fill['status']}: {perp_fill.get('error')}",
                    "spot_fill": spot_fill, "perp_fill": perp_fill,
                    "unwind": unwind, "funding": funding}

        return {
            "status": "ok",
            "coin": coin,
            "spot_pair": spot_name,
            "spot_fill": spot_fill,
            "perp_fill": perp_fill,
            "funding": funding,
        }

    # --- public: close a funding pair ---
    def close_funding_pair(self, coin: str, spot_size: float, perp_size: float) -> dict:
        """Close both legs. Reverse of open."""
        pair = self._spot_pair(coin)
        if pair is None:
            return {"status": "error", "reason": f"No spot pair {coin}/USDC"}
        spot_name = pair["name"]
        spot_fill = self._place(spot_name, False, spot_size,
                                self._mid(spot_name, pair.get("index")))
        perp_fill = self._place(coin, True, perp_size, self._mid(coin),
                                reduce_only=True)
        ok = spot_fill["status"] == "filled" and perp_fill["status"] == "filled"
        return {"status": "ok" if ok else "error",
                "spot_fill": spot_fill, "perp_fill": perp_fill}
