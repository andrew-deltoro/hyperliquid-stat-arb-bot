"""Main cycle: generate signals, manage positions, log everything."""
from __future__ import annotations

from pathlib import Path

from ..data.ohlcv import aligned_close_prices
from ..data.ohlcv import fetch_candles
from .config import load_config
from .signals import generate_signal
from .positions import PositionManager
from .executor import build_executor
from . import journal
from .scaler import PositionScaler



def _log(msg: str, fh=None):
    print(msg)
    if fh is not None:
        fh.write(msg + "\n")
        fh.flush()

def _get_btc_vol_24h() -> float:
    """Compute annualized 24h realized vol of BTC."""
    import numpy as np
    try:
        candles = fetch_candles("BTC", interval="1h", lookback_hours=48)
        closes = np.array([c.c for c in candles])
        if len(closes) < 25:
            return 0.0
        returns = np.diff(np.log(closes))
        last_24 = returns[-24:]
        return float(last_24.std() * np.sqrt(24 * 365))
    except Exception:
        return 0.0

def _log_to_fh(fh):
    def _fn(msg):
        _log(msg, fh)
    return _fn


def run_cycle(config_path=None, log_path=None):
    cfg = load_config(config_path)

    if log_path is None:
        from datetime import datetime
        from .paths import LOGS_DIR
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = LOGS_DIR / f"cycle_{stamp}.log"

    with open(log_path, "w") as fh:
        _log(f"=== Cycle start: {log_path.name} ===", fh)
        _log(f"Pairs: {cfg.pairs}", fh)

        # Compounding: scale position sizes by current balance
        scaler = PositionScaler(cfg, log_fn=_log_to_fh(fh))
        scale_result = scaler.refresh()

        # Scale safeguards only when balance GROWS (scale > 1).
        # When balance shrinks, notional is already scaled down;
        # we don't need to tighten the safeguard further.
        if scale_result.enabled and cfg.position.get("scale_safeguards", True):
            sg = cfg.raw.get("safeguards", {})
            safeguard_scale = max(1.0, scale_result.scale)
            sg["max_notional_per_order"] = (
                sg.get("max_notional_per_order", 100.0) * safeguard_scale
            )
            sg["max_total_exposure_usd"] = (
                sg.get("max_total_exposure_usd", 500.0) * safeguard_scale
            )
            _log(
                f"[SCALER] safeguard_scale={safeguard_scale:.3f} → "
                f"${sg['max_notional_per_order']:.2f}/order, "
                f"${sg['max_total_exposure_usd']:.2f} exposure",
                fh,
            )


        executor = build_executor(cfg, log_fn=print)

        def _dual_log(msg):
            _log(msg, fh)

        pm = PositionManager(cfg, executor=executor, log_fn=_dual_log)

        # Reconcile state with exchange reality: close state entries for
        # positions that no longer exist on the exchange.
        if executor is not None and hasattr(executor, "info"):
            try:
                live_state = executor.info.user_state(executor.wallet_address)
                live_coins = {
                    p["position"]["coin"]
                    for p in live_state.get("assetPositions", [])
                    if float(p["position"].get("szi", 0)) != 0
                }
                removed = []
                for pos in pm.state.positions:
                    if pos["status"] != "open":
                        continue
                    if pos["coin_a"] not in live_coins and pos["coin_b"] not in live_coins:
                        pos["status"] = "closed"
                        pos["exit_reason"] = "state_reconciled"
                        removed.append(pos["pair"])
                if removed:
                    from .state import save_state
                    save_state(pm.state)
                    _log(f"[RECONCILE] Removed stale: {removed}", fh)
            except Exception as e:
                _log(f"[RECONCILE] skipped: {e}", fh)


        _log(f"Open positions before cycle: {pm.open_count()}", fh)

        kp = cfg.ou_prior["kappa_pop"]
        kp_sd = cfg.ou_prior["kappa_pop_sd"]
        _log(f"OU prior: kappa_pop={kp:.4f} ± {kp_sd:.4f}", fh)
        _log("", fh)

        # ---------- 1. Generate signals ----------
        _log("--- Generating signals ---", fh)

        # Compute BTC vol for regime filter
        btc_vol = _get_btc_vol_24h()
        max_vol = cfg.signal.get("max_btc_vol", 1.0)
        vol_status = "OK" if btc_vol <= max_vol else "HIGH — will skip entries"
        _log(
            f"BTC 24h vol: {btc_vol*100:.1f}% "
            f"(threshold {max_vol*100:.0f}%) — {vol_status}",
            fh,
        )

        signals = {}
        for a, b in cfg.pairs:
            pair = f"{a}/{b}"
            _log(f"[{pair}] fitting...", fh)
            try:
                sig = generate_signal(
                    a, b, cfg,
                    kappa_prior_pop=kp,
                    kappa_prior_pop_sd=kp_sd,
                    scaler=scaler,
                    btc_vol=btc_vol,
                )
            except Exception as e:
                _log(f"[{pair}] FAILED: {type(e).__name__}: {e}", fh)
                continue

            signals[pair] = sig
            journal.log_signal(sig)
            _log(
                f"[{pair}] beta={sig.beta:+.3f} "
                f"nu_c={sig.nu_coint:.1f} "
                f"z={sig.z:+.2f} "
                f"HL={sig.half_life_hours:.1f}h "
                f"nu_ou={sig.nu_ou:.1f} "
                f"P={sig.p_revert:.3f} "
                f"edge={sig.edge:+.3f} "
                f"notional=${sig.notional:.2f} "
                f"→ {sig.action}",
                fh,
            )

        _log("", fh)

        # ---------- 2. Evaluate existing open positions ----------
        _log("--- Evaluating open positions ---", fh)
        for pos in list(pm.state.positions):
            if pos["status"] != "open":
                continue
            pair = pos["pair"]
            a, b = pos["coin_a"], pos["coin_b"]

            try:
                _, ca, cb = aligned_close_prices(
                    a, b,
                    interval=cfg.data["interval"],
                    lookback_hours=24,
                )
                price_a, price_b = float(ca[-1]), float(cb[-1])
            except Exception as e:
                _log(f"[{pair}] fetch failed: {e}", fh)
                continue

            fresh_beta = signals[pair].beta if pair in signals else None
            result = pm.evaluate_open(pair, price_a, price_b,
                                      fresh_beta=fresh_beta)

            if result["action"] == "closed":
                _log(f"[{pair}] ✅ CLOSED: {result['reason']} "
                     f"(exit spread={result['position']['exit_spread']:+.5f})",
                     fh)
            elif result["action"] == "hold":
                _log(f"[{pair}] holding: "
                     f"spread={result['current_centered_spread']:+.5f} "
                     f"z={result['z_now']:+.2f}", fh)

        _log("", fh)

        # ---------- 3. Open new positions ----------
        _log("--- Opening new positions ---", fh)
        for pair, sig in signals.items():
            if sig.action != "OPEN":
                continue
            try:
                _, ca, cb = aligned_close_prices(
                    sig.coin_a, sig.coin_b,
                    interval=cfg.data["interval"],
                    lookback_hours=24,
                )
                price_a, price_b = ca[-1], cb[-1]
            except Exception as e:
                _log(f"[{pair}] open fetch failed: {e}", fh)
                continue

            result = pm.try_open(sig, price_a, price_b)
            if result["opened"]:
                _log(f"[{pair}] ✅ OPENED "
                     f"notional=${result['position']['notional_usd']:.2f}", fh)
            else:
                _log(f"[{pair}] ✗ not opened: {result['reason']}", fh)

        _log("", fh)
        _log(f"Cycle end. Open positions: {pm.open_count()}", fh)
        _log(f"Total notional: ${pm.total_open_notional():.2f}", fh)

    return log_path
