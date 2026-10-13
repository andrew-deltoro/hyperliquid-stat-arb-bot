"""Main cycle: generate signals, manage positions, log everything."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

from ..data import ohlcv
from ..data.ohlcv import aligned_close_prices
from ..data.ohlcv import fetch_candles
from .config import load_config
from .signals import generate_signal
from .positions import PositionManager
from .executor import _dex_of, build_executor
from . import journal
from .leverage import TradeDistribution, size_trade
from .portfolio import calibration_for_pair, load_portfolio_stats
from .scaler import PositionScaler
from .exclusions import asset_exclusions, entry_block_reason, pair_health_blocks



def _log(msg: str, fh=None):
    print(msg)
    if fh is not None:
        fh.write(msg + "\n")
        fh.flush()

def _get_btc_vol_24h() -> float | None:
    """Annualized 24h realized vol of BTC, or None if it can't be computed."""
    import numpy as np
    try:
        candles = fetch_candles("BTC", interval="1h", lookback_hours=48)
        closes = np.array([c.c for c in candles])
        if len(closes) < 25:
            return None
        returns = np.diff(np.log(closes))
        last_24 = returns[-24:]
        return float(last_24.std() * np.sqrt(24 * 365))
    except Exception:
        return None

def _log_to_fh(fh):
    def _fn(msg):
        _log(msg, fh)
    return _fn


def _acquire_cycle_lock():
    """Exclusive lock on the runtime dir; None if another cycle holds it."""
    from .paths import RUNTIME_DIR
    fh = open(RUNTIME_DIR / ".cycle.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def _size_for_book(sig, pm, executor, equity, account_gross, calibration,
                   cfg, rho: float | None = None) -> tuple[float, str]:
    """Kelly/stress-constrained pair notional for `sig` given the open book."""
    lev = dict(cfg.raw.get("leverage", {}) or {})
    lev.setdefault("max_concurrent_positions", cfg.risk["max_concurrent_positions"])
    if rho is not None:
        lev["pair_correlation"] = rho
    dex = _dex_of(sig.coin_a)
    dex_equity = float(equity.get(dex, 0.0))
    if dex_equity <= 0:
        return 0.0, f"no equity on dex {dex or 'main'}"
    dist = TradeDistribution(
        returns=sig.trade_returns, exp_return=sig.exp_return,
        p_win=sig.p_revert, stress_loss_frac=sig.stress_loss_frac,
        mean_bars_held=0.0,
    )
    decision = size_trade(
        dist=dist, beta=sig.beta,
        book=pm.book_exposure(dex, dex_equity),
        account_gross=max(account_gross, pm.total_open_notional()),
        account_equity=sum(equity.values()),
        maint_frac_a=executor.maintenance_frac(sig.coin_a),
        maint_frac_b=executor.maintenance_frac(sig.coin_b),
        calibration=calibration,
        cfg_lev=lev,
    )
    notional, binding = decision.notional, decision.binding

    # Never size past what the per-order safeguard will accept on either leg.
    sg = cfg.raw.get("safeguards", {}) or {}
    per_order = float(sg.get("max_notional_per_order", float("inf")))
    sg_cap = 0.98 * 2.0 * per_order / max(1.0, sig.beta)
    if sg_cap < notional:
        notional, binding = sg_cap, "safeguard_per_order"

    # Hyperliquid's minimum order value is $10 per leg (with headroom).
    min_leg = float(lev.get("min_leg_usd", 11.0))
    if notional / 2.0 * min(1.0, sig.beta) < min_leg:
        return 0.0, f"below minimum leg size ({binding})"
    detail = " ".join(f"{k}={v}" for k, v in decision.detail.items())
    return notional, (f"f*={decision.kelly_f:.2f} c={calibration:.2f} "
                      f"binding={binding} [{detail}]")


