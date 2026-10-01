"""Generate a trade signal for one pair."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from ..data.ohlcv import aligned_close_prices
from ..data.ohlcv import fetch_candles
from ..stats.cointegration import fit_cointegration
from ..stats.mean_reversion import (
    fit_ou,
    simulate_reversion_probability,
    position_size,
)

def _ensemble_z(log_a, log_b, lookbacks_hours):
    """Fit cointegration at multiple lookbacks, average z-scores equally.
    Equal weights (not AIC) because AIC is not comparable across sample sizes.
    Returns (z_ensemble, alpha_avg, beta_avg, spread_series) or None.
    """
    from ..stats.cointegration import fit_cointegration
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
    p_revert: float
    edge: float
    notional: float
    direction: str               # "LONG_A_SHORT_B" / "SHORT_A_LONG_B"
    action: str                  # "OPEN" / "SKIP"

def generate_signal(
    coin_a: str,
    coin_b: str,
    cfg,
    kappa_prior_pop: Optional[float] = None,
    kappa_prior_pop_sd: Optional[float] = None,
    scaler=None,
    btc_vol: Optional[float] = None,
) -> Signal:
    """Fetch data, fit coint + OU, produce a Signal."""
    pair = f"{coin_a}/{coin_b}"


    ts, close_a, close_b = aligned_close_prices(
        coin_a, coin_b,
        interval=cfg.data["interval"],
        lookback_hours=cfg.data["lookback_hours"],
    )
    log_a = np.log(np.array(close_a))
    log_b = np.log(np.array(close_b))

    coint = fit_cointegration(
        log_a, log_b,
        draws=cfg.model["coint_draws"],
        tune=cfg.model["coint_tune"],
        chains=cfg.model["chains"],
        cores=cfg.model.get("cores", 1),
        progressbar=False,
    )

    # Centered spread
    spread_centered = coint.spread - coint.spread.mean()
    current_spread = float(spread_centered[-1])
    spread_sd = float(spread_centered.std())
    z = current_spread / (spread_sd + 1e-9)

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

    kappa_s = ou.idata.posterior["kappa"].stack(s=("chain", "draw")).values
    sigma_s = ou.idata.posterior["sigma"].stack(s=("chain", "draw")).values
    nu_s = ou.idata.posterior["nu"].stack(s=("chain", "draw")).values

    # Trade horizon = 2x half-life
    horizon = max(6, int(ou.half_life_bars * cfg.position["exit_horizon_mult"]))

    p_revert = simulate_reversion_probability(
        current_spread=current_spread,
        kappa_post=kappa_s,
        sigma_post=sigma_s,
        nu_post=nu_s,
        horizon=horizon,
        target_frac=cfg.position["target_frac"],
        paths_per_sample=10,
    )

    # Half-life filter: reject slow-reverting pairs (validated: HL>96h net-negative)
    hl_max = float(cfg.signal.get("half_life_max_hours", 999999))
    if ou.half_life_bars > hl_max:
        return None

    edge = p_revert - 0.5

    notional = position_size(
        p_revert=p_revert,
        sigma=ou.sigma_mean,
        nu=ou.nu_mean,
        base_notional=cfg.position["base_notional"],
    )


    # Apply compounding scale
    if scaler is not None:
        notional = scaler.scale_notional(notional)

    # Minimum order floor: Hyperliquid rejects orders below $10.
    # If the edge-weighted size is below that, skip rather than submit a
    # rejected order.
    MIN_ORDER_USD = 10.0
    if 0 < notional < MIN_ORDER_USD:
        notional = 0.0

    # Entry decision
    enough_z = abs(z) >= cfg.signal["z_entry_min"]
    enough_edge = edge >= cfg.signal["edge_min"]
    models_ok = coint.converged and ou.converged

    # Regime filter: skip entries during high-vol regimes
    regime_ok = True
    if btc_vol is not None:
        max_vol = cfg.signal.get("max_btc_vol", 1.0)
        if btc_vol > max_vol:
            regime_ok = False


    if enough_z and enough_edge and models_ok and regime_ok and notional > 0:
        action = "OPEN"
        direction = ("SHORT_A_LONG_B" if current_spread > 0
                     else "LONG_A_SHORT_B")
    else:
        action = "SKIP"
        direction = "NONE"

    return Signal(
        pair=pair,
        coin_a=coin_a, coin_b=coin_b,
        beta=coint.beta_mean, beta_sd=coint.beta_sd,
        nu_coint=coint.nu_mean, converged_coint=coint.converged,
        alpha=coint.alpha_mean,
        mean_raw=float(coint.spread.mean()),
        sigma_raw=spread_sd,
        current_spread=current_spread,
        spread_sd=spread_sd,
        z=z,
        kappa=ou.kappa_mean, half_life_hours=ou.half_life_bars,
        sigma_ou=ou.sigma_mean, nu_ou=ou.nu_mean,
        converged_ou=ou.converged,
        p_revert=p_revert, edge=edge, notional=notional,
        direction=direction, action=action,
    )
