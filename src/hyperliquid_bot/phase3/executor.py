"""
Executor: places orders (dry-run or live testnet/mainnet).

LiveExecutor uses hyperliquid-python-sdk with an API wallet pattern.
The API wallet's private key signs; the main wallet is the account_address.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .testnet_safeguards import Safeguards, REPO_ROOT


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
        state_path = REPO_ROOT / "state" / "positions.json"
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

        # --- Place order ---
        is_buy = order.side == "buy"
        try:
            slippage = float(
                self.cfg.raw.get("executor", {})
                .get("slippage_tolerance", 0.03)
            )
            result = self.exchange.market_open(
                order.coin, is_buy, order.size, ref_price, slippage,
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
            coin=order.coin, side=order.side, size=order.size,
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
