"""Position manager: open new, monitor existing, close on exit rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .state import Position, State, load_state, save_state, now_iso
from .signals import Signal
from .executor import BaseExecutor, Order, build_executor
from . import journal


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class PositionManager:
    def __init__(self, cfg, executor: BaseExecutor | None = None,
                 log_fn=print):
        self.cfg = cfg
        self.state: State = load_state()
        self.log_fn = log_fn
        self.executor = executor or build_executor(cfg, log_fn=log_fn)

    # ---------------- helpers ----------------
    def find_open(self, pair: str) -> Optional[dict]:
        for p in self.state.positions:
            if p["pair"] == pair and p["status"] == "open":
                return p
        return None

    def total_open_notional(self) -> float:
        return sum(
            p["notional_usd"] for p in self.state.positions
            if p["status"] == "open"
        )

    def open_count(self) -> int:
        return sum(1 for p in self.state.positions if p["status"] == "open")

    # ---------------- open ----------------
    def try_open(self, sig: Signal, price_a: float, price_b: float) -> dict:
        """Attempt to open a position from a signal."""
        if sig.action != "OPEN":
            return {"opened": False, "reason": f"signal action = {sig.action}"}

        if self.find_open(sig.pair) is not None:
            return {"opened": False, "reason": "already open"}

        if self.open_count() >= self.cfg.risk["max_concurrent_positions"]:
            return {"opened": False, "reason": "max concurrent positions"}

        if self.total_open_notional() + sig.notional > self.cfg.risk["max_total_notional"]:
            return {"opened": False, "reason": "max total notional"}

        # Compute exit levels
        target_frac = self.cfg.position["target_frac"]
        stop_mult = self.cfg.position["stop_mult"]
        hl = sig.half_life_hours

        entry_spread = sig.current_spread
        target_spread = target_frac * entry_spread
        stop_spread = stop_mult * entry_spread

        time_stop = datetime.now(timezone.utc) + timedelta(
            hours=hl * self.cfg.position["exit_horizon_mult"]
        )

        notional = sig.notional
        leg_a_usd = notional / 2.0
        leg_b_usd = (notional / 2.0) * sig.beta

        pos = Position(
            pair=sig.pair,
            coin_a=sig.coin_a,
            coin_b=sig.coin_b,
            direction=sig.direction,
            beta_entry=sig.beta,
            entry_alpha=sig.alpha,
            entry_mean_raw=sig.mean_raw,
            entry_sigma_raw=sig.sigma_raw,
            entry_spread=entry_spread,
            entry_price_a=price_a,
            entry_price_b=price_b,
            entry_time_iso=now_iso(),
            half_life_hours=hl,
            target_spread=target_spread,
            stop_spread=stop_spread,
            time_stop_iso=time_stop.isoformat(),
            notional_usd=notional,
            leg_a_usd=leg_a_usd,
            leg_b_usd=leg_b_usd,
            status="open",
        )

        pos_dict = pos.__dict__
        pos_dict["p_revert_at_entry"] = sig.p_revert
        pos_dict["beta_sd_entry"] = sig.beta_sd

        sign_a = "buy" if sig.direction == "LONG_A_SHORT_B" else "sell"
        sign_b = "sell" if sig.direction == "LONG_A_SHORT_B" else "buy"

        size_a = leg_a_usd / (price_a + 1e-12)
        size_b = leg_b_usd / (price_b + 1e-12)

        # --- Place leg A ---
        fill_a = self.executor.place_order(
            Order(coin=sig.coin_a, side=sign_a, size=size_a,
                  notional_usd=leg_a_usd, tag="arb_open"),
            ref_price=price_a,
        )
        if fill_a.status != "filled":
            return {
                "opened": False,
                "reason": f"leg A ({sig.coin_a}) failed: {fill_a.error}",
            }

        # --- Place leg B ---
        fill_b = self.executor.place_order(
            Order(coin=sig.coin_b, side=sign_b, size=size_b,
                  notional_usd=leg_b_usd, tag="arb_open"),
            ref_price=price_b,
        )
        if fill_b.status != "filled":
            # Roll back leg A: place a closing order
            self.log_fn(
                f"[POSITION] leg B ({sig.coin_b}) failed — "
                f"rolling back leg A ({sig.coin_a})"
            )
            close_side_a = "sell" if sign_a == "buy" else "buy"
            self.executor.place_order(
                Order(coin=sig.coin_a, side=close_side_a,
                      size=size_a, notional_usd=leg_a_usd,
                      tag="arb_rollback"),
                ref_price=price_a,
            )
            return {
                "opened": False,
                "reason": f"leg B ({sig.coin_b}) failed: {fill_b.error}",
            }

        # --- Both legs filled: record position ---
        self.state.positions.append(pos_dict)
        save_state(self.state)
        return {"opened": True, "position": pos_dict}

    # ---------------- monitor & close ----------------
    def evaluate_open(
        self,
        pair: str,
        price_a_now: float,
        price_b_now: float,
        fresh_beta: Optional[float] = None,
    ) -> dict:
        """Re-evaluate an open position in the entry's coordinate frame."""
        pos = self.find_open(pair)
        if pos is None:
            return {"action": "no_open_position"}

        import math

        # Current spread, in ENTRY coordinates
        raw_now = (
            math.log(price_a_now)
            - pos["entry_alpha"]
            - pos["beta_entry"] * math.log(price_b_now)
        )
        current_centered = raw_now - pos["entry_mean_raw"]

        # Helper closure: always passes fresh prices to _close
        def close(reason: str):
            return self._close(
                pos, current_centered, fresh_beta, reason,
                exit_price_a=price_a_now,
                exit_price_b=price_b_now,
            )

        direction = pos["direction"]
        long_a = direction == "LONG_A_SHORT_B"

        # --- 1. TARGET ---
        target_spread = pos["target_spread"]
        if long_a:
            if current_centered >= target_spread:
                return close("target")
        else:
            if current_centered <= target_spread:
                return close("target")

        # --- 2. DIVERGENCE STOP ---
        stop_spread = pos["stop_spread"]
        if long_a:
            if current_centered <= stop_spread:
                return close("divergence_stop")
        else:
            if current_centered >= stop_spread:
                return close("divergence_stop")

        # --- 3. TIME STOP ---
        if datetime.now(timezone.utc) >= _parse_iso(pos["time_stop_iso"]):
            return close("time_stop")

        # --- 4. BETA DRIFT STOP (posterior-aware) ---
        # Only exit if the fresh beta is BOTH statistically improbable
        # (>2 posterior sds from entry) AND economically large (>20% drift).
        if fresh_beta is not None:
            beta_entry = pos["beta_entry"]
            beta_sd = pos.get("beta_sd_entry", 0.0)
            rel_drift = abs(fresh_beta - beta_entry) / (abs(beta_entry) + 1e-9)
            posterior_drift = abs(fresh_beta - beta_entry) / (beta_sd + 1e-9)
            if rel_drift > 0.20 and posterior_drift > 2.0:
                return close("beta_shift")

        # Still open
        z_now = current_centered / (pos["entry_sigma_raw"] + 1e-9)
        return {
            "action": "hold",
            "position": pos,
            "current_centered_spread": current_centered,
            "z_now": z_now,
        }

    def _close(
        self,
        pos: dict,
        exit_spread: float,
        exit_beta: Optional[float],
        reason: str,
        exit_price_a: Optional[float] = None,
        exit_price_b: Optional[float] = None,
    ) -> dict:
        # Place close orders first (dry-run just logs)
        if exit_price_a is not None and exit_price_b is not None:
            sign_a = "sell" if pos["direction"] == "LONG_A_SHORT_B" else "buy"
            sign_b = "buy" if pos["direction"] == "LONG_A_SHORT_B" else "sell"
            self.executor.place_order(
                Order(coin=pos["coin_a"], side=sign_a,
                      size=pos["leg_a_usd"] / (exit_price_a + 1e-12),
                      notional_usd=pos["leg_a_usd"],
                      tag=f"arb_close_{reason}"),
                ref_price=exit_price_a,
            )
            self.executor.place_order(
                Order(coin=pos["coin_b"], side=sign_b,
                      size=pos["leg_b_usd"] / (exit_price_b + 1e-12),
                      notional_usd=pos["leg_b_usd"],
                      tag=f"arb_close_{reason}"),
                ref_price=exit_price_b,
            )

        pos["status"] = "closed"
        pos["exit_reason"] = reason
        pos["exit_spread"] = exit_spread
        pos["exit_beta"] = exit_beta
        pos["exit_time_iso"] = now_iso()

        # Journal the completed trade
        if exit_price_a is not None and exit_price_b is not None:
            journal.log_trade(pos, exit_price_a, exit_price_b)
            pnl_usd, pnl_pct = journal.compute_pnl(
                pos, exit_price_a, exit_price_b,
            )
            pos["pnl_usd"] = pnl_usd
            pos["pnl_pct"] = pnl_pct

        save_state(self.state)
        return {"action": "closed", "reason": reason, "position": pos}
