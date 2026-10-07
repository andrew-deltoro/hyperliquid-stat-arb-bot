"""
Joint error-correction / OU-on-spread model (audit H6).

Two-step Engle–Granger fits an i.i.d. cointegrating regression, then an OU on
the posterior-mean residual. That understates beta uncertainty (residuals are
AR(1)) and never feeds beta draws into the OU (Statistical Rethinking §15.1,
§16.4).

Here the cointegrating residual *is* the OU state in one likelihood:

    r_t = log A_t − α − β log B_t
    r_{t+1} = (1 − κ) r_t + ε_t,   ε_t ~ StudentT(ν, 0, σ)

Equivalently, for t = 0 … n−2:

    E[log A_{t+1} | ·] = α + β log B_{t+1}
                         + (1 − κ) (log A_t − α − β log B_t)

β and κ share one posterior, so OU parameters and trade simulations inherit
β uncertainty. Innovation lag-1 autocorrelation near zero is a PPC that the
serial dependence was absorbed into κ.
"""
from __future__ import annotations

from dataclasses import dataclass

import arviz as az
import numpy as np
import pymc as pm

from .cointegration import (
    EG_CRITICAL,
    engle_granger_tstat,
    residual_autocorr,
)


@dataclass
class ErrorCorrectionFit:
    alpha_mean: float
    alpha_sd: float
    beta_mean: float
    beta_sd: float
    kappa_mean: float
    kappa_sd: float
    sigma_mean: float
    sigma_sd: float
    nu_mean: float
    half_life_bars: float
    spread: np.ndarray                 # posterior-mean residual, uncentered
    spread_samples: np.ndarray         # (draws, T)
    current_spread_draws: np.ndarray   # centered terminal residual per draw
    kappa_draws: np.ndarray
    sigma_draws: np.ndarray
    nu_draws: np.ndarray
    beta_draws: np.ndarray
    alpha_draws: np.ndarray
    idata: az.InferenceData
    converged: bool
    eg_tstat: float
    innovation_rho: float              # lag-1 autocorr of OU innovations (PPC)
    p_kappa_gt: float                  # P(κ > kappa_floor); low ⇒ random walk


def fit_error_correction(
    log_a: np.ndarray,
    log_b: np.ndarray,
    draws: int = 1000,
    tune: int = 1000,
    chains: int = 4,
    cores: int = 1,
    seed: int = 0,
    progressbar: bool = True,
    kappa_prior_pop: float | None = None,
    kappa_prior_pop_sd: float | None = None,
    kappa_floor: float = 0.002,
) -> ErrorCorrectionFit:
    log_a = np.asarray(log_a, dtype=float)
    log_b = np.asarray(log_b, dtype=float)
    assert len(log_a) == len(log_b)
    assert len(log_a) > 50, "Need at least ~50 bars"

    log_b_mean = float(np.mean(log_b))
    log_b_c = log_b - log_b_mean
    y0, y1 = log_a[:-1], log_a[1:]
    x0, x1 = log_b_c[:-1], log_b_c[1:]

    with pm.Model():
        alpha_c = pm.Normal("alpha", mu=0.0, sigma=5.0)
        beta = pm.Normal("beta", mu=1.0, sigma=1.0)

        if kappa_prior_pop is not None and kappa_prior_pop_sd is not None:
            kp = float(np.clip(kappa_prior_pop, 1e-4, 1 - 1e-4))
            logit_mean = float(np.log(kp / (1 - kp)))
            logit_sd = float(kappa_prior_pop_sd / (kp * (1 - kp)) + 1e-3)
            logit_kappa = pm.Normal("logit_kappa", mu=logit_mean, sigma=logit_sd)
            kappa = pm.Deterministic("kappa", pm.math.sigmoid(logit_kappa))
        else:
            kappa = pm.Beta("kappa", alpha=2.0, beta=2.0)

        sigma = pm.HalfNormal("sigma", sigma=0.5)
        nu = pm.Exponential("nu", lam=1 / 10.0)

        resid0 = y0 - alpha_c - beta * x0
        mu = alpha_c + beta * x1 + (1.0 - kappa) * resid0
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=y1)

        idata = pm.sample(
            draws=draws, tune=tune, chains=chains, cores=cores,
            target_accept=0.95, random_seed=seed, progressbar=progressbar,
        )

    rhat = az.rhat(idata)
    names = ["alpha", "beta", "kappa", "sigma", "nu"]
    max_rhat = float(max(float(rhat[n].values) for n in names))
    converged = max_rhat < 1.05

    alpha_c_s = idata.posterior["alpha"].stack(s=("chain", "draw")).values
    beta_s = idata.posterior["beta"].stack(s=("chain", "draw")).values
    kappa_s = idata.posterior["kappa"].stack(s=("chain", "draw")).values
    sigma_s = idata.posterior["sigma"].stack(s=("chain", "draw")).values
    nu_s = idata.posterior["nu"].stack(s=("chain", "draw")).values
    alpha_s = alpha_c_s - beta_s * log_b_mean

    spread_samples = log_a[None, :] - (alpha_s[:, None] + beta_s[:, None] * log_b[None, :])
    spread_mean = spread_samples.mean(axis=0)

    # Center each draw's path so the OU mean is zero (same as fit_ou).
    centered = spread_samples - spread_samples.mean(axis=1, keepdims=True)
    current_spread_draws = centered[:, -1]

    # PPC: innovations ε_t = r_{t+1} − (1−κ) r_t should be nearly white.
    r_path = spread_mean - spread_mean.mean()
    k_m = float(kappa_s.mean())
    innov = r_path[1:] - (1.0 - k_m) * r_path[:-1]
    innov_rho = residual_autocorr(innov)

    kappa_mean = float(kappa_s.mean())
    if 1e-6 < kappa_mean < 1:
        half_life = float(np.log(0.5) / np.log(1 - kappa_mean))
    else:
        half_life = float("inf")

    return ErrorCorrectionFit(
        alpha_mean=float(alpha_s.mean()),
        alpha_sd=float(alpha_s.std()),
        beta_mean=float(beta_s.mean()),
        beta_sd=float(beta_s.std()),
        kappa_mean=kappa_mean,
        kappa_sd=float(kappa_s.std()),
        sigma_mean=float(sigma_s.mean()),
        sigma_sd=float(sigma_s.std()),
        nu_mean=float(nu_s.mean()),
        half_life_bars=half_life,
        spread=spread_mean,
        spread_samples=spread_samples,
        current_spread_draws=current_spread_draws,
        kappa_draws=kappa_s,
        sigma_draws=sigma_s,
        nu_draws=nu_s,
        beta_draws=beta_s,
        alpha_draws=alpha_s,
        idata=idata,
        converged=converged,
        eg_tstat=engle_granger_tstat(spread_mean - spread_mean.mean()),
        innovation_rho=innov_rho,
        p_kappa_gt=float((kappa_s > kappa_floor).mean()),
    )


def rejects_random_walk(fit: ErrorCorrectionFit, eg_level: float | None = 0.10,
                        min_p_kappa: float = 0.80) -> tuple[bool, str]:
    """
    True if the pair should be skipped as non-cointegrated / non-mean-reverting.
    Combines Engle–Granger on the posterior-mean residual with P(κ > floor).
    """
    reasons = []
    if eg_level is not None and fit.eg_tstat > EG_CRITICAL[float(eg_level)]:
        reasons.append("not_stationary")
    if fit.p_kappa_gt < min_p_kappa:
        reasons.append("kappa_near_zero")
    return (bool(reasons), ",".join(reasons))
