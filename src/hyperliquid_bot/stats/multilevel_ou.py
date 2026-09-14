"""
Multilevel Ornstein-Uhlenbeck model.

Given spreads from N pairs, fit a single model with:
    - A population mean-reversion speed kappa_pop
    - A population spread-sigma sigma_pop
    - Per-pair kappas drawn from a distribution around kappa_pop
    - Per-pair sigmas drawn from a distribution around sigma_pop

This is McElreath Ch 12 §12.1 (tadpole model) applied to stat-arb pairs.

The key benefit: pairs with limited data borrow strength from the population.
The key diagnostic: how much does each pair's kappa move toward the population?
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pymc as pm
import arviz as az


@dataclass
class MultilevelOUFit:
    kappa_pop_mean: float
    kappa_pop_sd: float
    kappa_pop_hdi_low: float
    kappa_pop_hdi_high: float
    kappa_pair_means: dict
    kappa_pair_sds: dict
    half_life_pair: dict
    sigma_pop_mean: float
    nu_mean: float
    idata: az.InferenceData
    converged: bool
    pairs: list


def _half_life(kappa_samples: np.ndarray) -> float:
    """Compute posterior mean half-life from kappa samples."""
    k_safe = np.clip(kappa_samples, 1e-4, 1 - 1e-4)
    hl = np.log(0.5) / np.log(1 - k_safe)
    return float(hl.mean())


def fit_multilevel_ou(
    spreads: dict[str, np.ndarray],
    draws: int = 1000,
    tune: int = 1000,
    chains: int = 4,
    seed: int = 0,
    progressbar: bool = True,
) -> MultilevelOUFit:
    pair_names = list(spreads.keys())
    n_pairs = len(pair_names)

    # Build ragged data
    y_lag_list, y_next_list, pair_idx_list = [], [], []
    for p_idx, name in enumerate(pair_names):
        s = np.asarray(spreads[name], dtype=float)
        s_centered = s - s.mean()                 # center each pair on zero
        y_lag_list.append(s_centered[:-1])
        y_next_list.append(s_centered[1:])
        pair_idx_list.append(np.full(len(s) - 1, p_idx, dtype=int))

    y_lag = np.concatenate(y_lag_list)
    y_next = np.concatenate(y_next_list)
    pair_idx = np.concatenate(pair_idx_list)

    with pm.Model() as model:
        # --- Population (hyperpriors) ---
        # logit(kappa_pop) around logit(0.05) ~ -2.94
        logit_kappa_pop = pm.Normal("logit_kappa_pop", mu=-2.94, sigma=1.0)
        kappa_pop = pm.Deterministic(
            "kappa_pop", pm.math.sigmoid(logit_kappa_pop)
        )
        sigma_kappa = pm.HalfNormal("sigma_kappa", sigma=1.0)

        # Population residual scale
        sigma_pop = pm.HalfCauchy("sigma_pop", beta=0.01)
        sigma_scale = pm.HalfNormal("sigma_scale", sigma=0.5)

        # --- Per-pair (non-centered parameterization, Ch 13 §13.3) ---
        kappa_z = pm.Normal("kappa_z", mu=0, sigma=1, shape=n_pairs)
        logit_kappa_pair = logit_kappa_pop + kappa_z * sigma_kappa
        kappa_pair = pm.Deterministic(
            "kappa_pair", pm.math.sigmoid(logit_kappa_pair)
        )

        sigma_z = pm.Normal("sigma_z", mu=0, sigma=1, shape=n_pairs)
        sigma_pair = pm.Deterministic(
            "sigma_pair", sigma_pop * pm.math.exp(sigma_z * sigma_scale)
        )

        # Fat-tailed residuals
        nu = pm.Exponential("nu", lam=1 / 10.0)

        # --- Likelihood ---
        mu = (1 - kappa_pair[pair_idx]) * y_lag
        pm.StudentT(
            "obs", nu=nu, mu=mu, sigma=sigma_pair[pair_idx], observed=y_next
        )

        idata = pm.sample(
            draws=draws, tune=tune, chains=chains, cores=1,
            target_accept=0.95, random_seed=seed, progressbar=progressbar,
        )

    # Convergence
    rhat = az.rhat(idata)
    max_rhat = float(max(
        rhat["logit_kappa_pop"].values,
        rhat["sigma_kappa"].values,
        rhat["sigma_pop"].values,
        float(rhat["kappa_z"].values.max()),
    ))
    converged = max_rhat < 1.05

    # Extract posteriors
    kappa_pop_samples = idata.posterior["kappa_pop"].stack(
        s=("chain", "draw")
    ).values
    kappa_pair_samples = idata.posterior["kappa_pair"].stack(
        s=("chain", "draw")
    ).values  # shape: (n_pairs, n_samples)

    hdi = az.hdi(idata, prob=0.89)["kappa_pop"].values

    kappa_pair_means, kappa_pair_sds, half_life_pair = {}, {}, {}
    for p_idx, name in enumerate(pair_names):
        k = kappa_pair_samples[p_idx, :]
        kappa_pair_means[name] = float(k.mean())
        kappa_pair_sds[name] = float(k.std())
        half_life_pair[name] = _half_life(k)

    return MultilevelOUFit(
        kappa_pop_mean=float(kappa_pop_samples.mean()),
        kappa_pop_sd=float(kappa_pop_samples.std()),
        kappa_pop_hdi_low=float(hdi[0]),
        kappa_pop_hdi_high=float(hdi[1]),
        kappa_pair_means=kappa_pair_means,
        kappa_pair_sds=kappa_pair_sds,
        half_life_pair=half_life_pair,
        sigma_pop_mean=float(idata.posterior["sigma_pop"].mean()),
        nu_mean=float(idata.posterior["nu"].mean()),
        idata=idata,
        converged=converged,
        pairs=pair_names,
    )