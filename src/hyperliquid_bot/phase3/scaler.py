"""
Position scaler for compounding.

If compounding is enabled, reads the current account balance and
returns a scale factor applied to position sizes AND safeguard caps.

Baseline: baseline_balance = $5,000 → scale = 1.0
Grows to: balance = $10,000 → scale = 2.0
Shrinks:  balance = $3,000 → scale = 0.6
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class ScaleResult:
    enabled: bool
    balance: float
    baseline: float
    scale: float
    reason: str = ""


class PositionScaler:
    def __init__(self, cfg, log_fn=print):
        self.cfg = cfg
        self.log_fn = log_fn
        self._cached: ScaleResult | None = None

    def refresh(self) -> ScaleResult:
        """Fetch balance and compute scale. Called once per cycle."""
        pos_cfg = self.cfg.position or {}
        enabled = bool(pos_cfg.get("compounding_enabled", False))
        baseline = float(pos_cfg.get("baseline_balance", 5000.0))

        if not enabled:
            return ScaleResult(
                enabled=False, balance=0.0, baseline=baseline,
                scale=1.0, reason="compounding_disabled",
            )

        ex_cfg = self.cfg.raw.get("executor", {})
        mode = ex_cfg.get("mode", "dry_run")
        network = ex_cfg.get("network", "testnet")

        # For dry_run, use a config-supplied fake balance
        if mode == "dry_run":
            fake_balance = float(
                pos_cfg.get("dry_run_balance", baseline)
            )
            scale = fake_balance / baseline
            result = ScaleResult(
                enabled=True, balance=fake_balance, baseline=baseline,
                scale=scale, reason="dry_run_fake_balance",
            )
            self.log_fn(
                f"[SCALER] dry_run: balance=${fake_balance:.2f} "
                f"baseline=${baseline:.2f} scale={scale:.3f}"
            )
            self._cached = result
            return result

        # For live, fetch real balance
        try:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
            base_url = (
                constants.MAINNET_API_URL if network == "mainnet"
                else constants.TESTNET_API_URL
            )
            info = Info(base_url, skip_ws=True)

            acct_env = ex_cfg.get(
                "account_address_env", "HL_ACCOUNT_ADDRESS",
            )
            wallet = os.getenv(acct_env, "").strip()
            if not wallet:
                raise RuntimeError(f"{acct_env} not set")

            state = info.user_state(wallet)
            margin = state.get("marginSummary", {})
            balance = float(margin.get("accountValue", 0))

            # Unified-mode fallback
            if balance < 1:
                spot = info.spot_user_state(wallet)
                for b in spot.get("balances", []):
                    if b.get("coin") == "USDC":
                        balance = float(b.get("total", 0))
                        break

            scale = balance / baseline if baseline > 0 else 1.0
            result = ScaleResult(
                enabled=True, balance=balance, baseline=baseline,
                scale=scale, reason="live_balance_fetched",
            )
            self.log_fn(
                f"[SCALER] live: balance=${balance:.2f} "
                f"baseline=${baseline:.2f} scale={scale:.3f}"
            )
            self._cached = result
            return result

        except Exception as e:
            self.log_fn(f"[SCALER] ERROR: {e} — defaulting scale=1.0")
            result = ScaleResult(
                enabled=True, balance=0.0, baseline=baseline,
                scale=1.0, reason=f"error: {e}",
            )
            self._cached = result
            return result

    def scale_notional(self, notional: float) -> float:
        if self._cached is None:
            return notional
        return notional * self._cached.scale
