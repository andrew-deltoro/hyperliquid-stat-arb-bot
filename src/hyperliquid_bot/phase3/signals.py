"""Generate a trade signal for one pair."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..data.ohlcv import aligned_close_prices, interval_hours
from ..stats.cointegration import (
    EG_CRITICAL,
    engle_granger_tstat,
    fit_cointegration,
)
from ..stats.error_correction import fit_error_correction
from ..stats.mean_reversion import fit_ou, position_size
from .leverage import exit_policy_from_cfg, simulate_trade_returns

def _ensemble_z(log_a, log_b, lookbacks_hours):
    """Fit cointegration at multiple lookbacks, average z-scores equally.
    Equal weights (not AIC) because AIC is not comparable across sample sizes.
    Returns (z_ensemble, alpha_avg, beta_avg, spread_series) or None.
    """
    fits = []
    for lb in lookbacks_hours:
        if lb >= len(log_a):
            continue
        la = log_a[-lb:]
        lb_ = log_b[-lb:]
        try:
            coint = fit_cointegration(
                la, lb_, draws=500, tune=500, chains=2, cores=2,
                progressbar=False,
            )
        except Exception:
            continue
        spread = la - coint.alpha_mean - coint.beta_mean * lb_
        spread_c = spread - spread.mean()
        z = float(spread_c[-1] / (spread_c.std() + 1e-9))
        fits.append({
            "z": z,
            "alpha": coint.alpha_mean,
            "beta": coint.beta_mean,
        })
    if not fits:
        return None
    z_ens = float(sum(f["z"] for f in fits) / len(fits))
    alpha_avg = float(sum(f["alpha"] for f in fits) / len(fits))
    beta_avg = float(sum(f["beta"] for f in fits) / len(fits))
    spread_final = log_a[-24*30:] - alpha_avg - beta_avg * log_b[-24*30:]
    spread_final = spread_final - spread_final.mean()
    return {
        "z": z_ens,
        "alpha": alpha_avg,
        "beta": beta_avg,
        "spread": spread_final,
        "n_lookbacks": len(fits),
    }



@dataclass
class Signal:
    pair: str
    coin_a: str
    coin_b: str
    # Cointegration fit
    beta: float
    beta_sd: float
    nu_coint: float
    converged_coint: bool
    alpha: float
    mean_raw: float
    sigma_raw: float
    # Spread
    current_spread: float        # centered (mean-subtracted)
    spread_sd: float
    z: float
    # OU fit
    kappa: float
    half_life_hours: float
    sigma_ou: float
    nu_ou: float
    converged_ou: bool
    # Trade decision
    p_revert: float              # P(net trade return > 0) under the exit policy
    edge: float                  # p_revert - 0.5
    notional: float
    direction: str               # "LONG_A_SHORT_B" / "SHORT_A_LONG_B"
    action: str                  # "OPEN" / "SKIP"
    exp_return: float = 0.0      # E[net trade return] per unit notional
    stress_loss_frac: float = 0.0
    eg_tstat: float = 0.0
    skip_reason: str = ""
    trade_returns: Optional[np.ndarray] = field(default=None, repr=False)
    # Posterior draws carried out of the joint fit so a position's exit gate
    # can re-simulate the spread later without a fresh fit (see leverage.py).
    spread_for_sim: Optional[np.ndarray] = field(default=None, repr=False)
    kappa_draws: Optional[np.ndarray] = field(default=None, repr=False)
    sigma_draws: Optional[np.ndarray] = field(default=None, repr=False)

def _should_use_kalman(pair: str, cfg) -> bool:
    """Kalman smoothing helps fast-reverting crypto pairs (walk-forward:
    1.5-1.6x total PnL at 30 bps fees) but hurts slow semis/metals pairs
    (0.25x). Gate by half_life_overrides presence -- those are exactly
    the pairs we've flagged as slow-reverting."""
    sig_obj = cfg.signal
    enabled = sig_obj.get("kalman_enabled", False)
    overrides = sig_obj.get("half_life_overrides", {}) or {}
    return bool(enabled) and pair not in overrides


