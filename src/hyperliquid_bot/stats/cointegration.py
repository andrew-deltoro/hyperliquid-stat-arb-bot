"""Bayesian cointegration model — McElreath Ch 4, 5, 6, 11."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pymc as pm
import arviz as az


@dataclass
class CointegrationFit:
    alpha_mean: float
    alpha_sd: float
    beta_mean: float
    beta_sd: float
    sigma_mean: float
    sigma_sd: float
    nu_mean: float
    spread: np.ndarray
    spread_samples: np.ndarray
    idata: az.InferenceData
    waic: float
    converged: bool


def fit_cointegration(
    log_a: np.ndarray,
    log_b: np.ndarray,
    draws: int = 1000,
    tune: int = 1000,
    chains: int = 4,
    cores: int = 1,
    seed: int = 0,
    progressbar: bool = True,
    )-> CointegrationFit:
    assert len(log_a) == len(log_b)
    assert len(log_a) > 20, "Need at least ~20 bars"

    with pm.Model() as model:
        alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
        beta = pm.Normal("beta", mu=1.0, sigma=1.0)
        sigma = pm.HalfNormal("sigma", sigma=0.5)
        nu = pm.Exponential("nu", lam=1 / 10.0)

        mu = alpha + beta * log_b
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)

        idata = pm.sample(
            draws=draws, tune=tune, chains=chains, cores=cores,
            target_accept=0.95, random_seed=seed, progressbar=progressbar,
        )
        pm.compute_log_likelihood(idata, model=model)
        log_lik = idata.log_likelihood["obs"].values       # (chains, draws, obs)
        mean_ll_per_obs = log_lik.mean(axis=(0, 1))         # per-observation mean log-lik
        lppd = float(mean_ll_per_obs.sum())                 # log pointwise predictive density
        waic_val = -2 * lppd                                # deviance scale, smaller is better

    rhat = az.rhat(idata)
    max_rhat = float(max(
        rhat["alpha"].values, rhat["beta"].values,
        rhat["sigma"].values, rhat["nu"].values,
    ))
    converged = max_rhat < 1.05

    alpha_s = idata.posterior["alpha"].stack(s=("chain", "draw")).values
    beta_s = idata.posterior["beta"].stack(s=("chain", "draw")).values
    sigma_s = idata.posterior["sigma"].stack(s=("chain", "draw")).values
    nu_s = idata.posterior["nu"].stack(s=("chain", "draw")).values

    spread_samples = log_a[None, :] - (alpha_s[:, None] + beta_s[:, None] * log_b[None, :])
    spread_mean = spread_samples.mean(axis=0)

    return CointegrationFit(
        alpha_mean=float(alpha_s.mean()),
        alpha_sd=float(alpha_s.std()),
        beta_mean=float(beta_s.mean()),
        beta_sd=float(beta_s.std()),
        sigma_mean=float(sigma_s.mean()),
        sigma_sd=float(sigma_s.std()),
        nu_mean=float(nu_s.mean()),
        spread=spread_mean,
        spread_samples=spread_samples,
        idata=idata,
        waic=waic_val,
        converged=converged,
    )


def fit_null_model(
    log_a: np.ndarray,
    draws: int = 1000, tune: int = 1000, chains: int = 4, seed: int = 0,
    progressbar: bool = True,
) -> float:
    with pm.Model() as model:
        alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
        sigma = pm.HalfNormal("sigma", sigma=0.5)
        nu = pm.Exponential("nu", lam=1 / 10.0)
        pm.StudentT("obs", nu=nu, mu=alpha, sigma=sigma, observed=log_a)

        idata = pm.sample(
            draws=draws, tune=tune, chains=chains, cores=cores,
            target_accept=0.95, random_seed=seed, progressbar=progressbar,
        )
        pm.compute_log_likelihood(idata, model=model)
        log_lik = idata.log_likelihood["obs"].values
        mean_ll_per_obs = log_lik.mean(axis=(0, 1))
        lppd = float(mean_ll_per_obs.sum())
        return -2 * lppd
