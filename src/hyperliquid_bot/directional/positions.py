"""
Directional position manager.

State: state/directional_positions.json
Each position = single-leg perp with entry, stop, target, time_stop.

Daily cycle:
  1. Evaluate existing positions (stop, target, time, signal flip)
  2. Open new positions from signals
  3. Persist state
"""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ..phase3.paths import STATE_DIR
from .signals import Signal, TIME_STOP_DAYS


POS_FILE = STATE_DIR / "directional_positions.json"


@dataclass
class DirPosition:
    coin: str
    direction: str              # "LONG" / "SHORT"
    entry_price: float
    entry_iso: str
    size: float                 # contracts
    notional: float             # USD
    stop_price: float
    target_price: float
    time_stop_iso: str
    status: str = "open"        # "open" / "closed"
    exit_price: float | None = None
    exit_iso: str | None = None
    exit_reason: str | None = None
    pnl_usd: float | None = None


class DirPositionManager:
    def __init__(self, cfg, executor, log_fn=print):
        self.cfg = cfg
        self.executor = executor
        self.log_fn = log_fn
        self.positions = self._load()

    def _load(self) -> list[dict]:
        if not POS_FILE.exists():
            return []
        try:
            return json.loads(POS_FILE.read_text()).get("positions", [])
        except Exception:
            return []

    def _save(self):
        POS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = POS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"positions": self.positions}, indent=2,
                                   default=str))
        tmp.replace(POS_FILE)

    def open_count(self) -> int:
        return sum(1 for p in self.positions if p["status"] == "open")

    def total_open_notional(self) -> float:
        return sum(p["notional"] for p in self.positions
                   if p["status"] == "open")

    def find_open(self, coin: str) -> dict | None:
        for p in self.positions:
            if p["coin"] == coin and p["status"] == "open":
                return p
        return None

    # ---- open ----
    def try_open(self, sig: Signal, equity: float, leverage: float,
                 max_concurrent: int, max_notional_per_pos: float) -> dict:
        if sig.action not in ("OPEN_LONG", "OPEN_SHORT"):
            return {"opened": False, "reason": f"action={sig.action}"}
        if self.find_open(sig.coin):
            return {"opened": False, "reason": "already_open"}
        if self.open_count() >= max_concurrent:
            return {"opened": False, "reason": "max_concurrent"}

        notional = min(sig.suggested_notional * leverage, max_notional_per_pos)
        if notional < 10:
            return {"opened": False, "reason": f"notional_too_small={notional:.2f}"}

        is_buy = sig.direction == "LONG"
        # HL contracts: for BTC, size = notional / price
        size_contracts = notional / sig.close

        ref_px = self.executor.get_mid(sig.coin) or sig.close
        result = self.executor.market_open(sig.coin, is_buy, size_contracts, ref_px)
        if result.get("status") != "filled":
            return {"opened": False, "reason": f"exec:{result.get('error')}"}

        entry_px = float(result.get("avg_px") or ref_px)
        size_filled = float(result.get("total_sz") or size_contracts)
        actual_notional = size_filled * entry_px

        now = datetime.now(timezone.utc)
        time_stop = now + timedelta(days=TIME_STOP_DAYS)

        pos = DirPosition(
            coin=sig.coin, direction=sig.direction,
            entry_price=entry_px, entry_iso=now.isoformat(),
            size=size_filled, notional=actual_notional,
            stop_price=sig.stop_price, target_price=sig.target_price,
            time_stop_iso=time_stop.isoformat(),
        )
        self.positions.append(asdict(pos))
        self._save()
        return {"opened": True, "position": asdict(pos)}

    # ---- evaluate ----
    def evaluate(self, coin: str, current_px: float, signal: Signal | None) -> dict:
        pos = self.find_open(coin)
        if not pos:
            return {"action": "no_position"}

        long = pos["direction"] == "LONG"
        # Stop / target check
        if long and current_px <= pos["stop_price"]:
            return self._close(pos, current_px, "stop")
        if (not long) and current_px >= pos["stop_price"]:
            return self._close(pos, current_px, "stop")
        if long and current_px >= pos["target_price"]:
            return self._close(pos, current_px, "target")
        if (not long) and current_px <= pos["target_price"]:
            return self._close(pos, current_px, "target")

        # Time stop
        if datetime.now(timezone.utc) >= datetime.fromisoformat(pos["time_stop_iso"]):
            return self._close(pos, current_px, "time")

        # Signal flip
        if signal is not None:
            if long and signal.trend == "down" and signal.momentum < -0.01:
                return self._close(pos, current_px, "signal_flip")
            if (not long) and signal.trend == "up" and signal.momentum > 0.01:
                return self._close(pos, current_px, "signal_flip")

        # Holding
        unreal = ((current_px - pos["entry_price"]) / pos["entry_price"]
                  * (1 if long else -1) * pos["notional"])
        return {"action": "hold", "unrealized_usd": unreal,
                "position": pos}

    def _close(self, pos: dict, exit_px: float, reason: str) -> dict:
        is_buy_close = pos["direction"] == "SHORT"
        ref_px = self.executor.get_mid(pos["coin"]) or exit_px
        result = self.executor.market_close(pos["coin"], pos["size"], ref_px)
        if result.get("status") != "filled":
            self.log_fn(f"[DIR-CLOSE] {pos['coin']} failed: {result.get('error')} — will retry next cycle")
            return {"action": "close_failed", "error": result.get("error")}

        fill_px = float(result.get("avg_px") or ref_px)
        long = pos["direction"] == "LONG"
        pnl = ((fill_px - pos["entry_price"]) / pos["entry_price"]
               * (1 if long else -1) * pos["notional"])

        pos["status"] = "closed"
        pos["exit_price"] = fill_px
        pos["exit_iso"] = datetime.now(timezone.utc).isoformat()
        pos["exit_reason"] = reason
        pos["pnl_usd"] = pnl
        self._save()
        return {"action": "closed", "reason": reason,
                "position": pos, "pnl_usd": pnl}