def run_cycle(config_path=None, log_path=None):
    cfg = load_config(config_path)
    ex_cfg = cfg.raw.get("executor", {}) or {}

    if ex_cfg.get("mode") == "live" and not os.getenv("HLBOT_RUNTIME_DIR", "").strip():
        raise RuntimeError(
            "live mode requires HLBOT_RUNTIME_DIR so state isn't split "
            "between the repo and the runtime directory"
        )

    lock = _acquire_cycle_lock()
    if lock is None:
        print("[CYCLE] another cycle is running on this runtime dir; exiting")
        return None

    try:
        return _run_cycle(cfg, ex_cfg, log_path)
    finally:
        lock.close()


def _run_cycle(cfg, ex_cfg, log_path):
    ohlcv.configure(ex_cfg.get("network", "mainnet"))

    if log_path is None:
        from datetime import datetime
        from .paths import LOGS_DIR
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = LOGS_DIR / f"cycle_{stamp}.log"

    lev_cfg = cfg.raw.get("leverage", {}) or {}
    kelly_mode = lev_cfg.get("mode") == "kelly"

    with open(log_path, "w") as fh:
        _log(f"=== Cycle start: {log_path.name} ===", fh)
        _log(f"Pairs: {cfg.pairs}", fh)

        scaler = None
        if not kelly_mode:
            # Compounding: scale position sizes by current balance
            scaler = PositionScaler(cfg, log_fn=_log_to_fh(fh))
            scaler.refresh()
        if cfg.position.get("scale_safeguards"):
            _log("[SAFEGUARDS] scale_safeguards is ignored: safeguard caps are "
                 "fixed account limits", fh)

        executor = build_executor(cfg, log_fn=print)

        def _dual_log(msg):
            _log(msg, fh)

        pm = PositionManager(cfg, executor=executor, log_fn=_dual_log)

        exchange_ok = pm.reconcile()
        for rev in pm.needs_review():
            _log(f"[REVIEW] {rev['pair']}: {rev.get('review_reason', '')} "
                 f"— entries on {rev['coin_a']}/{rev['coin_b']} blocked", fh)

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
        if btc_vol is None:
            _log("BTC 24h vol: unavailable — will skip entries", fh)
        else:
            vol_status = "OK" if btc_vol <= max_vol else "HIGH — will skip entries"
            _log(
                f"BTC 24h vol: {btc_vol*100:.1f}% "
                f"(threshold {max_vol*100:.0f}%) — {vol_status}",
                fh,
            )

        signals = {}
        prices = {}
        for a, b in cfg.pairs:
            pair = f"{a}/{b}"
            _log(f"[{pair}] fitting...", fh)
            try:
                _, ca, cb = aligned_close_prices(
                    a, b,
                    interval=cfg.data["interval"],
                    lookback_hours=cfg.data["lookback_hours"],
                )
                prices[pair] = (float(ca[-1]), float(cb[-1]))
                sig = generate_signal(
                    a, b, cfg,
                    kappa_prior_pop=kp,
                    kappa_prior_pop_sd=kp_sd,
                    scaler=scaler,
                    btc_vol=btc_vol,
                    prices=(ca, cb),
                )
            except Exception as e:
                _log(f"[{pair}] FAILED: {type(e).__name__}: {e}", fh)
                continue

            signals[pair] = sig
            journal.log_signal(sig)
            _log(
                f"[{pair}] beta={sig.beta:+.3f}±{sig.beta_sd:.3f} "
                f"nu_c={sig.nu_coint:.1f} "
                f"z={sig.z:+.2f} "
                f"EG_t={sig.eg_tstat:+.2f} "
                f"HL={sig.half_life_hours:.1f}h "
                f"nu_ou={sig.nu_ou:.1f} "
                f"P(win)={sig.p_revert:.3f} "
                f"E[r]={sig.exp_return*100:+.3f}% "
                f"stress={sig.stress_loss_frac*100:.2f}% "
                f"→ {sig.action}"
                f"{' (' + sig.skip_reason + ')' if sig.skip_reason else ''}",
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

            if pair in prices:
                price_a, price_b = prices[pair]
            else:
                try:
                    _, ca, cb = aligned_close_prices(
                        a, b,
                        interval=cfg.data["interval"],
                        lookback_hours=24,
                        validate=False,
                    )
                    price_a, price_b = float(ca[-1]), float(cb[-1])
                except Exception as e:
                    _log(f"[{pair}] fetch failed: {e}", fh)
                    continue

            fresh = signals.get(pair)
            result = pm.evaluate_open(
                pair, price_a, price_b,
                fresh_beta=fresh.beta if fresh else None,
                fresh_beta_sd=fresh.beta_sd if fresh else None,
                fresh_exp_return=fresh.exp_return if fresh else None,
            )

            if result["action"] == "closed":
                _log(f"[{pair}] ✅ CLOSED: {result['reason']} "
                     f"(exit spread={result['position']['exit_spread']:+.5f})",
                     fh)
            elif result["action"] == "close_failed":
                _log(f"[{pair}] ⚠ CLOSE FAILED ({result['reason']}): "
                     f"{result.get('error', '')}", fh)
            elif result["action"] == "hold":
                _log(f"[{pair}] holding: "
                     f"spread={result['current_centered_spread']:+.5f} "
                     f"z={result['z_now']:+.2f}", fh)

        _log("", fh)

        # ---------- 3. Open new positions ----------
        _log("--- Opening new positions ---", fh)
        equity = account_gross = None
        port = None
        if not exchange_ok:
            _log("Skipping entries: exchange position state unavailable", fh)
            signals = {}
        elif kelly_mode:
            equity = executor.account_equity()
            account_gross = executor.account_gross()
            if equity is None or account_gross is None:
                _log("Skipping entries: account equity/exposure unavailable", fh)
                signals = {}
            else:
                port = load_portfolio_stats(lev_cfg)
                _log(f"[LEVERAGE] equity={ {k or 'main': round(v, 2) for k, v in equity.items()} } "
                     f"gross=${account_gross:.2f} "
                     f"gross_leverage={account_gross / max(sum(equity.values()), 1e-9):.2f}x "
                     f"{port.detail}", fh)

        excluded_assets = asset_exclusions(cfg.raw)
        unhealthy_pairs = pair_health_blocks(cfg.raw)
        if excluded_assets:
            _log(
                "[ENTRY EXCLUSIONS] assets: "
                + ", ".join(f"{coin} ({reason})" for coin, reason in excluded_assets.items()),
                fh,
            )
        if unhealthy_pairs:
            _log(
                "[ENTRY EXCLUSIONS] pairs: "
                + ", ".join(f"{pair} ({reason})" for pair, reason in unhealthy_pairs.items()),
                fh,
            )

        ranked = sorted(signals.items(), key=lambda kv: -kv[1].exp_return)
        for pair, sig in ranked:
            if sig is None or sig.action != "OPEN":
                continue
            block_reason = entry_block_reason(pair, excluded_assets, unhealthy_pairs)
            if block_reason:
                _log(f"[{pair}] entry quarantined: {block_reason}", fh)
                continue
            if pm.find_open(pair) is not None:
                _log(f"[{pair}] already open", fh)
                continue
            price_a, price_b = prices[pair]

            if kelly_mode:
                c = calibration_for_pair(port, pair) if port else 0.5
                notional, why = _size_for_book(
                    sig, pm, executor, equity, account_gross, c, cfg,
                    rho=port.rho if port else None,
                )
                _log(f"[{pair}] sizing: N=${notional:.2f} c_pair={c:.2f} {why}", fh)
                if notional <= 0:
                    continue
                sig.notional = notional

            result = pm.try_open(sig, price_a, price_b)
            if result["opened"]:
                _log(f"[{pair}] ✅ OPENED "
                     f"notional=${result['position']['notional_usd']:.2f}", fh)
                if account_gross is not None:
                    p = result["position"]
                    account_gross += abs(p["leg_a_usd"]) + abs(p["leg_b_usd"])
            else:
                _log(f"[{pair}] ✗ not opened: {result['reason']}", fh)

        _log("", fh)
        _log(f"Cycle end. Open positions: {pm.open_count()}", fh)
        _log(f"Gross open notional: ${pm.total_open_notional():.2f}", fh)

    return log_path
