"""
Testnet safeguards. All checks run before any order reaches the exchange.

Limits (configurable via phase3.yaml):
  - MAX_NOTIONAL_PER_ORDER:  $50   per single order
  - MAX_TOTAL_EXPOSURE_USD:  $100  across all open positions
  - MAX_DAILY_ORDERS:        20    per UTC day
  - KILL_SWITCH_FILE:        KILL  if this file exists, refuse all orders
                                   except reduce-only closes

Reduce-only orders bypass every limit so positions can always be closed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


from .paths import STATE_DIR, KILL_SWITCH_PATH, RUNTIME_DIR

REPO_ROOT = RUNTIME_DIR
DAILY_ORDERS_FILE = STATE_DIR / "daily_orders.json"

@dataclass
class SafeguardResult:
    allowed: bool
    reason: str = ""


class Safeguards:
    def __init__(
        self,
        max_notional_per_order: float = 50.0,
        max_total_exposure_usd: float = 100.0,
        max_daily_orders: int = 20,
        kill_switch_path: Path = KILL_SWITCH_PATH,
    ):
        self.max_notional_per_order = max_notional_per_order
        self.max_total_exposure_usd = max_total_exposure_usd
        self.max_daily_orders = max_daily_orders
        self.kill_switch_path = kill_switch_path

    # ---------------- kill switch ----------------
    def kill_switch_active(self) -> bool:
        return self.kill_switch_path.exists()

    # ---------------- daily order counter ----------------
    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load_daily(self) -> dict:
        if not DAILY_ORDERS_FILE.exists():
            return {"date": self._today(), "count": 0}
        with open(DAILY_ORDERS_FILE) as f:
            data = json.load(f)
        if data.get("date") != self._today():
            return {"date": self._today(), "count": 0}
        return data

    def _save_daily(self, data: dict) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DAILY_ORDERS_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(DAILY_ORDERS_FILE)

    def daily_order_count(self) -> int:
        return self._load_daily().get("count", 0)

    def increment_daily_order_count(self) -> None:
        data = self._load_daily()
        data["count"] = data.get("count", 0) + 1
        self._save_daily(data)

    # ---------------- exposure ----------------
    def check_exposure(self, current_total_notional: float) -> SafeguardResult:
        if current_total_notional > self.max_total_exposure_usd:
            return SafeguardResult(
                False,
                f"total exposure ${current_total_notional:.2f} "
                f"> ${self.max_total_exposure_usd:.2f}",
            )
        return SafeguardResult(True)

    # ---------------- main check ----------------
    def check_order(
        self,
        order_notional_usd: float,
        current_total_notional: float | None,
        reduce_only: bool = False,
    ) -> SafeguardResult:
        # Reduce-only orders can only shrink exposure, so none of the caps
        # (including the kill switch) may block them.
        if reduce_only:
            return SafeguardResult(True, "reduce_only")

        if self.kill_switch_active():
            return SafeguardResult(
                False, f"KILL switch active ({self.kill_switch_path})"
            )

        if current_total_notional is None:
            return SafeguardResult(False, "current exposure unknown")

        if order_notional_usd > self.max_notional_per_order:
            return SafeguardResult(
                False,
                f"order notional ${order_notional_usd:.2f} "
                f"> ${self.max_notional_per_order:.2f}",
            )

        projected = current_total_notional + order_notional_usd
        if projected > self.max_total_exposure_usd:
            return SafeguardResult(
                False,
                f"projected exposure ${projected:.2f} "
                f"> ${self.max_total_exposure_usd:.2f}",
            )

        daily = self.daily_order_count()
        if daily >= self.max_daily_orders:
            return SafeguardResult(
                False, f"daily order limit reached ({daily}/{self.max_daily_orders})"
            )

        return SafeguardResult(True)
