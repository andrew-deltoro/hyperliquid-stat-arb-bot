"""Position manager: open new, monitor existing, close on exit rules."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from .state import Position, State, load_state, save_state, now_iso, upsert_position
from .signals import Signal
from .executor import BaseExecutor, Fill, Order, _dex_of, build_executor
from . import journal
from .correlation_guard import CorrelationGuard
from .leverage import (
    BookExposure,
    exit_policy_from_cfg,
    simulate_trade_returns,
)


# A leg counts as filled when at least this fraction of the requested size
# shows up on the exchange.
FULL_FILL_FRAC = 0.95
# Live exposure below this many USD doesn't block re-entry on a coin.
DUST_THRESHOLD_USD = 5.0
# Cap on stored posterior draws per position, to keep positions.json small.
MAX_POSTERIOR_DRAWS = 256


def _subsample(values) -> list:
    """Evenly subsample posterior draws to MAX_POSTERIOR_DRAWS, rounded."""
    if values is None:
        return []
    try:
        import numpy as np

        arr = np.asarray(values, dtype=float).ravel()
    except Exception:  # noqa: BLE001
        return []
    if arr.size == 0:
        return []
    if arr.size > MAX_POSTERIOR_DRAWS:
        step = arr.size / MAX_POSTERIOR_DRAWS
        idx = (np.arange(MAX_POSTERIOR_DRAWS) * step).astype(int)
        arr = arr[idx]
    return [round(float(v), 8) for v in arr]


def _bar_hours_from_cfg(cfg) -> float:
    """Duration of one bar in hours from the data interval string."""
    interval = str((cfg.data or {}).get("interval", "1h"))
    try:
        if interval.endswith("h"):
            return float(interval[:-1])
        if interval.endswith("m"):
            return float(interval[:-1]) / 60.0
        if interval.endswith("d"):
            return float(interval[:-1]) * 24.0
    except ValueError:
        pass
    return 1.0


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _signed(side: str, size: float) -> float:
    return size if side == "buy" else -size


@dataclass
class LegResult:
    placed: bool
    delta: Optional[float]      # signed position change; None if unknown
    fill: Optional[Fill]


class PositionManager:
    def __init__(self, cfg, executor: BaseExecutor | None = None,
                 log_fn=print):
        self.cfg = cfg
        self.state: State = load_state()
        self.log_fn = log_fn
        self.executor = executor or build_executor(cfg, log_fn=log_fn)

        # Correlation guard
        risk = cfg.raw.get("risk", {}) or {}
        coin_cap = risk.get("max_coin_gross_usd")
        self.correlation_guard = CorrelationGuard(
            cluster_caps=risk.get("cluster_caps", {}) or {},
            max_coin_gross=float(coin_cap) if coin_cap is not None else None,
        )


    # ---------------- helpers ----------------
    def find_open(self, pair: str) -> Optional[dict]:
        for p in self.state.positions:
            if p["pair"] == pair and p["status"] == "open":
                return p
        return None

    def needs_review(self) -> list[dict]:
        return [p for p in self.state.positions if p["status"] == "needs_review"]

    def total_open_notional(self) -> float:
        """Gross leg notional of open positions."""
        return sum(
            abs(p["leg_a_usd"]) + abs(p["leg_b_usd"])
            for p in self.state.positions if p["status"] == "open"
        )

    def book_exposure(self, dex: str, equity: float) -> BookExposure:
        """Open book on one dex, for the stress-sizing constraint."""
        default_stress = float(
            (self.cfg.raw.get("leverage", {}) or {}).get("default_stress_loss_frac", 0.05))
        book = BookExposure(equity=equity)
        for p in self.state.positions:
            if p["status"] not in ("open", "needs_review") or "leg_a_usd" not in p:
                continue
            on_dex = False
            for leg in ("a", "b"):
                coin = p[f"coin_{leg}"]
                if _dex_of(coin) != dex:
                    continue
                on_dex = True
                g = abs(p[f"leg_{leg}_usd"])
                book.gross += g
                book.maintenance += g * self.executor.maintenance_frac(coin)
            if on_dex:
                book.stress_loss += p["notional_usd"] * float(
                    p.get("stress_loss_frac", default_stress))
        return book

    def open_count(self) -> int:
        return sum(
            1 for p in self.state.positions
            if p["status"] in ("open", "needs_review")
        )

    def _verifies(self) -> bool:
        return getattr(self.executor, "verifies_positions", False)

    def _lot(self, coin: str) -> float:
        return max(self.executor.lot_size(coin), 1e-9)

    def _leg_target(self, pos: dict, leg: str) -> float:
        """Signed quantity this position holds in leg 'a' or 'b'."""
        key = f"size_{leg}"
        if key in pos:
            return float(pos[key])
        # Positions opened before sizes were stored: rebuild the entry
        # quantity the same way try_open sized it.
        long_a = pos["direction"] == "LONG_A_SHORT_B"
        sign = (1.0 if long_a else -1.0) * (1.0 if leg == "a" else -1.0)
        return sign * abs(pos[f"leg_{leg}_usd"]) / pos[f"entry_price_{leg}"]

    @staticmethod
    def _holds(live: float, target: float) -> bool:
        return live != 0 and (live > 0) == (target > 0)

    def _execute_leg(self, coin: str, side: str, size: float, notional: float,
                     tag: str, ref_price: float,
                     reduce_only: bool = False) -> LegResult:
        """Place one order and measure the resulting position change."""
        verifies = self._verifies()
        before = self.executor.get_position(coin) if verifies else 0.0
        if verifies and before is None:
            return LegResult(placed=False, delta=None, fill=None)

        fill = self.executor.place_order(
            Order(coin=coin, side=side, size=size, notional_usd=notional,
                  tag=tag, reduce_only=reduce_only),
            ref_price=ref_price,
        )
        if not verifies:
            filled = fill.size if fill.status in ("filled", "partial") else 0.0
            return LegResult(placed=True, delta=_signed(side, filled), fill=fill)

        after = self.executor.get_position(coin)
        delta = None if after is None else after - before
        return LegResult(placed=True, delta=delta, fill=fill)

    def _unwind(self, coin: str, delta: float, ref_price: float, tag: str,
                attempts: int = 3) -> bool:
        """Reverse a measured position change. True if the coin is back."""
        for _ in range(attempts):
            if abs(delta) < self._lot(coin):
                return True
            side = "sell" if delta > 0 else "buy"
            res = self._execute_leg(
                coin, side, abs(delta), abs(delta) * ref_price, tag, ref_price,
                reduce_only=True,
            )
            if res.delta is None:
                return False
            delta += res.delta
        return abs(delta) < self._lot(coin)

    def _flag_review(self, sig: Signal, reason: str, **legs) -> dict:
        record = {
            "pair": sig.pair,
            "coin_a": sig.coin_a,
            "coin_b": sig.coin_b,
            "direction": sig.direction,
            "status": "needs_review",
            "review_reason": reason,
            "review_time_iso": now_iso(),
            **legs,
        }
        self.state.positions.append(record)
        save_state(self.state)
        self.log_fn(f"[POSITION] ⚠ NEEDS REVIEW {sig.pair}: {reason} {legs}")
        return {"opened": False, "reason": f"needs_review: {reason}"}

    # ---------------- reconcile ----------------
    def reconcile(self) -> bool:
        """
        Compare open positions with the exchange.

        Returns False (and changes nothing) if exchange state is unavailable;
        callers must not open new positions in that case.
        """
        if not self._verifies():
            return True
        live = self.executor.all_positions()
        if live is None:
            self.log_fn(
                "[RECONCILE] exchange state unavailable — no state changes; "
                "new entries blocked this cycle"
            )
            return False

        changed = False
        tracked: set[str] = set()
        for pos in self.state.positions:
            if pos["status"] not in ("open", "needs_review"):
                continue
            a, b = pos["coin_a"], pos["coin_b"]
            tracked |= {a, b}
            if pos["status"] != "open":
                continue
            has_a = self._holds(live.get(a, 0.0), self._leg_target(pos, "a"))
            has_b = self._holds(live.get(b, 0.0), self._leg_target(pos, "b"))
            if has_a and has_b:
                continue
            if not has_a and not has_b:
                pos["status"] = "closed"
                pos["exit_reason"] = "state_reconciled"
                self.log_fn(f"[RECONCILE] {pos['pair']}: both legs gone on exchange; marked closed")
            else:
                missing = a if not has_a else b
                pos["status"] = "needs_review"
                pos["review_reason"] = f"leg {missing} missing on exchange"
                self.log_fn(f"[RECONCILE] ⚠ {pos['pair']}: leg {missing} missing on exchange; needs review")
            changed = True

        orphans = sorted(set(live) - tracked)
        if orphans:
            self.log_fn(
                f"[RECONCILE] ⚠ exchange positions not tracked in state: "
                f"{ {c: live[c] for c in orphans} }"
            )
        if changed:
            save_state(self.state)
        return True

    # ---------------- open ----------------
    def try_open(self, sig: Signal, price_a: float, price_b: float) -> dict:
        """Attempt to open a position from a signal."""
        if sig.action != "OPEN":
            return {"opened": False, "reason": f"signal action = {sig.action}"}

        if self.find_open(sig.pair) is not None:
            return {"opened": False, "reason": "already open"}

        coins = {sig.coin_a, sig.coin_b}
        for p in self.needs_review():
            if coins & {p["coin_a"], p["coin_b"]}:
                return {"opened": False, "reason": f"needs_review on {p['pair']}"}

        # Defensive: verify against live exchange (state can drift)
        if self._verifies():
            for coin, px in ((sig.coin_a, price_a), (sig.coin_b, price_b)):
                live = self.executor.get_position(coin)
                if live is None:
                    return {"opened": False, "reason": f"live position unknown for {coin}"}
                # Dust residuals from partial exits shouldn't block re-entry.
                if abs(live) * px > DUST_THRESHOLD_USD:
                    self.log_fn(
                        f"[PRE-TRADE] {sig.pair}: already has live exposure "
                        f"on {coin}. Skipping to avoid duplicate."
                    )
                    return {"opened": False, "reason": f"live_exposure: {coin}"}

        if sig.beta <= 0:
            return {"opened": False, "reason": f"non-positive beta {sig.beta:.3f}"}

        if _dex_of(sig.coin_a) != _dex_of(sig.coin_b):
            return {"opened": False, "reason": "legs on different dexes"}

        if self.open_count() >= self.cfg.risk["max_concurrent_positions"]:
            return {"opened": False, "reason": "max concurrent positions"}

        new_gross = sig.notional / 2.0 * (1.0 + sig.beta)
        if self.total_open_notional() + new_gross > self.cfg.risk["max_total_notional"]:
            return {"opened": False, "reason": "max total notional"}

        # Correlation guard: prevent correlated clusters from stacking
        guard = self.correlation_guard.check(
            coin_a=sig.coin_a,
            coin_b=sig.coin_b,
            new_notional=sig.notional,
            open_positions=self.state.positions,
            beta=sig.beta,
        )
        if not guard.allowed:
            self.log_fn(f"[CORR-GUARD] {guard.reason}")
            return {"opened": False, "reason": f"correlation_guard: {guard.reason}"}
        self.log_fn(f"[CORR-GUARD] {guard.reason}")


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

        sign_a = "buy" if sig.direction == "LONG_A_SHORT_B" else "sell"
        sign_b = "sell" if sig.direction == "LONG_A_SHORT_B" else "buy"

        size_a = leg_a_usd / (price_a + 1e-12)
        size_b = leg_b_usd / (price_b + 1e-12)

        for coin in (sig.coin_a, sig.coin_b):
            if not self.executor.ensure_leverage(coin):
                return {"opened": False, "reason": f"leverage not set for {coin}"}

        # --- Place leg A ---
        res_a = self._execute_leg(sig.coin_a, sign_a, size_a, leg_a_usd,
                                  "arb_open", price_a)
        if res_a.delta is None:
            if not res_a.placed:
                return {"opened": False, "reason": f"live position unknown for {sig.coin_a}"}
            return self._flag_review(sig, f"leg A ({sig.coin_a}) outcome unknown",
                                     delta_a=None)
        if abs(res_a.delta) < size_a * FULL_FILL_FRAC:
            error = res_a.fill.error if res_a.fill else ""
            if abs(res_a.delta) > 0 and not self._unwind(
                sig.coin_a, res_a.delta, price_a, "arb_rollback"
            ):
                return self._flag_review(
                    sig, f"leg A ({sig.coin_a}) partial and unwind failed",
                    delta_a=res_a.delta,
                )
            return {
                "opened": False,
                "reason": f"leg A ({sig.coin_a}) failed: {error or 'not filled'}",
            }

        # --- Place leg B ---
        res_b = self._execute_leg(sig.coin_b, sign_b, size_b, leg_b_usd,
                                  "arb_open", price_b)
        if res_b.delta is None or abs(res_b.delta) < size_b * FULL_FILL_FRAC:
            error = res_b.fill.error if res_b.fill else ""
            self.log_fn(
                f"[POSITION] leg B ({sig.coin_b}) failed — "
                f"rolling back leg A ({sig.coin_a})"
            )
            if res_b.delta is None and res_b.placed:
                return self._flag_review(
                    sig, f"leg B ({sig.coin_b}) outcome unknown",
                    delta_a=res_a.delta, delta_b=None,
                )
            ok_b = self._unwind(sig.coin_b, res_b.delta or 0.0, price_b, "arb_rollback")
            ok_a = self._unwind(sig.coin_a, res_a.delta, price_a, "arb_rollback")
            if not (ok_a and ok_b):
                return self._flag_review(
                    sig, "leg B failed and rollback incomplete",
                    delta_a=res_a.delta, delta_b=res_b.delta,
                )
            return {
                "opened": False,
                "reason": f"leg B ({sig.coin_b}) failed: {error or 'not filled'}",
            }

        entry_a = res_a.fill.price if res_a.fill and res_a.fill.price > 0 else price_a
        entry_b = res_b.fill.price if res_b.fill and res_b.fill.price > 0 else price_b

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
            entry_price_a=entry_a,
            entry_price_b=entry_b,
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
        pos_dict["size_a"] = res_a.delta
        pos_dict["size_b"] = res_b.delta
        pos_dict["entry_size_a"] = res_a.delta
        pos_dict["entry_size_b"] = res_b.delta
        pos_dict["exp_return_at_entry"] = sig.exp_return
        pos_dict["stress_loss_frac"] = sig.stress_loss_frac
        # Finding 3.4: carry the entry posterior so the beta-drift gate can
        # evaluate the spread's posterior predictive PnL later, without a
        # fresh fit. Subsample to bound the state file size.
        pos_dict["entry_spread_draws"] = _subsample(sig.spread_for_sim)
        pos_dict["entry_kappa_draws"] = _subsample(sig.kappa_draws)
        pos_dict["entry_sigma_draws"] = _subsample(sig.sigma_draws)
        pos_dict["entry_nu_mean"] = float(sig.nu_ou)

        # --- Both legs filled: record position ---
        self.state.positions.append(pos_dict)
        save_state(self.state)
        return {"opened": True, "position": pos_dict}

    # ---------------- monitoring helpers ----------------
    def _spread_pnl_should_exit(
        self,
        pos: dict,
        current_centered: float,
        fresh_exp_return: Optional[float] = None,
        require_signal: bool = False,
    ) -> tuple[bool, dict]:
        """
        Economics gate for beta-drift exits.

        Finding 3.4 (sanity floor): if the position carries its stored entry
        posterior (spread draws, kappa, sigma, nu), simulate the REAL exit
        policy from the *current* level and exit only when the posterior
        predictive mean net return is <= 0. If the posterior is unavailable,
        fall back to the live signal's posterior predictive mean
        (`fresh_exp_return`, produced by `simulate_trade_returns` in signals).

        When `require_signal` is true and neither posterior is available, do
        not exit (used for the beta-drift path). A hard floor exits whenever
        the current level is already beyond the level the stop would have
        closed at, so a position with no posterior can always be made safe.
        """
        detail: dict = {"source": "none"}

        # 0. Preferred: the live signal's posterior predictive mean, computed
        #    by signals.simulate_trade_returns with the real exit policy.
        if fresh_exp_return is not None:
            detail = {"source": "signal_posterior", "exp_return": round(float(fresh_exp_return), 5)}
            if float(fresh_exp_return) <= 0.0:
                return True, detail
            # Positive expected return: the spread still pays. Do not exit on
            # a pure beta drift (Finding 3.2).
            return False, detail

        # 1. Position-local entry posterior: simulate the exit policy from the
        #    current level but with the ENTRY geometry as the reference. Used
        #    when the signal is unavailable.
        s0_draws = pos.get("entry_spread_draws")
        kappa = pos.get("entry_kappa_draws")
        sigma = pos.get("entry_sigma_draws")
        nu_mean = pos.get("entry_nu_mean")
        if s0_draws and kappa and sigma and nu_mean is not None:
            try:
                import numpy as np

                n = min(len(s0_draws), len(kappa), len(sigma))
                s0 = np.asarray(s0_draws[:n], dtype=float)
                # Shift the entry draws' dispersion onto the current level and
                # re-center, then hold the entry level fixed as the reference.
                s_now = s0 - s0.mean() + current_centered
                entry_ref = np.full(n, float(pos["entry_spread"]))
                policy = exit_policy_from_cfg(
                    self.cfg,
                    half_life_bars=min(
                        pos["half_life_hours"] / _bar_hours_from_cfg(self.cfg), 500.0
                    ),
                    beta=pos["beta_entry"],
                    bar_hours=_bar_hours_from_cfg(self.cfg),
                )
                dist = simulate_trade_returns(
                    current_spread=s_now,
                    kappa_post=np.asarray(kappa[:n], dtype=float),
                    sigma_post=np.asarray(sigma[:n], dtype=float),
                    nu_post=np.full(n, float(nu_mean)),
                    policy=policy,
                    seed=int((self.cfg.model or {}).get("seed", 0)),
                    spread_entry=entry_ref,
                )
                exp_ret = float(dist.exp_return)
                detail = {
                    "source": "position_posterior",
                    "exp_return": round(exp_ret, 5),
                    "p_win": round(float(dist.p_win), 3),
                    "draws": len(dist.returns),
                }
                return exp_ret <= 0.0, detail
            except Exception as exc:  # noqa: BLE001
                self.log_fn(f"[BETA-EXIT] {pos['pair']}: posterior sim failed: {exc}")
                detail = {"source": "position_posterior_failed", "error": str(exc)}

        # 2. Hard level floor: current spread already past the stop level.
        stop_spread = float(pos.get("stop_spread", 0.0))
        long_a = pos["direction"] == "LONG_A_SHORT_B"
        breached = (
            current_centered <= stop_spread if long_a else current_centered >= stop_spread
        )
        detail = {"source": "level_floor", "breached_stop": bool(breached)}
        if breached:
            return True, detail

        return False, detail

    # ---------------- monitor & close ----------------
    def evaluate_open(
        self,
        pair: str,
        price_a_now: float,
        price_b_now: float,
        fresh_beta: Optional[float] = None,
        fresh_beta_sd: Optional[float] = None,
        fresh_exp_return: Optional[float] = None,
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

        # Finding 3.1: positions opened before the autocorrelation correction
        # stored no beta sd, so the beta-drift gate had no scale to test
        # against. Backfill from the fresh posterior sd on first sight and
        # persist it immediately, so a restart cannot resurrect the blind
        # relative rule. The beta-drift exit is disabled until this exists.
        if not pos.get("beta_sd_entry") and fresh_beta_sd:
            pos["beta_sd_entry"] = float(fresh_beta_sd)
            pos["beta_sd_backfilled_iso"] = now_iso()
            upsert_position(self.state, pos)
            save_state(self.state)
            self.log_fn(
                f"[BETA-EXIT] {pos['pair']}: backfilled beta_sd_entry="
                f"{float(fresh_beta_sd):.5f} from current posterior"
            )

        # Helper closure: always passes fresh prices to _close
        def close(reason: str):
            return self._close(
                pos, current_centered, fresh_beta, reason,
                exit_price_a=price_a_now,
                exit_price_b=price_b_now,
            )

        # A previous close didn't complete: finish it before anything else.
        if pos.get("close_pending"):
            return close(pos["close_pending"])

        direction = pos["direction"]
        long_a = direction == "LONG_A_SHORT_B"

        # --- 1. TARGET, TRAILING STOP, or CROSS EXIT ---
        use_trail = self.cfg.position.get("use_trailing_stop", False)
        exit_mode = self.cfg.position.get("exit_mode", "trail")

        if exit_mode == "cross":
            # Cross exit: wait for spread to reverse past zero and reach
            # +/- cross_frac * |entry_spread| on the OPPOSITE side.
            # Validated via walk-forward: +45% mean PnL over trail (n=189).
            cross_frac = float(self.cfg.position.get("cross_exit_frac", 0.75))
            entry_spread = pos["entry_spread"]
            cross_exit_level = -cross_frac * entry_spread  # opposite-side target
            if long_a:
                if current_centered >= cross_exit_level:
                    return close("cross")
            else:
                if current_centered <= cross_exit_level:
                    return close("cross")
        elif use_trail:
            trail_frac = float(self.cfg.position.get("trail_frac", 0.25))
            activation_frac = float(self.cfg.position.get("trail_activation_frac", 0.50))
            entry_spread = pos["entry_spread"]
            entry_abs = abs(entry_spread)

            # Track best (closest-to-zero) spread seen since entry
            best_spread = pos.get("best_spread", entry_spread)
            if long_a:
                if current_centered > best_spread:
                    best_spread = current_centered
            else:
                if current_centered < best_spread:
                    best_spread = current_centered

            # Persist best_spread so it survives across cycles
            if best_spread != pos.get("best_spread"):
                pos["best_spread"] = best_spread
                save_state(self.state)

            # Activation: has spread reverted at least activation_frac of entry magnitude?
            if long_a:
                reversion = current_centered - entry_spread
            else:
                reversion = entry_spread - current_centered
            activated = reversion >= (activation_frac * entry_abs)

            if activated:
                trail_dist = trail_frac * entry_abs
                if long_a:
                    if current_centered < best_spread - trail_dist:
                        return close("trail")
                else:
                    if current_centered > best_spread + trail_dist:
                        return close("trail")
        else:
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

        # --- 4. BETA DRIFT STOP (posterior, spread-level PnL gate) ---
        # Finding 3.2: the old gate used the *relative* point-estimate drift
        # (|Δβ|/|β| > 20%) as a hard exit. Relative drift is not
        # uncertainty-aware, so a 12% move on a spuriously precise beta
        # force-closed trades mid-reversion (see cross_only_counterfactual).
        # Now beta drift is only a *trigger*: it must be statistically large
        # next to the beta posterior AND the current spread must have a
        # non-positive expected net PnL under the real exit policy. Beta is a
        # hedge ratio, not a forecast, so a drift that hasn't yet damaged the
        # economics of the position is not a reason to exit.
        #
        # Finding 3.1: if a position has no stored beta sd (pre-correction
        # entry), the relative rule is disabled entirely (no sd -> no gate).
        if fresh_beta is not None:
            beta_entry = pos["beta_entry"]
            beta_sd = max(pos.get("beta_sd_entry", 0.0), fresh_beta_sd or 0.0)
            min_z = float(self.cfg.position.get("beta_shift_min_posterior_sd", 2.0))
            # Trigger requires a genuine posterior sigma to scale against.
            posterior_drift = (
                abs(fresh_beta - beta_entry) / beta_sd if beta_sd > 0 else 0.0
            )
            if beta_sd > 0 and posterior_drift >= min_z:
                # Route through the shared economics gate. Required (not held)
                # so unbackfilled positions are made safe on their next beta
                # drift instead of being force-closed.
                should_exit, _detail = self._spread_pnl_should_exit(
                    pos, current_centered, fresh_exp_return, require_signal=False
                )
                if should_exit:
                    return close("beta_shift")

        # Still open
        z_now = current_centered / (pos["entry_sigma_raw"] + 1e-9)
        return {
            "action": "hold",
            "position": pos,
            "current_centered_spread": current_centered,
            "z_now": z_now,
        }

    def _close_leg(self, pos: dict, leg: str, price: float,
                   reason: str) -> tuple[bool, Optional[float], str]:
        """
        Reduce one leg toward flat. Returns (done, avg_exit_price, problem);
        problem is non-empty when the leg needs human review.
        """
        coin = pos[f"coin_{leg}"]
        remaining = self._leg_target(pos, leg)
        lot = self._lot(coin)
        if abs(remaining) < lot:
            return True, None, ""

        size = abs(remaining)
        if self._verifies():
            live = self.executor.get_position(coin)
            if live is None:
                return False, None, ""
            if not self._holds(live, remaining):
                return False, None, f"leg {coin} expected {remaining:+.6f}, exchange has {live:+.6f}"
            # Never ask for more than the exchange holds; the remainder of an
            # unrounded legacy size is below one lot.
            size = min(size, abs(live))

        side = "sell" if remaining > 0 else "buy"
        res = self._execute_leg(
            coin, side, size, size * price, f"arb_close_{reason}", price,
            reduce_only=True,
        )
        if res.delta is None:
            return False, None, ""

        new_remaining = remaining + res.delta
        pos[f"size_{leg}"] = 0.0 if abs(new_remaining) < lot else new_remaining
        exit_px = res.fill.price if res.fill and res.fill.size > 0 else None
        return abs(new_remaining) < lot, exit_px, ""

    def _close(
        self,
        pos: dict,
        exit_spread: float,
        exit_beta: Optional[float],
        reason: str,
        exit_price_a: Optional[float] = None,
        exit_price_b: Optional[float] = None,
    ) -> dict:
        if exit_price_a is not None and exit_price_b is not None:
            done_a, px_a, problem_a = self._close_leg(pos, "a", exit_price_a, reason)
            done_b, px_b, problem_b = self._close_leg(pos, "b", exit_price_b, reason)
            if px_a:
                pos["exit_fill_price_a"] = px_a
            if px_b:
                pos["exit_fill_price_b"] = px_b
            exit_price_a = pos.get("exit_fill_price_a", exit_price_a)
            exit_price_b = pos.get("exit_fill_price_b", exit_price_b)

            problem = "; ".join(p for p in (problem_a, problem_b) if p)
            if problem:
                pos["status"] = "needs_review"
                pos["review_reason"] = problem
                save_state(self.state)
                self.log_fn(f"[POSITION] ⚠ NEEDS REVIEW {pos['pair']}: {problem}")
                return {"action": "close_failed", "reason": reason,
                        "error": problem, "position": pos}
            if not (done_a and done_b):
                pos["close_pending"] = reason
                save_state(self.state)
                self.log_fn(
                    f"[POSITION] {pos['pair']}: close incomplete "
                    f"(size_a={pos.get('size_a')}, size_b={pos.get('size_b')}); "
                    f"will retry next cycle"
                )
                return {"action": "close_failed", "reason": reason,
                        "error": "close incomplete", "position": pos}

        pos.pop("close_pending", None)
        pos["status"] = "closed"
        pos["exit_reason"] = reason
        pos["exit_spread"] = exit_spread
        pos["exit_beta"] = exit_beta
        pos["exit_time_iso"] = now_iso()

        # Journal the completed trade (price PnL + account funding on both coins)
        if exit_price_a is not None and exit_price_b is not None:
            funding_usd = 0.0
            info = getattr(self.executor, "info", None)
            wallet = getattr(self.executor, "wallet_address", "") or ""
            if info is not None and wallet:
                try:
                    from .funding import position_funding_usd
                    funding_usd = position_funding_usd(info, wallet, pos)
                except Exception as e:
                    self.log_fn(f"[FUNDING] {pos['pair']}: query failed: {e}")
            pos["funding_usd"] = funding_usd
            pnl_usd, pnl_pct = journal.compute_pnl(
                pos, exit_price_a, exit_price_b, funding_usd=funding_usd,
            )
            pos["pnl_usd"] = pnl_usd
            pos["pnl_pct"] = pnl_pct
            journal.log_trade(pos, exit_price_a, exit_price_b,
                              funding_usd=funding_usd)
            if funding_usd:
                self.log_fn(f"[FUNDING] {pos['pair']}: {funding_usd:+.4f} USDC")

        save_state(self.state)
        return {"action": "closed", "reason": reason, "position": pos}