def _apply_kalman(pair, coint, log_a, log_b, cfg) -> None:
    """
    Replace the NUTS (alpha, beta) with the terminal Kalman estimate if it
    passes the drift guards. log_b is centered so alpha and beta don't trade
    off against each other through the level of log_b.
    """
    import math
    from ..pairs.kalman import KalmanBeta

    beta_nuts = coint.beta_mean
    alpha_nuts = coint.alpha_mean
    m = float(np.mean(log_b))
    log_b_c = log_b - m
    resid_sd = float(np.std(coint.spread))
    try:
        kf = KalmanBeta(
            beta_init=beta_nuts,
            alpha_init=alpha_nuts + beta_nuts * m,
            q=float(cfg.signal.get("kalman_q", 1e-5)),
            r=float(cfg.signal.get("kalman_r", 1e-4)),
        )
        for i in range(len(log_a)):
            kf.update(float(log_a[i]), float(log_b_c[i]))
        beta_k = float(kf.x[0])
        alpha_c_k = float(kf.x[1])
    except Exception as e:
        print(f"[KALMAN] {pair}: failed ({e}), keeping NUTS")
        return

    alpha_k = alpha_c_k - beta_k * m
    rel_drift = abs(beta_k - beta_nuts) / (abs(beta_nuts) + 1e-9)
    abs_drift = abs(beta_k - beta_nuts)
    # Level shift of the spread at the centre of log_b, in residual sds.
    level_shift = abs(alpha_c_k - (alpha_nuts + beta_nuts * m)) / (resid_sd + 1e-9)
    max_shift = float(cfg.signal.get("kalman_alpha_max_sd", 3.0))
    if not (math.isfinite(beta_k) and math.isfinite(alpha_k)):
        print(f"[KALMAN] {pair}: non-finite result, keeping NUTS")
        return
    if rel_drift > 0.50 or abs_drift > 0.40:
        print(f"[KALMAN] {pair}: beta drift rel={rel_drift:.1%} abs={abs_drift:.3f} "
              f"({beta_nuts:.4f} -> {beta_k:.4f}), keeping NUTS")
        return
    if level_shift > max_shift:
        print(f"[KALMAN] {pair}: alpha shift {level_shift:.2f} sd > {max_shift}, keeping NUTS")
        return

    coint.beta_mean = beta_k
    coint.alpha_mean = alpha_k
    coint.spread = log_a - alpha_k - beta_k * log_b
    print(f"[KALMAN] {pair}: beta {beta_nuts:.4f} -> {beta_k:.4f}  "
          f"alpha {alpha_nuts:.4f} -> {alpha_k:.4f}")
    try:
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        from .paths import STATE_DIR as _SD
        _rec = {
            "ts": _dt.now(_tz.utc).isoformat(),
            "pair": pair,
            "beta_nuts": beta_nuts,
            "beta_kalman": beta_k,
            "alpha_nuts": alpha_nuts,
            "alpha_kalman": alpha_k,
        }
        with open(_SD / "kalman_beta_log.jsonl", "a") as _fh:
            _fh.write(_json.dumps(_rec) + "\n")
    except Exception as _log_exc:
        print(f"[KALMAN-LOG] {pair}: failed to append: {_log_exc}")


