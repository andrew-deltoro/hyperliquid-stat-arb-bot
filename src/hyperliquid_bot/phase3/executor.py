"""
Executor: places orders (dry-run or live testnet/mainnet).

LiveExecutor uses hyperliquid-python-sdk with an API wallet pattern.
The API wallet's private key signs; the main wallet is the account_address.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .testnet_safeguards import Safeguards
from .paths import STATE_DIR


@dataclass
class Order:
    coin: str
    side: str           # "buy" / "sell"
    size: float
    notional_usd: float
    tag: str


@dataclass
class Fill:
    coin: str
    side: str
    size: float
    price: float
    notional_usd: float
    tag: str
    timestamp_iso: str
    simulated: bool
    status: str = "filled"      # "filled", "rejected", "error"
    order_id: str = ""
    error: str = ""


class BaseExecutor:
    def __init__(self, cfg):
        self.cfg = cfg

    def place_order(self, order: Order, ref_price: float) -> Fill:
        raise NotImplementedError


class DryRunExecutor(BaseExecutor):
    def __init__(self, cfg, log_fn=print):
        super().__init__(cfg)
        self.log_fn = log_fn

    def place_order(self, order: Order, ref_price: float) -> Fill:
        fill = Fill(
            coin=order.coin, side=order.side, size=order.size,
            price=ref_price, notional_usd=order.notional_usd,
            tag=order.tag,
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            simulated=True, status="filled",
        )
        self.log_fn(
            f"[DRY-RUN] {fill.side.upper():4s} {fill.size:.6f} "
            f"{fill.coin:6s} @ {fill.price:.6f}  "
            f"(≈${fill.notional_usd:.2f})  tag={fill.tag}"
        )
        return fill


class LiveExecutor(BaseExecutor):
    """Real Hyperliquid testnet/mainnet order placement with safeguards."""

    def __init__(self, cfg, log_fn=print):
        super().__init__(cfg)
        self.log_fn = log_fn

        try:
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
            from eth_account import Account
        except ImportError as e:
            raise RuntimeError(
                f"Live executor requires hyperliquid-python-sdk and "
                f"eth-account: {e}"
            ) from e

        # --- Load executor config ---
        ex_cfg = cfg.raw.get("executor", {})
        network = ex_cfg.get("network", "testnet")
        self.network = network
        self.base_url = (
            constants.TESTNET_API_URL
            if network == "testnet"
            else constants.MAINNET_API_URL
        )

        # --- Load API wallet private key ---
        pk_env = ex_cfg.get("private_key_env", "HL_PRIVATE_KEY")
        self.private_key = os.getenv(pk_env)
        if not self.private_key:
            raise RuntimeError(
                f"Live executor requires env var {pk_env} in .env"
            )

        # --- Init SDK clients ---
        account = Account.from_key(self.private_key)
        self.api_wallet_address = account.address

        # API wallet trades on behalf of the main wallet.
        acct_env = ex_cfg.get("account_address_env", "HL_ACCOUNT_ADDRESS")
        self.wallet_address = os.getenv(acct_env, "").strip()
        if not self.wallet_address:
            raise RuntimeError(
                f"Live executor requires env var {acct_env} "
                f"(main wallet address) in .env"
            )

        self.exchange = Exchange(
            account, self.base_url, account_address=self.wallet_address,
        )
        self.info = Info(self.base_url, skip_ws=True)

        # --- Safeguards ---
        sg = cfg.raw.get("safeguards", {})
        self.safeguards = Safeguards(
            max_notional_per_order=float(sg.get("max_notional_per_order", 50.0)),
            max_total_exposure_usd=float(sg.get("max_total_exposure_usd", 100.0)),
            max_daily_orders=int(sg.get("max_daily_orders", 20)),
        )

        self.log_fn(
            f"[LIVE-EXECUTOR] network={network} "
            f"api_wallet={self.api_wallet_address[:10]}... "
            f"account={self.wallet_address[:10]}... "
            f"safeguards: max_order=${sg.get('max_notional_per_order', 50)}, "
            f"max_exposure=${sg.get('max_total_exposure_usd', 100)}, "
            f"max_daily={sg.get('max_daily_orders', 20)}"
        )

        # --- Verify account has funds ---
        try:
            user_state = self.info.user_state(self.wallet_address)
            margin = user_state.get("marginSummary", {})
            account_value = float(margin.get("accountValue", 0))

            # Unified account mode: USDC held as spot collateral,
            # perp marginSummary.accountValue reports 0. Fall back to spot.
            if account_value < 1:
                spot_state = self.info.spot_user_state(self.wallet_address)
                for b in spot_state.get("balances", []):
                    if b.get("coin") == "USDC":
                        account_value = float(b.get("total", 0))
                        break

            self.log_fn(
                f"[LIVE-EXECUTOR] account value: ${account_value:.2f}"
            )
            if account_value < 10:
                self.log_fn(
                    f"[LIVE-EXECUTOR] WARNING: account value "
                    f"${account_value:.2f} is low. Visit "
                    f"https://app.hyperliquid-testnet.xyz/drip"
                )
        except Exception as e:
            self.log_fn(f"[LIVE-EXECUTOR] could not fetch account state: {e}")

    def _current_total_notional(self) -> float:
        import json
        state_path = STATE_DIR / "positions.json"
        if not state_path.exists():
            return 0.0
        with open(state_path) as f:
            data = json.load(f)
        return sum(
            p.get("notional_usd", 0.0)
            for p in data.get("positions", [])
            if p.get("status") == "open"
        )

    def place_order(self, order: Order, ref_price: float) -> Fill:
        # --- Safeguards ---
        check = self.safeguards.check_order(
            order_notional_usd=order.notional_usd,
            current_total_notional=self._current_total_notional(),
        )
        if not check.allowed:
            self.log_fn(f"[LIVE-EXECUTOR] REJECTED: {check.reason}")
            return Fill(
                coin=order.coin, side=order.side, size=order.size,
                price=ref_price, notional_usd=order.notional_usd,
                tag=order.tag,
                timestamp_iso=datetime.now(timezone.utc).isoformat(),
                simulated=False, status="rejected", error=check.reason,
            )

        # --- Round size to exchange precision ---
        try:
            meta = self.info.meta()
            sz_decimals = None
            for asset in meta.get("universe", []):
                if asset.get("name") == order.coin:
                    sz_decimals = int(asset.get("szDecimals", 0))
                    break
            if sz_decimals is None:
                sz_decimals = 2
            rounded_size = round(order.size, sz_decimals)
            if rounded_size <= 0:
                raise ValueError(
                    f"rounded size {rounded_size} is zero for {order.coin}"
                )
        except Exception as e:
            self.log_fn(f"[LIVE-EXECUTOR] ERROR sizing {order.coin}: {e}")
            return Fill(
                coin=order.coin, side=order.side, size=order.size,
                price=ref_price, notional_usd=order.notional_usd,
                tag=order.tag,
                timestamp_iso=datetime.now(timezone.utc).isoformat(),
                simulated=False, status="error", error=str(e),
            )

        # --- Try maker order first (if enabled) ---
        ex_cfg = self.cfg.raw.get("executor", {})
        prefer_maker = bool(ex_cfg.get("prefer_maker", False))
        maker_timeout = int(ex_cfg.get("maker_timeout_sec", 30))

        if prefer_maker:
            maker_fill = self._try_maker(order, rounded_size, maker_timeout)
            if maker_fill is not None:
                self.safeguards.increment_daily_order_count()
                return maker_fill

        # --- Fall back to market order ---
        is_buy = order.side == "buy"
        try:
            slippage = float(ex_cfg.get("slippage_tolerance", 0.03))
            result = self.exchange.market_open(
                order.coin, is_buy, rounded_size, ref_price, slippage,
            )
        except Exception as e:
            self.log_fn(f"[LIVE-EXECUTOR] ERROR placing {order.tag}: {e}")
            return Fill(
                coin=order.coin, side=order.side, size=rounded_size,
                price=ref_price, notional_usd=order.notional_usd,
                tag=order.tag,
                timestamp_iso=datetime.now(timezone.utc).isoformat(),
                simulated=False, status="error", error=str(e),
            )

        # --- Parse result ---
        order_status = "error"
        oid = ""
        error_msg = ""

        if isinstance(result, dict):
            top = result.get("status", "unknown")
            if top != "ok":
                error_msg = f"top-level status={top}"
            else:
                statuses = (
                    result.get("response", {})
                    .get("data", {})
                    .get("statuses", [])
                )
                if not statuses:
                    error_msg = "no statuses in response"
                else:
                    first = statuses[0]
                    if isinstance(first, dict):
                        if "error" in first:
                            error_msg = str(first["error"])
                        elif "filled" in first:
                            order_status = "filled"
                            oid = str(first["filled"].get("oid", ""))
                        elif "resting" in first:
                            order_status = "filled"
                            oid = str(first["resting"].get("oid", ""))
                        else:
                            error_msg = f"unknown status: {first}"
                    else:
                        error_msg = f"non-dict status: {first}"

        fill = Fill(
            coin=order.coin, side=order.side, size=rounded_size,
            price=ref_price, notional_usd=order.notional_usd,
            tag=order.tag,
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            simulated=False,
            status=order_status,
            order_id=oid,
            error=error_msg,
        )

        if fill.status == "filled":
            self.safeguards.increment_daily_order_count()
            self.log_fn(
                f"[LIVE-EXECUTOR] TAKER {fill.side.upper():4s} "
                f"{fill.size:.6f} {fill.coin:6s} @ {fill.price:.6f}  "
                f"oid={fill.order_id}  tag={fill.tag}"
            )
        else:
            self.log_fn(
                f"[LIVE-EXECUTOR] FAIL {fill.side.upper():4s} "
                f"{fill.size:.6f} {fill.coin:6s}  "
                f"ERROR: {fill.error}  tag={fill.tag}"
            )
        return fill

    # ---------------- maker order helper ----------------
    def _try_maker(self, order: Order, rounded_size: float,
                   timeout_sec: int) -> Fill | None:
        """
        Try to place a post-only limit order at the best bid/ask.
        Returns a Fill if successful, None if we should fall back to taker.
        """
        is_buy = order.side == "buy"
        try:
            book = self.info.l2_snapshot(order.coin)
            bids = book["levels"][0]
            asks = book["levels"][1]
            if not bids or not asks:
                self.log_fn(
                    f"[LIVE-EXECUTOR] maker skip: empty book for {order.coin}"
                )
                return None
            best_bid = float(bids[0]["px"])
            best_ask = float(asks[0]["px"])

            # Post-only at best bid (buy) or best ask (sell)
            maker_px = best_bid if is_buy else best_ask
            self.log_fn(
                f"[LIVE-EXECUTOR] maker attempt: {order.side.upper()} "
                f"{rounded_size:.6f} {order.coin} @ {maker_px:.6f} "
                f"(bid={best_bid:.4f} ask={best_ask:.4f})"
            )

            maker_result = self.exchange.order(
                name=order.coin,
                is_buy=is_buy,
                sz=rounded_size,
                limit_px=maker_px,
                order_type={"limit": {"tif": "Alo"}},
            )
        except Exception as e:
            self.log_fn(f"[LIVE-EXECUTOR] maker error: {e}")
            return None

        # Extract OID
        oid = ""
        if isinstance(maker_result, dict):
            statuses = (
                maker_result.get("response", {})
                .get("data", {})
                .get("statuses", [])
            )
            if statuses and isinstance(statuses[0], dict):
                first = statuses[0]
                if "resting" in first:
                    oid = str(first["resting"].get("oid", ""))
                elif "filled" in first:
                    # Filled immediately (rare for post-only but possible)
                    oid = str(first["filled"].get("oid", ""))
                elif "error" in first:
                    self.log_fn(
                        f"[LIVE-EXECUTOR] maker rejected: {first['error']}"
                    )
                    return None

        if not oid:
            self.log_fn("[LIVE-EXECUTOR] maker no OID returned")
            return None

        # Wait for fill
        poll_interval = 5
        polls = max(1, timeout_sec // poll_interval)
        for _ in range(polls):
            time.sleep(poll_interval)
            try:
                status = self.info.order_status(self.wallet_address, int(oid))
                if isinstance(status, dict):
                    order_info = status.get("order", {})
                    if order_info.get("status") == "filled":
                        self.log_fn(
                            f"[LIVE-EXECUTOR] MAKER FILLED "
                            f"{order.side.upper()} {rounded_size:.6f} "
                            f"{order.coin} @ {maker_px:.6f} oid={oid}"
                        )
                        return Fill(
                            coin=order.coin, side=order.side,
                            size=rounded_size, price=maker_px,
                            notional_usd=order.notional_usd,
                            tag=order.tag,
                            timestamp_iso=datetime.now(timezone.utc).isoformat(),
                            simulated=False,
                            status="filled", order_id=oid, error="",
                        )
            except Exception as e:
                self.log_fn(f"[LIVE-EXECUTOR] status check error: {e}")
                break

        # Timeout: cancel and return None
        try:
            self.exchange.cancel(order.coin, int(oid))
            self.log_fn(
                f"[LIVE-EXECUTOR] maker timeout ({timeout_sec}s), "
                f"cancelled oid={oid}"
            )
        except Exception as e:
            self.log_fn(f"[LIVE-EXECUTOR] cancel error: {e}")

        return None


        # --- Place order ---
        is_buy = order.side == "buy"

        # Round size to exchange precision (szDecimals per asset).
        # Hyperliquid rejects sizes with too many decimals.
        try:
            meta = self.info.meta()
            sz_decimals = None
            for asset in meta.get("universe", []):
                if asset.get("name") == order.coin:
                    sz_decimals = int(asset.get("szDecimals", 0))
                    break
            if sz_decimals is None:
                # Fallback: assume 2 decimals for unknown assets
                sz_decimals = 2
            rounded_size = round(order.size, sz_decimals)
            if rounded_size <= 0:
                raise ValueError(
                    f"rounded size {rounded_size} is zero for {order.coin} "
                    f"(raw={order.size}, szDecimals={sz_decimals})"
                )
        except Exception as e:
            self.log_fn(
                f"[LIVE-EXECUTOR] ERROR sizing {order.coin}: {e}"
            )
            return Fill(
                coin=order.coin, side=order.side, size=order.size,
                price=ref_price, notional_usd=order.notional_usd,
                tag=order.tag,
                timestamp_iso=datetime.now(timezone.utc).isoformat(),
                simulated=False, status="error", error=str(e),
            )

        try:
            slippage = float(
                self.cfg.raw.get("executor", {})
                .get("slippage_tolerance", 0.03)
            )
            result = self.exchange.market_open(
                order.coin, is_buy, rounded_size, ref_price, slippage,
            )


        except Exception as e:
            self.log_fn(f"[LIVE-EXECUTOR] ERROR placing {order.tag}: {e}")
            return Fill(
                coin=order.coin, side=order.side, size=order.size,
                price=ref_price, notional_usd=order.notional_usd,
                tag=order.tag,
                timestamp_iso=datetime.now(timezone.utc).isoformat(),
                simulated=False, status="error", error=str(e),
            )

        # --- Parse result ---
        # Hyperliquid SDK returns:
        #   {"status": "ok", "response": {"data": {"statuses": [...]}}}
        # Each entry in statuses can be:
        #   {"resting": {"oid": 123}}  -> limit order placed
        #   {"filled": {"oid": 456}}   -> market order filled
        #   {"error": "message"}       -> rejected
        order_status = "error"
        oid = ""
        error_msg = ""

        if isinstance(result, dict):
            top = result.get("status", "unknown")
            if top != "ok":
                error_msg = f"top-level status={top}"
            else:
                statuses = (
                    result.get("response", {})
                    .get("data", {})
                    .get("statuses", [])
                )
                if not statuses:
                    error_msg = "no statuses in response"
                else:
                    first = statuses[0]
                    if isinstance(first, dict):
                        if "error" in first:
                            error_msg = str(first["error"])
                        elif "filled" in first:
                            order_status = "filled"
                            oid = str(first["filled"].get("oid", ""))
                        elif "resting" in first:
                            order_status = "filled"
                            oid = str(first["resting"].get("oid", ""))
                        else:
                            error_msg = f"unknown status: {first}"
                    else:
                        error_msg = f"non-dict status: {first}"

        fill = Fill(
            coin=order.coin, side=order.side, size=rounded.size,
            price=ref_price, notional_usd=order.notional_usd,
            tag=order.tag,
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            simulated=False,
            status=order_status,
            order_id=oid,
            error=error_msg,
        )

        if fill.status == "filled":
            self.safeguards.increment_daily_order_count()
            self.log_fn(
                f"[LIVE-EXECUTOR] OK {fill.side.upper():4s} "
                f"{fill.size:.6f} {fill.coin:6s} @ {fill.price:.6f}  "
                f"oid={fill.order_id}  tag={fill.tag}"
            )
        else:
            self.log_fn(
                f"[LIVE-EXECUTOR] FAIL {fill.side.upper():4s} "
                f"{fill.size:.6f} {fill.coin:6s}  "
                f"ERROR: {fill.error}  tag={fill.tag}"
            )
        return fill


def build_executor(cfg, log_fn=print) -> BaseExecutor:
    mode = cfg.raw.get("executor", {}).get("mode", "dry_run")
    if mode == "dry_run":
        return DryRunExecutor(cfg, log_fn=log_fn)
    elif mode == "live":
        return LiveExecutor(cfg, log_fn=log_fn)
    raise ValueError(f"Unknown executor mode: {mode}")
