"""
Ornstein-Uhlenbeck (OU) mean-reversion model.

Given a spread series S_t, we model:
    S_{t+1} = (1 - kappa) * S_t + eps_t
    eps_t ~ StudentT(nu, 0, sigma)

where kappa ∈ [0, 1] is the mean-reversion speed (kappa=0 => random walk;
kappa=1 => instant snap-back). We fit this on top of the cointegration model
to compute the posterior probability of reversion within a horizon.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pymc as pm
import arviz as az


@dataclass
class OUFit:
    kappa_mean: float
    kappa_sd: float
    sigma_mean: float
    sigma_sd: float
    nu_mean: float
    half_life_bars: float
    idata: az.InferenceData
    converged: bool


def fit_ou(
    spread: np.ndarray,
    draws: int = 1000,
    tune: int = 1000,
    chains: int = 4,
    seed: int = 0,
    progressbar: bool = True,
    kappa_prior_pop: float | None = None,
    kappa_prior_pop_sd: float | None = None,
) -> OUFit:
    """
    Fit OU model on the spread series.
    The spread should already be mean-centered (subtract its own mean
    before passing in, OR the model handles it via a grand mean).
    """
    y = np.asarray(spread, dtype=float)
    assert len(y) > 50, "Need at least ~50 bars"

    # Center the spread so kappa pulls it toward zero
    y_centered = y - y.mean()

    y_lag = y_centered[:-1]
    y_next = y_centered[1:]

    with pm.Model() as model:
        if kappa_prior_pop is not None and kappa_prior_pop_sd is not None:
            # Population-informed prior (from multilevel fit).
            # We use logit(kappa) ~ Normal(logit_mean, logit_sd).
            # Delta method converts population sd to logit scale.
            kp = float(np.clip(kappa_prior_pop, 1e-4, 1 - 1e-4))
            logit_mean = float(np.log(kp / (1 - kp)))
            logit_sd = float(
                kappa_prior_pop_sd / (kp * (1 - kp)) + 1e-3
            )
            logit_kappa = pm.Normal(
                "logit_kappa", mu=logit_mean, sigma=logit_sd,
            )
            kappa = pm.Deterministic(
                "kappa", pm.math.sigmoid(logit_kappa),
            )
        else:
            # Fallback: uninformative Beta(2, 2)
            kappa = pm.Beta("kappa", alpha=2.0, beta=2.0)

        # Residual noise after mean reversion step
        sigma = pm.HalfCauchy("sigma", beta=1.0)

        # Fat tails again (Ch 11)
        nu = pm.Exponential("nu", lam=1 / 10.0)

        mu = (1 - kappa) * y_lag
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=y_next)

        idata = pm.sample(
            draws=draws, tune=tune, chains=chains, cores=1,
            target_accept=0.95, random_seed=seed, progressbar=progressbar,
        )

    rhat = az.rhat(idata)
    max_rhat = float(max(
        rhat["kappa"].values, rhat["sigma"].values, rhat["nu"].values,
    ))
    converged = max_rhat < 1.05

    kappa_s = idata.posterior["kappa"].stack(s=("chain", "draw")).values
    sigma_s = idata.posterior["sigma"].stack(s=("chain", "draw")).values
    nu_s = idata.posterior["nu"].stack(s=("chain", "draw")).values

    kappa_mean = float(kappa_s.mean())
    # Half-life in bars: solve (1 - kappa)^h = 0.5 => h = log(0.5) / log(1 - kappa)
    if kappa_mean > 1e-6 and kappa_mean < 1:
        half_life = float(np.log(0.5) / np.log(1 - kappa_mean))
    else:
        half_life = float("inf")

    return OUFit(
        kappa_mean=kappa_mean,
        kappa_sd=float(kappa_s.std()),
        sigma_mean=float(sigma_s.mean()),
        sigma_sd=float(sigma_s.std()),
        nu_mean=float(nu_s.mean()),
        half_life_bars=half_life,
        idata=idata,
        converged=converged,
    )


def simulate_reversion_probability(
    current_spread: float,
    kappa_post: np.ndarray,
    sigma_post: np.ndarray,
    nu_post: np.ndarray,
    horizon: int = 24,
    target_frac: float = 0.5,
    paths_per_sample: int = 5,
) -> float:
    """
    Posterior predictive probability that the spread reverts to within
    `target_frac` of its current deviation within `horizon` bars.

    For each posterior sample, simulate `paths_per_sample` synthetic paths.
    Return the fraction that ever hit the target.
    """
    S = len(kappa_post)
    n_total = S * paths_per_sample

    # Tile posterior samples
    k = np.tile(kappa_post, paths_per_sample)
    sig = np.tile(sigma_post, paths_per_sample)
    nu_p = np.tile(nu_post, paths_per_sample)

    # Target: |spread| < target_frac * |current_spread|
    target_abs = target_frac * abs(current_spread)

    S_t = np.full(n_total, current_spread, dtype=float)
    hit = np.zeros(n_total, dtype=bool)

    for _ in range(horizon):
        # Student-t innovations
        eps = np.random.standard_t(nu_p) * sig
        S_t = (1 - k) * S_t + eps
        hit = hit | (np.abs(S_t) < target_abs)

    return float(hit.mean())


def position_size(
    p_revert: float,
    sigma: float,
    nu: float,
    base_notional: float = 1000.0,
) -> float:
    """
    Position sizing heuristic combining:
      - edge = p_revert - 0.5   (above baseline)
      - tail_discount = f(nu)    (fat tails => smaller)
      - vol_discount = 1/sigma   (higher spread noise => smaller)
    Returns notional in USD (0 if no edge).
    """
    edge = max(0.0, p_revert - 0.5)
    if edge < 0.05:
        return 0.0

    # Tail discount: 0.2 at nu=2, 0.5 at nu=10, 0.9 at nu=30, ~1 at nu=100
    tail_discount = float(np.tanh(nu / 20.0))
    tail_discount = max(0.1, min(1.0, tail_discount))

    # Volatility discount: normalized to a "good" sigma of 0.02
    vol_discount = min(1.0, 0.02 / (sigma + 1e-6))

    size = base_notional * edge * tail_discount * vol_discount
    return float(size)