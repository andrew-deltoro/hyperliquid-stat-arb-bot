"""
Executor: places orders (dry-run or live testnet/mainnet).

LiveExecutor uses hyperliquid-python-sdk with an API wallet pattern.
The API wallet's private key signs; the main wallet is the account_address.
Optional strategy subaccounts are passed as Exchange.vault_address (HL M8).
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ..account import resolve_account_targets
from .testnet_safeguards import Safeguards


@dataclass
class Order:
    coin: str
    side: str           # "buy" / "sell"
    size: float
    notional_usd: float
    tag: str
    reduce_only: bool = False


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
    status: str = "filled"      # "filled", "partial", "none", "rejected", "error", "unknown"
    order_id: str = ""
    error: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dex_of(coin: str) -> str:
    return coin.split(":", 1)[0] if ":" in coin else ""


class BaseExecutor:
    # True when get_position() reflects real exchange state and callers
    # should measure position changes instead of trusting fills.
    verifies_positions = False

    def __init__(self, cfg):
        self.cfg = cfg

    def place_order(self, order: Order, ref_price: float) -> Fill:
        raise NotImplementedError

    def get_position(self, coin: str) -> float | None:
        """Signed position size on the exchange, or None if unknown."""
        return None

    def all_positions(self) -> dict[str, float] | None:
        """Non-zero signed positions across all traded dexes, or None if unknown."""
        return None

    def lot_size(self, coin: str) -> float:
        return 0.0

    def account_equity(self) -> dict[str, float] | None:
        """Equity per dex ("" = main), or None if unknown."""
        bal = float(self.cfg.position.get("dry_run_balance",
                                          self.cfg.position.get("baseline_balance", 0.0)))
        split = (self.cfg.raw.get("leverage", {}) or {}).get("dry_run_dex_split", {"": 1.0})
        return {dex: bal * float(frac) for dex, frac in split.items()}

    def account_gross(self) -> float | None:
        """Gross position value across all dexes, or None if unknown."""
        return 0.0

    def maintenance_frac(self, coin: str) -> float:
        lev = self.cfg.raw.get("leverage", {}) or {}
        return 0.5 / float(lev.get("default_max_leverage", 3.0))

    def ensure_leverage(self, coin: str) -> bool:
        return True


class DryRunExecutor(BaseExecutor):
    def __init__(self, cfg, log_fn=print):
        super().__init__(cfg)
        self.log_fn = log_fn

    def place_order(self, order: Order, ref_price: float) -> Fill:
        fill = Fill(
            coin=order.coin, side=order.side, size=order.size,
            price=ref_price, notional_usd=order.notional_usd,
            tag=order.tag,
            timestamp_iso=_now_iso(),
            simulated=True, status="filled",
        )
        self.log_fn(
            f"[DRY-RUN] {fill.side.upper():4s} {fill.size:.6f} "
            f"{fill.coin:6s} @ {fill.price:.6f}  "
            f"(≈${fill.notional_usd:.2f})  tag={fill.tag}"
            f"{'  reduce_only' if order.reduce_only else ''}"
        )
        return fill


class LiveExecutor(BaseExecutor):
    """Real Hyperliquid testnet/mainnet order placement with safeguards."""

    verifies_positions = True
    # Main dex ("") plus the xyz HIP-3 dex. Passing perp_dexs=["xyz"] alone
    # would hide all main-dex symbols, so "" must be listed explicitly.
    PERP_DEXS = ["", "xyz"]

    def __init__(self, cfg, log_fn=print, exchange=None, info=None,
                 wallet_address: str | None = None):
        super().__init__(cfg)
        self.log_fn = log_fn

        ex_cfg = cfg.raw.get("executor", {})
        self.network = ex_cfg.get("network", "testnet")

        if exchange is not None and info is not None:
            self.exchange = exchange
            self.info = info
            self.wallet_address = wallet_address or ""
            self.master_address = self.wallet_address
            self.vault_address = None
            self.api_wallet_address = ""
        else:
            self._init_sdk(ex_cfg)

        sg = cfg.raw.get("safeguards", {})
        self.safeguards = Safeguards(
            max_notional_per_order=float(sg.get("max_notional_per_order", 50.0)),
            max_total_exposure_usd=float(sg.get("max_total_exposure_usd", 100.0)),
            max_daily_orders=int(sg.get("max_daily_orders", 20)),
        )

        vault = getattr(self, "vault_address", None)
        acct_note = (
            f"subaccount={self.wallet_address[:10]}... "
            f"(master={getattr(self, 'master_address', self.wallet_address)[:10]}...)"
            if vault else
            f"account={self.wallet_address[:10]}..."
        )
        self.log_fn(
            f"[LIVE-EXECUTOR] network={self.network} "
            f"api_wallet={self.api_wallet_address[:10]}... "
            f"{acct_note} "
            f"safeguards: max_order=${sg.get('max_notional_per_order', 50)}, "
            f"max_exposure=${sg.get('max_total_exposure_usd', 100)}, "
            f"max_daily={sg.get('max_daily_orders', 20)}"
        )

        if exchange is None:
            self._log_account_value()

    def _init_sdk(self, ex_cfg: dict) -> None:
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

        base_url = (
            constants.TESTNET_API_URL
            if self.network == "testnet"
            else constants.MAINNET_API_URL
        )

        pk_env = ex_cfg.get("private_key_env", "HL_PRIVATE_KEY")
        private_key = os.getenv(pk_env)
        if not private_key:
            raise RuntimeError(
                f"Live executor requires env var {pk_env} in .env"
            )
        account = Account.from_key(private_key)
        self.api_wallet_address = account.address

        # API wallet signs; master owns the agent; optional subaccount holds
        # positions (Exchange.vault_address). Info queries use trading_address.
        targets = resolve_account_targets(ex_cfg)
        self.master_address = targets.master_address
        self.wallet_address = targets.trading_address
        self.vault_address = targets.vault_address

        self.exchange = Exchange(
            account, base_url,
            account_address=self.master_address,
            vault_address=self.vault_address,
            perp_dexs=self.PERP_DEXS,
        )
        self.info = Info(base_url, skip_ws=True, perp_dexs=self.PERP_DEXS)

    def _log_account_value(self) -> None:
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
                    f"${account_value:.2f} is low."
                )
        except Exception as e:
            self.log_fn(f"[LIVE-EXECUTOR] could not fetch account state: {e}")

    # ---------------- exchange state ----------------
    def _user_state(self, dex: str):
        if dex:
            return self.info.user_state(self.wallet_address, dex)
        return self.info.user_state(self.wallet_address)

    def get_position(self, coin: str) -> float | None:
        try:
            state = self._user_state(_dex_of(coin))
        except Exception as e:
            self.log_fn(f"[LIVE-EXECUTOR] position query failed for {coin}: {e}")
            return None
        for p in state.get("assetPositions", []):
            pos = p.get("position", {})
            if pos.get("coin") == coin:
                return float(pos.get("szi", 0))
        return 0.0

    def all_positions(self) -> dict[str, float] | None:
        out: dict[str, float] = {}
        for dex in self.PERP_DEXS:
            try:
                state = self._user_state(dex)
            except Exception as e:
                self.log_fn(f"[LIVE-EXECUTOR] position query failed (dex={dex!r}): {e}")
                return None
            for p in state.get("assetPositions", []):
                pos = p.get("position", {})
                szi = float(pos.get("szi", 0))
                if szi != 0:
                    out[pos["coin"]] = szi
        return out

    def account_equity(self) -> dict[str, float] | None:
        out: dict[str, float] = {}
        for dex in self.PERP_DEXS:
            try:
                state = self._user_state(dex)
            except Exception as e:
                self.log_fn(f"[LIVE-EXECUTOR] equity query failed (dex={dex!r}): {e}")
                return None
            out[dex] = float(state.get("marginSummary", {}).get("accountValue", 0))
        if out.get("", 0.0) < 1:
            # Unified account mode reports perp accountValue 0 and holds
            # collateral as spot USDC.
            try:
                spot = self.info.spot_user_state(self.wallet_address)
            except Exception as e:
                self.log_fn(f"[LIVE-EXECUTOR] spot equity query failed: {e}")
                return None
            for b in spot.get("balances", []):
                if b.get("coin") == "USDC":
                    out[""] = float(b.get("total", 0))
        return out

    def account_gross(self) -> float | None:
        return self._current_total_notional()

    def _asset_meta(self, coin: str) -> dict:
        if not hasattr(self, "_meta_cache"):
            self._meta_cache: dict[str, dict] = {}
        if coin not in self._meta_cache:
            dex = _dex_of(coin)
            meta = self.info.meta(dex=dex) if dex else self.info.meta()
            for a in meta.get("universe", []):
                name = a.get("name", "")
                key = name if (not dex or name.startswith(f"{dex}:")) else f"{dex}:{name}"
                self._meta_cache[key] = a
        return self._meta_cache.get(coin, {})

    def maintenance_frac(self, coin: str) -> float:
        # Assumes maintenance margin = half the initial margin at max
        # leverage; verify against Hyperliquid's margin tables per asset.
        try:
            max_lev = float(self._asset_meta(coin).get("maxLeverage", 0))
        except Exception:
            max_lev = 0.0
        if max_lev <= 0:
            return super().maintenance_frac(coin)
        return 0.5 / max_lev

    def ensure_leverage(self, coin: str) -> bool:
        """
        Set the exchange leverage and margin mode for `coin` before an entry.
        Returns False (block the entry) if it can't be set.
        """
        if not hasattr(self, "_leverage_ok"):
            self._leverage_ok: set[str] = set()
        if coin in self._leverage_ok:
            return True
        lev_cfg = self.cfg.raw.get("leverage", {}) or {}
        try:
            meta = self._asset_meta(coin)
        except Exception as e:
            self.log_fn(f"[LEVERAGE] {coin}: metadata unavailable: {e}")
            return False
        isolated = bool(meta.get("onlyIsolated", False))
        want = int(lev_cfg.get("isolated_leverage" if isolated else "exchange_leverage",
                               2 if isolated else 3))
        max_lev = int(meta.get("maxLeverage", want) or want)
        want = max(1, min(want, max_lev))
        try:
            result = self.exchange.update_leverage(want, coin, is_cross=not isolated)
        except Exception as e:
            self.log_fn(f"[LEVERAGE] {coin}: update_leverage failed: {e}")
            return False
        if not isinstance(result, dict) or result.get("status") != "ok":
            self.log_fn(f"[LEVERAGE] {coin}: update_leverage rejected: {result!r}")
            return False
        self.log_fn(f"[LEVERAGE] {coin}: {want}x {'isolated' if isolated else 'cross'} (max {max_lev}x)")
        self._leverage_ok.add(coin)
        return True

    def _current_total_notional(self) -> float | None:
        """Gross position value across all traded dexes, or None if unknown."""
        total = 0.0
        for dex in self.PERP_DEXS:
            try:
                state = self._user_state(dex)
            except Exception as e:
                self.log_fn(f"[LIVE-EXECUTOR] live exposure check failed (dex={dex!r}): {e}")
                return None
            for p in state.get("assetPositions", []):
                pos = p.get("position", {})
                if float(pos.get("szi", 0)) != 0:
                    total += abs(float(pos.get("positionValue", 0)))
        return total

    def _sz_decimals(self, coin: str) -> int:
        asset = self.info.coin_to_asset.get(coin)
        if asset is None:
            raise ValueError(f"unknown asset {coin}: no size precision available")
        return int(self.info.asset_to_sz_decimals[asset])

    def lot_size(self, coin: str) -> float:
        try:
            return 10.0 ** -self._sz_decimals(coin)
        except Exception:
            return 0.0

    @staticmethod
    def _round_down(size: float, decimals: int) -> float:
        scale = 10 ** decimals
        return round(math.floor(size * scale + 1e-9) / scale, decimals)

    def _fill(self, order: Order, price: float, size: float, status: str,
              oid: str = "", error: str = "") -> Fill:
        return Fill(
            coin=order.coin, side=order.side, size=size, price=price,
            notional_usd=order.notional_usd, tag=order.tag,
            timestamp_iso=_now_iso(), simulated=False,
            status=status, order_id=oid, error=error,
        )

    # ---------------- order placement ----------------
    def place_order(self, order: Order, ref_price: float) -> Fill:
        current = None if order.reduce_only else self._current_total_notional()
        check = self.safeguards.check_order(
            order_notional_usd=order.notional_usd,
            current_total_notional=current,
            reduce_only=order.reduce_only,
        )
        if not check.allowed:
            self.log_fn(f"[LIVE-EXECUTOR] REJECTED: {check.reason}")
            return self._fill(order, ref_price, 0.0, "rejected", error=check.reason)

        try:
            decimals = self._sz_decimals(order.coin)
            rounded_size = self._round_down(order.size, decimals)
            if rounded_size <= 0:
                raise ValueError(
                    f"rounded size {rounded_size} is zero for {order.coin} "
                    f"(raw={order.size}, szDecimals={decimals})"
                )
        except Exception as e:
            self.log_fn(f"[LIVE-EXECUTOR] ERROR sizing {order.coin}: {e}")
            return self._fill(order, ref_price, 0.0, "error", error=str(e))

        ex_cfg = self.cfg.raw.get("executor", {})
        prefer_maker = bool(ex_cfg.get("prefer_maker", False))
        maker_timeout = int(ex_cfg.get("maker_timeout_sec", 30))

        # HIP-3 pairs (xyz:*) have thin books — maker orders rest for
        # minutes without filling. Force taker for them.
        skip_maker = order.coin.startswith("xyz:")

        maker_sz, maker_px, oid = 0.0, 0.0, ""
        if prefer_maker and not skip_maker:
            maker = self._try_maker(order, rounded_size, maker_timeout)
            if maker is not None:
                if maker.status == "unknown":
                    self.log_fn(
                        f"[LIVE-EXECUTOR] maker outcome unknown for {order.coin} "
                        f"oid={maker.order_id}; not sending taker order"
                    )
                    return maker
                maker_sz, maker_px, oid = maker.size, maker.price, maker.order_id
                if maker.status == "filled":
                    if not order.reduce_only:
                        self.safeguards.increment_daily_order_count()
                    return maker

        remaining = self._round_down(rounded_size - maker_sz, decimals)
        taker_sz, taker_px, taker_oid, err = 0.0, 0.0, "", ""
        if remaining > 0:
            taker_sz, taker_px, taker_oid, err = self._taker(order, remaining, ref_price)

        total = maker_sz + taker_sz
        avg_px = (
            (maker_sz * maker_px + taker_sz * taker_px) / total
            if total > 0 else ref_price
        )
        if total >= rounded_size * 0.95:
            status = "filled"
        elif total > 0:
            status = "partial"
        else:
            status = "error"
        fill = self._fill(
            order, avg_px, total, status,
            oid=taker_oid or oid,
            error=err if status != "filled" else "",
        )

        # The daily limit throttles new risk; closes don't consume it.
        if total > 0 and not order.reduce_only:
            self.safeguards.increment_daily_order_count()
        label = {"filled": "TAKER", "partial": "PARTIAL"}.get(status, "FAIL")
        self.log_fn(
            f"[LIVE-EXECUTOR] {label} {fill.side.upper():4s} "
            f"{fill.size:.6f}/{rounded_size:.6f} {fill.coin:6s} @ {fill.price:.6f}  "
            f"oid={fill.order_id}  tag={fill.tag}"
            f"{'  reduce_only' if order.reduce_only else ''}"
            f"{'  ERROR: ' + fill.error if fill.error else ''}"
        )
        return fill

    def _taker(self, order: Order, size: float,
               ref_price: float) -> tuple[float, float, str, str]:
        """IOC order. Returns (filled_size, avg_px, oid, error)."""
        is_buy = order.side == "buy"
        slippage = float(
            self.cfg.raw.get("executor", {}).get("slippage_tolerance", 0.03)
        )
        try:
            px = self.exchange._slippage_price(order.coin, is_buy, slippage, ref_price)
            result = self.exchange.order(
                order.coin, is_buy, size, px,
                {"limit": {"tif": "Ioc"}},
                reduce_only=order.reduce_only,
            )
        except Exception as e:
            return 0.0, 0.0, "", f"{type(e).__name__}: {e}"
        return self._parse_ioc(result)

    @staticmethod
    def _parse_ioc(result) -> tuple[float, float, str, str]:
        if not isinstance(result, dict):
            return 0.0, 0.0, "", f"bad response: {result!r}"
        if result.get("status") != "ok":
            return 0.0, 0.0, "", f"top-level status={result.get('status')}: {result.get('response')}"
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses or not isinstance(statuses[0], dict):
            return 0.0, 0.0, "", f"no usable status: {statuses!r}"
        first = statuses[0]
        if "filled" in first:
            f = first["filled"]
            return (float(f.get("totalSz", 0)), float(f.get("avgPx", 0)),
                    str(f.get("oid", "")), "")
        if "error" in first:
            return 0.0, 0.0, "", str(first["error"])
        if "resting" in first:
            return 0.0, 0.0, str(first["resting"].get("oid", "")), "unexpected resting IOC order"
        return 0.0, 0.0, "", f"unknown status: {first}"

    def _filled_by_oid(self, oid: str) -> tuple[float, float] | None:
        """(filled_size, avg_px) from the user's fills, or None if unavailable."""
        try:
            fills = self.info.user_fills(self.wallet_address)
        except Exception as e:
            self.log_fn(f"[LIVE-EXECUTOR] fill query failed for oid={oid}: {e}")
            return None
        mine = [f for f in fills if str(f.get("oid")) == str(oid)]
        size = sum(float(f["sz"]) for f in mine)
        if size <= 0:
            return 0.0, 0.0
        return size, sum(float(f["sz"]) * float(f["px"]) for f in mine) / size

    # ---------------- maker order helper ----------------
    def _try_maker(self, order: Order, rounded_size: float,
                   timeout_sec: int) -> Fill | None:
        """
        Try a post-only limit order at the best bid/ask.

        Returns None if no maker order was placed (caller sends the full size
        as taker). Otherwise returns a Fill whose size is the quantity the
        maker order actually filled; status "unknown" means the filled
        quantity could not be determined and nothing more should be sent.
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
            maker_px = best_bid if is_buy else best_ask
            self.log_fn(
                f"[LIVE-EXECUTOR] maker attempt: {order.side.upper()} "
                f"{rounded_size:.6f} {order.coin} @ {maker_px:.6f} "
                f"(bid={best_bid:.4f} ask={best_ask:.4f})"
            )
            maker_result = self.exchange.order(
                order.coin, is_buy, rounded_size, maker_px,
                {"limit": {"tif": "Alo"}},
                reduce_only=order.reduce_only,
            )
        except Exception as e:
            self.log_fn(f"[LIVE-EXECUTOR] maker error: {e}")
            return None

        if not isinstance(maker_result, dict) or maker_result.get("status") != "ok":
            self.log_fn(f"[LIVE-EXECUTOR] maker not accepted: {maker_result!r}")
            return None
        statuses = maker_result.get("response", {}).get("data", {}).get("statuses", [])
        first = statuses[0] if statuses and isinstance(statuses[0], dict) else {}
        if "error" in first:
            self.log_fn(f"[LIVE-EXECUTOR] maker rejected: {first['error']}")
            return None
        if "filled" in first:
            f = first["filled"]
            sz = float(f.get("totalSz", rounded_size))
            px = float(f.get("avgPx", maker_px))
            status = "filled" if sz >= rounded_size * 0.95 else "partial"
            return self._fill(order, px, sz, status, oid=str(f.get("oid", "")))
        if "resting" not in first:
            return self._fill(order, maker_px, 0.0, "unknown",
                              error=f"unrecognized maker response: {first}")
        oid = str(first["resting"].get("oid", ""))

        poll_interval = 5
        polls = max(1, timeout_sec // poll_interval)
        for _ in range(polls):
            time.sleep(poll_interval)
            try:
                order_info = self.info.query_order_by_oid(
                    self.wallet_address, int(oid),
                )
                inner = order_info.get("order", order_info) if isinstance(order_info, dict) else {}
                status = inner.get("status", "")
                if status == "filled":
                    got = self._filled_by_oid(oid)
                    sz, px = got if got and got[0] > 0 else (rounded_size, maker_px)
                    self.log_fn(
                        f"[LIVE-EXECUTOR] MAKER FILLED {order.side.upper()} "
                        f"{sz:.6f} {order.coin} @ {px:.6f} oid={oid}"
                    )
                    return self._fill(order, px, sz, "filled", oid=oid)
                if status in ("canceled", "rejected", "marginCanceled"):
                    self.log_fn(f"[LIVE-EXECUTOR] maker order {status}: oid={oid}")
                    break
            except Exception as e:
                self.log_fn(f"[LIVE-EXECUTOR] status check error: {e}")
                break

        try:
            self.exchange.cancel(order.coin, int(oid))
            self.log_fn(f"[LIVE-EXECUTOR] maker cancelled oid={oid}")
        except Exception as e:
            self.log_fn(f"[LIVE-EXECUTOR] cancel error oid={oid}: {e}")

        got = self._filled_by_oid(oid)
        if got is None:
            return self._fill(order, maker_px, 0.0, "unknown", oid=oid,
                              error="maker fill quantity unknown after cancel")
        sz, px = got
        if sz >= rounded_size * 0.95:
            status = "filled"
        elif sz > 0:
            status = "partial"
        else:
            status = "none"
        if sz > 0:
            self.log_fn(
                f"[LIVE-EXECUTOR] maker filled {sz:.6f}/{rounded_size:.6f} "
                f"{order.coin} before cancel oid={oid}"
            )
        return self._fill(order, px or maker_px, sz, status, oid=oid)


def build_executor(cfg, log_fn=print) -> BaseExecutor:
    mode = cfg.raw.get("executor", {}).get("mode", "dry_run")
    if mode == "dry_run":
        return DryRunExecutor(cfg, log_fn=log_fn)
    elif mode == "live":
        return LiveExecutor(cfg, log_fn=log_fn)
    raise ValueError(f"Unknown executor mode: {mode}")
