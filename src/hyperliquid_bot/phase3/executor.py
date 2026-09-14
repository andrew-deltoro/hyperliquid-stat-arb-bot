"""
Executor: places orders (dry-run or live).

Interface is identical between modes. In dry-run, orders are logged
but not sent to Hyperliquid. In live, they use the hyperliquid SDK.

For now, live mode is a stub — we flip it on after 2+ weeks of dry-run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Order:
    coin: str
    side: str           # "buy" or "sell"
    size: float         # in coin units
    notional_usd: float
    tag: str            # e.g. "arb_pair_long", "arb_pair_short", "close"


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


class BaseExecutor:
    def __init__(self, cfg):
        self.cfg = cfg

    def place_order(self, order: Order, ref_price: float) -> Fill:
        raise NotImplementedError

    def close_all(self, positions: list) -> list:
        raise NotImplementedError


class DryRunExecutor(BaseExecutor):
    """Logs orders and returns synthetic fills at the reference price."""

    def __init__(self, cfg, log_fn=print):
        super().__init__(cfg)
        self.log_fn = log_fn

    def place_order(self, order: Order, ref_price: float) -> Fill:
        fill = Fill(
            coin=order.coin,
            side=order.side,
            size=order.size,
            price=ref_price,
            notional_usd=order.notional_usd,
            tag=order.tag,
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
            simulated=True,
        )
        self.log_fn(
            f"[DRY-RUN] {fill.side.upper():4s} {fill.size:.6f} "
            f"{fill.coin:6s} @ {fill.price:.6f}  "
            f"(≈ ${fill.notional_usd:.2f})  tag={fill.tag}"
        )
        return fill

    def close_all(self, positions: list) -> list:
        # In dry-run, "closing" is just a log; the position manager handles
        # the state change.
        fills = []
        for pos in positions:
            self.log_fn(f"[DRY-RUN] CLOSE {pos['pair']}")
        return fills


class LiveExecutor(BaseExecutor):
    """
    Real Hyperliquid order placement.

    WARNING: Not implemented yet. Only enable this after:
      1. Two weeks of consistent dry-run P&L
      2. Testnet trading validated
      3. Adequate risk controls in place

    To implement, install: pip install hyperliquid-python-sdk
    Then fill in place_order() and close_all() using the SDK.
    """

    def __init__(self, cfg, log_fn=print):
        super().__init__(cfg)
        self.log_fn = log_fn
        pk_env = cfg.raw["executor"]["private_key_env"]
        self.private_key = os.getenv(pk_env)
        if not self.private_key:
            raise RuntimeError(
                f"Live executor requires env var {pk_env} to be set"
            )
        # TODO: import and initialize hyperliquid SDK Exchange client
        # from hyperliquid.exchange import Exchange
        # from hyperliquid.info import Info
        # self.info = Info(base_url=..., skip_ws=True)
        # self.exchange = Exchange(wallet, base_url=...)
        raise NotImplementedError(
            "Live execution not yet implemented. Use executor.mode='dry_run'."
        )

    def place_order(self, order: Order, ref_price: float) -> Fill:
        raise NotImplementedError

    def close_all(self, positions: list) -> list:
        raise NotImplementedError


def build_executor(cfg, log_fn=print) -> BaseExecutor:
    mode = cfg.raw.get("executor", {}).get("mode", "dry_run")
    if mode == "dry_run":
        return DryRunExecutor(cfg, log_fn=log_fn)
    elif mode == "live":
        return LiveExecutor(cfg, log_fn=log_fn)
    else:
        raise ValueError(f"Unknown executor mode: {mode}")