def generate_signal(
    coin_a: str,
    coin_b: str,
    cfg,
    kappa_prior_pop: Optional[float] = None,
    kappa_prior_pop_sd: Optional[float] = None,
    scaler=None,
    btc_vol: Optional[float] = None,
    prices: Optional[tuple] = None,
) -> Signal:
    """Fetch data, fit coint + OU, produce a Signal."""
    pair = f"{coin_a}/{coin_b}"
    bar_hours = interval_hours(cfg.data["interval"])

    if prices is None:
        _, close_a, close_b = aligned_close_prices(
            coin_a, coin_b,
            interval=cfg.data["interval"],
            lookback_hours=cfg.data["lookback_hours"],
        )
    else:
        close_a, close_b = prices
    log_a = np.log(np.array(close_a))
    log_b = np.log(np.array(close_b))

    joint = bool(cfg.model.get("joint_ecm", True))
    eg_level = cfg.signal.get("stationarity_level", 0.10)
    min_p_kappa = float(cfg.signal.get("min_p_kappa", 0.80))
    kappa_floor = float(cfg.signal.get("kappa_floor", 0.002))

    if joint:
        ecm = fit_error_correction(
            log_a, log_b,
            draws=cfg.model.get("ecm_draws", cfg.model["coint_draws"]),
            tune=cfg.model.get("ecm_tune", cfg.model["coint_tune"]),
            chains=cfg.model["chains"],
            cores=cfg.model.get("cores", 1),
            progressbar=False,
            kappa_prior_pop=kappa_prior_pop,
            kappa_prior_pop_sd=kappa_prior_pop_sd,
            kappa_floor=kappa_floor,
        )
        # Optional Kalman on the terminal (α, β) for fast crypto pairs only.
        # Rebuild the mean residual path; keep posterior draws for simulation.
        class _CointView:
            pass
        coint = _CointView()
        coint.alpha_mean = ecm.alpha_mean
        coint.beta_mean = ecm.beta_mean
        coint.beta_sd = ecm.beta_sd
        coint.nu_mean = ecm.nu_mean
        coint.converged = ecm.converged
        coint.spread = ecm.spread
        nuts_spread = ecm.spread - ecm.spread.mean()
        if _should_use_kalman(pair, cfg):
            _apply_kalman(pair, coint, log_a, log_b, cfg)
            # Point estimates follow Kalman; predictive draws stay joint-ECM
            # (Kalman has no posterior over paths here).
        spread_centered = coint.spread - coint.spread.mean()
        current_spread = float(spread_centered[-1])
        spread_sd = float(max(spread_centered.std(), nuts_spread.std()))
        z = current_spread / (spread_sd + 1e-9)
        eg_t = ecm.eg_tstat if coint.spread is ecm.spread else engle_granger_tstat(spread_centered)
        eg_ok = eg_level is None or eg_t <= EG_CRITICAL[float(eg_level)]
        kappa_ok = ecm.p_kappa_gt >= min_p_kappa
        half_life_hours = ecm.half_life_bars * bar_hours
        kappa_s, sigma_s, nu_s = ecm.kappa_draws, ecm.sigma_draws, ecm.nu_draws
        # Entry level uncertainty from the joint posterior (β uncertainty).
        spread_for_sim = ecm.current_spread_draws
        # If Kalman moved the point estimate, shift draws so their mean matches.
        spread_for_sim = spread_for_sim - spread_for_sim.mean() + current_spread
        sigma_mean, nu_mean = ecm.sigma_mean, ecm.nu_mean
        kappa_mean = ecm.kappa_mean
        models_ok = ecm.converged
        innov_rho = ecm.innovation_rho
        p_kappa = ecm.p_kappa_gt
    else:
        coint = fit_cointegration(
            log_a, log_b,
            draws=cfg.model["coint_draws"],
            tune=cfg.model["coint_tune"],
            chains=cfg.model["chains"],
            cores=cfg.model.get("cores", 1),
            progressbar=False,
        )
        nuts_spread = coint.spread - coint.spread.mean()
        if _should_use_kalman(pair, cfg):
            _apply_kalman(pair, coint, log_a, log_b, cfg)
        spread_centered = coint.spread - coint.spread.mean()
        current_spread = float(spread_centered[-1])
        spread_sd = float(max(spread_centered.std(), nuts_spread.std()))
        z = current_spread / (spread_sd + 1e-9)
        eg_t = engle_granger_tstat(spread_centered)
        eg_ok = eg_level is None or eg_t <= EG_CRITICAL[float(eg_level)]
        kappa_ok = True
        ou = fit_ou(
            spread_centered,
            draws=cfg.model["ou_draws"],
            tune=cfg.model["ou_tune"],
            chains=cfg.model["chains"],
            cores=cfg.model.get("cores", 1),
            progressbar=False,
            kappa_prior_pop=kappa_prior_pop,
            kappa_prior_pop_sd=kappa_prior_pop_sd,
        )
        half_life_hours = ou.half_life_bars * bar_hours
        kappa_s = ou.idata.posterior["kappa"].stack(s=("chain", "draw")).values
        sigma_s = ou.idata.posterior["sigma"].stack(s=("chain", "draw")).values
        nu_s = ou.idata.posterior["nu"].stack(s=("chain", "draw")).values
        spread_for_sim = current_spread
        sigma_mean, nu_mean, kappa_mean = ou.sigma_mean, ou.nu_mean, ou.kappa_mean
        models_ok = coint.converged and ou.converged
        innov_rho = 0.0
        p_kappa = 1.0

    policy = exit_policy_from_cfg(
        cfg, half_life_bars=min(half_life_hours / bar_hours, 500.0),
        beta=coint.beta_mean, bar_hours=bar_hours,
    )
    stress_q = float((cfg.raw.get("leverage", {}) or {}).get("stress_quantile", 0.01))
    dist = simulate_trade_returns(
        current_spread=spread_for_sim,
        kappa_post=kappa_s, sigma_post=sigma_s, nu_post=nu_s,
        policy=policy, stress_quantile=stress_q,
        seed=int(cfg.model.get("seed", 0)),
    )
    p_revert = dist.p_win
    edge = p_revert - 0.5

    # Half-life filter: reject slow-reverting pairs (validated: HL>96h net-negative)
    # Per-pair overrides: HIP-3 pairs (semis, metals) revert slower than
    # crypto pairs. The 96h global filter was validated on crypto only.
    overrides = cfg.signal.get("half_life_overrides", {}) or {}
    hl_max = float(overrides.get(pair, cfg.signal.get("half_life_max_hours", 999999)))
    half_life_rejected = half_life_hours > hl_max

    kelly_mode = (cfg.raw.get("leverage", {}) or {}).get("mode") == "kelly"
    if kelly_mode:
        # Sized later by the scheduler from the trade distribution and book.
        notional = 0.0
    else:
        notional = position_size(
            p_revert=p_revert,
            sigma=sigma_mean,
            nu=nu_mean,
            base_notional=cfg.position["base_notional"],
        )
        if scaler is not None:
            notional = scaler.scale_notional(notional)
        # Hyperliquid rejects orders below $10.
        if 0 < notional < 10.0:
            notional = 0.0

    # Asymmetric z thresholds: tight side (z < 0) showed stronger edge
    # (walk-forward n=445: tight CI lower +0.045 vs wide -0.009).
    z_thresh_wide = float(cfg.signal.get("z_entry_min_wide", cfg.signal.get("z_entry_min", 1.0)))
    z_thresh_tight = float(cfg.signal.get("z_entry_min_tight", cfg.signal.get("z_entry_min", 1.0)))
    threshold = z_thresh_wide if z > 0 else z_thresh_tight
    min_ret = float(cfg.signal.get("min_expected_return", 0.0))

    max_vol = cfg.signal.get("max_btc_vol", 1.0)
    checks = [
        (models_ok, "model_not_converged"),
        (coint.beta_mean > 0, "beta_nonpositive"),
        (btc_vol is not None, "btc_vol_unknown"),
        (btc_vol is None or btc_vol <= max_vol, "btc_vol_high"),
        (eg_ok, "not_stationary"),
        (kappa_ok, "kappa_near_zero"),
        (not half_life_rejected, "half_life_too_long"),
        (abs(z) >= threshold, "z_too_small"),
        (dist.exp_return > min_ret, "expected_return_too_small"),
        (kelly_mode or edge >= cfg.signal["edge_min"], "edge_too_small"),
        (kelly_mode or notional > 0, "notional_zero"),
    ]
    failed = [reason for ok, reason in checks if not ok]

    if not failed:
        action = "OPEN"
        direction = ("SHORT_A_LONG_B" if current_spread > 0
                     else "LONG_A_SHORT_B")
    else:
        action = "SKIP"
        direction = "NONE"

    if joint:
        print(f"[ECM] {pair}: β={coint.beta_mean:.3f}±{coint.beta_sd:.3f} "
              f"κ={kappa_mean:.4f} P(κ>{kappa_floor})={p_kappa:.2f} "
              f"innov_ρ={innov_rho:+.3f} EG_t={eg_t:+.2f}")

    return Signal(
        pair=pair,
        coin_a=coin_a, coin_b=coin_b,
        beta=coint.beta_mean, beta_sd=coint.beta_sd,
        nu_coint=getattr(coint, "nu_mean", nu_mean),
        converged_coint=models_ok,
        alpha=coint.alpha_mean,
        mean_raw=float(coint.spread.mean()),
        sigma_raw=spread_sd,
        current_spread=current_spread,
        spread_sd=spread_sd,
        z=z,
        kappa=kappa_mean, half_life_hours=half_life_hours,
        sigma_ou=sigma_mean, nu_ou=nu_mean,
        converged_ou=models_ok,
        p_revert=p_revert, edge=edge, notional=notional,
        direction=direction, action=action,
        exp_return=dist.exp_return,
        stress_loss_frac=dist.stress_loss_frac,
        eg_tstat=eg_t,
        skip_reason=",".join(failed),
        trade_returns=dist.returns,
        spread_for_sim=np.asarray(spread_for_sim, dtype=float),
        kappa_draws=np.asarray(kappa_s, dtype=float),
        sigma_draws=np.asarray(sigma_s, dtype=float),
    )
