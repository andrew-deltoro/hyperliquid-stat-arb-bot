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
    deviance: float              # -2 * lppd (in-sample; no WAIC penalty)
    converged: bool
    beta_sd_iid: float = 0.0     # posterior sd under the i.i.d. likelihood
    residual_rho: float = 0.0    # lag-1 autocorrelation of the residuals


def residual_autocorr(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float) - np.mean(x)
    denom = float((x * x).sum())
    if denom <= 0 or len(x) < 3:
        return 0.0
    return float((x[1:] * x[:-1]).sum() / denom)


def autocorr_inflation(rho: float) -> float:
    """sd multiplier sqrt(n / n_eff) for AR(1) residuals with coefficient rho."""
    rho = float(np.clip(rho, 0.0, 0.999))
    return float(np.sqrt((1 + rho) / (1 - rho)))


# MacKinnon (2010) critical values for the Engle-Granger test, 2 variables,
# constant, no trend.
EG_CRITICAL = {0.01: -3.90, 0.05: -3.34, 0.10: -3.04}


def engle_granger_tstat(spread: np.ndarray, lags: int = 1) -> float:
    """
    ADF t-statistic on cointegration residuals:
        dS_t = a + b S_{t-1} + sum_j c_j dS_{t-j} + e_t
    More negative is stronger evidence of stationarity; compare with
    EG_CRITICAL (standard DF tables are too lenient for estimated residuals).
    """
    s = np.asarray(spread, dtype=float)
    ds = np.diff(s)
    y = ds[lags:]
    cols = [np.ones_like(y), s[lags:-1]]
    for j in range(1, lags + 1):
        cols.append(ds[lags - j:-j])
    X = np.column_stack(cols)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    dof = max(1, len(y) - X.shape[1])
    s2 = float(resid @ resid) / dof
    cov = s2 * np.linalg.inv(X.T @ X)
    return float(coef[1] / np.sqrt(cov[1, 1]))


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

    # Center log_b to decorrelate alpha and beta. This drastically speeds up NUTS
    # on cointegration models. beta stays the same; alpha shifts by beta * mean(log_b).
    log_b_mean = float(np.mean(log_b))
    log_b_c = log_b - log_b_mean

    with pm.Model() as model:
        alpha_c = pm.Normal("alpha", mu=0.0, sigma=5.0)   # intercept at centered scale
        beta = pm.Normal("beta", mu=1.0, sigma=1.0)
        sigma = pm.HalfNormal("sigma", sigma=0.5)
        nu = pm.Exponential("nu", lam=1 / 10.0)

        mu = alpha_c + beta * log_b_c
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)

        idata = pm.sample(
            draws=draws, tune=tune, chains=chains, cores=cores,
            target_accept=0.95, random_seed=seed, progressbar=progressbar,
        )
        pm.compute_log_likelihood(idata, model=model)
        log_lik = idata.log_likelihood["obs"].values       # (chains, draws, obs)
        mean_ll_per_obs = log_lik.mean(axis=(0, 1))         # per-observation mean log-lik
        lppd = float(mean_ll_per_obs.sum())                 # log pointwise predictive density
        deviance = -2 * lppd                                # smaller is better; not penalized

    rhat = az.rhat(idata)
    max_rhat = float(max(
        rhat["alpha"].values, rhat["beta"].values,
        rhat["sigma"].values, rhat["nu"].values,
    ))
    converged = max_rhat < 1.05

    alpha_c_s = idata.posterior["alpha"].stack(s=("chain", "draw")).values
    beta_s_raw = idata.posterior["beta"].stack(s=("chain", "draw")).values
    # Recover alpha on original (uncentered) scale: alpha = alpha_c - beta * mean(log_b)
    alpha_s = alpha_c_s - beta_s_raw * log_b_mean
    beta_s = beta_s_raw
    sigma_s = idata.posterior["sigma"].stack(s=("chain", "draw")).values
    nu_s = idata.posterior["nu"].stack(s=("chain", "draw")).values

    spread_samples = log_a[None, :] - (alpha_s[:, None] + beta_s[:, None] * log_b[None, :])
    spread_mean = spread_samples.mean(axis=0)

    # The likelihood treats residuals as i.i.d., but they are strongly
    # autocorrelated, so the posterior sd of beta is too small by roughly
    # sqrt(n / n_eff). Report the corrected value as beta_sd.
    rho = residual_autocorr(spread_mean)
    beta_sd_iid = float(beta_s.std())

    return CointegrationFit(
        alpha_mean=float(alpha_s.mean()),
        alpha_sd=float(alpha_s.std()),
        beta_mean=float(beta_s.mean()),
        beta_sd=beta_sd_iid * autocorr_inflation(rho),
        sigma_mean=float(sigma_s.mean()),
        sigma_sd=float(sigma_s.std()),
        nu_mean=float(nu_s.mean()),
        spread=spread_mean,
        spread_samples=spread_samples,
        idata=idata,
        deviance=deviance,
        converged=converged,
        beta_sd_iid=beta_sd_iid,
        residual_rho=rho,
    )


def fit_null_model(
    log_a: np.ndarray,
    draws: int = 1000, tune: int = 1000, chains: int = 4, cores: int = 1,
    seed: int = 0, progressbar: bool = True,
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
