"""
Linear-drift beta cointegration model.
McElreath Ch 7 (interactions with time as continuous predictor).

Instead of a static beta, or a GP-modulated beta, model beta as:

    beta(t) = beta0 + beta1 * t

where t is normalized to [-1, +1] across the fit window. This lets
the hedge ratio drift linearly over time at a small fraction of the
cost of a full GP. Same likelihood, same priors, one extra parameter.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pymc as pm
import arviz as az


@dataclass
class DriftCointFit:
    alpha_mean: float
    beta0_mean: float
    beta1_mean: float
    beta_path_mean: float
    beta_path_end: float
    sigma_mean: float
    nu_mean: float
    spread: np.ndarray
    converged: bool
    max_rhat: float


def fit_cointegration_drift(
    log_a: np.ndarray,
    log_b: np.ndarray,
    draws: int = 1000,
    tune: int = 1000,
    chains: int = 2,
    cores: int = 2,
    seed: int = 0,
    progressbar: bool = False,
) -> DriftCointFit:
    n = len(log_a)
    assert n == len(log_b)
    assert n > 50, "need at least 50 obs"

    # Normalized time in [-1, +1]
    t = np.linspace(-1.0, 1.0, n)

    with pm.Model() as model:
        alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
        beta0 = pm.Normal("beta0", mu=1.0, sigma=1.0)       # intercept of beta
        beta1 = pm.Normal("beta1", mu=0.0, sigma=0.2)       # drift per unit time
        sigma = pm.HalfNormal("sigma", sigma=0.5)
        nu = pm.Exponential("nu", lam=1 / 10.0)

        beta_path = beta0 + beta1 * t
        mu = alpha + beta_path * log_b
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)

        idata = pm.sample(
            draws=draws, tune=tune, chains=chains, cores=cores,
            target_accept=0.95, random_seed=seed, progressbar=progressbar,
        )

    rhat = az.rhat(idata)
    max_rhat = max(
        float(rhat[v].values) for v in ["alpha", "beta0", "beta1", "sigma", "nu"]
    )
    converged = max_rhat < 1.05

    alpha_s = idata.posterior["alpha"].stack(s=("chain", "draw")).values
    beta0_s = idata.posterior["beta0"].stack(s=("chain", "draw")).values
    beta1_s = idata.posterior["beta1"].stack(s=("chain", "draw")).values
    sigma_s = idata.posterior["sigma"].stack(s=("chain", "draw")).values
    nu_s = idata.posterior["nu"].stack(s=("chain", "draw")).values

    alpha_m = float(alpha_s.mean())
    beta0_m = float(beta0_s.mean())
    beta1_m = float(beta1_s.mean())
    beta_path_m = beta0_m + beta1_m * t

    spread = log_a - alpha_m - beta_path_m * log_b
    spread = spread - spread.mean()

    return DriftCointFit(
        alpha_mean=alpha_m,
        beta0_mean=beta0_m,
        beta1_mean=beta1_m,
        beta_path_mean=float(beta_path_m.mean()),
        beta_path_end=float(beta_path_m[-1]),
        sigma_mean=float(sigma_s.mean()),
        nu_mean=float(nu_s.mean()),
        spread=spread,
        converged=converged,
        max_rhat=max_rhat,
    )


if __name__ == "__main__":
    import sys
    sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
    from src.hyperliquid_bot.data.ohlcv import aligned_close_prices

    ts, ca, cb = aligned_close_prices("ETH", "BTC", interval="1h", lookback_hours=24 * 30)
    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    print(f"Data: {len(log_a)} bars")

    import time
    t0 = time.time()
    fit = fit_cointegration_drift(log_a, log_b)
    elapsed = time.time() - t0

    print(f"Elapsed:      {elapsed:.1f}s")
    print(f"alpha:        {fit.alpha_mean:+.4f}")
    print(f"beta0:        {fit.beta0_mean:+.4f}")
    print(f"beta1:        {fit.beta1_mean:+.4f}  (drift per unit time)")
    print(f"beta_path_end {fit.beta_path_end:+.4f}  (beta at end of window)")
    print(f"nu:           {fit.nu_mean:.2f}")
    print(f"max R-hat:    {fit.max_rhat:.3f}")
    print(f"converged:    {fit.converged}")
