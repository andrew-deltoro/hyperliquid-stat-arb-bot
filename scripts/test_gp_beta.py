"""
Test GP-based varying beta vs constant beta on ETH/BTC.

Fits both models on 30 days of hourly data. Compares:
  - Fit time
  - Beta trajectory (constant vs GP)
  - WAIC (if computable)

Run standalone. Doesn't touch the live bot.
"""
import sys
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pymc as pm
import arviz as az

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices


def fit_constant_beta(log_a, log_b):
    """Baseline: single constant beta."""
    t0 = time.time()
    with pm.Model() as model:
        alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
        beta = pm.Normal("beta", mu=1.0, sigma=1.0)
        sigma = pm.HalfCauchy("sigma", beta=1.0)
        nu = pm.Exponential("nu", lam=1/10.0)
        mu = alpha + beta * log_b
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)
        idata = pm.sample(
            draws=500, tune=500, chains=2, cores=1,
            progressbar=False, random_seed=0,
        )
    elapsed = time.time() - t0
    beta_mean = float(idata.posterior["beta"].mean())
    return {"beta_mean": beta_mean, "elapsed": elapsed, "idata": idata}


def fit_gp_beta(log_a, log_b):
    """Varying beta via Gaussian Process over time."""
    T = len(log_a)
    t = np.arange(T, dtype=float) / T  # normalized time

    t0 = time.time()
    with pm.Model() as model:
        # GP prior for beta(t)
        # Lengthscale controls how smooth beta is; longer = smoother
        ls = pm.Gamma("ls", alpha=2.0, beta=4.0)
        eta = pm.HalfCauchy("eta", beta=1.0)

        cov = eta**2 * pm.gp.cov.ExpQuad(1, ls=ls)
        gp = pm.gp.Latent(cov_func=cov)
        beta_t = gp.prior("beta_t", X=t.reshape(-1, 1))

        alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
        sigma = pm.HalfCauchy("sigma", beta=1.0)
        nu = pm.Exponential("nu", lam=1/10.0)

        mu = alpha + beta_t * log_b
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)

        idata = pm.sample(
            draws=500, tune=500, chains=2, cores=1,
            progressbar=False, random_seed=0,
            target_accept=0.95,
        )
    elapsed = time.time() - t0
    beta_t_samples = idata.posterior["beta_t"].values  # (chains, draws, T)
    beta_t_mean = beta_t_samples.reshape(-1, T).mean(axis=0)
    return {
        "beta_t": beta_t_mean,
        "beta_first": float(beta_t_mean[0]),
        "beta_last": float(beta_t_mean[-1]),
        "beta_drift": float(beta_t_mean[-1] - beta_t_mean[0]),
        "elapsed": elapsed,
        "idata": idata,
    }


def main():
    print("=" * 70)
    print("GP BETA vs CONSTANT BETA — ETH/BTC")
    print("=" * 70)
    print()

    print("Fetching ETH/BTC data...")
    ts, ca, cb = aligned_close_prices("ETH", "BTC", interval="1h", lookback_hours=720)
    print(f"Got {len(ts)} bars\n")

    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))

    print("--- Fitting CONSTANT beta (baseline) ---")
    r_const = fit_constant_beta(log_a, log_b)
    print(f"  beta:    {r_const['beta_mean']:.4f}")
    print(f"  elapsed: {r_const['elapsed']:.1f}s\n")

    print("--- Fitting GP beta (varying) ---")
    r_gp = fit_gp_beta(log_a, log_b)
    print(f"  beta[0]:   {r_gp['beta_first']:.4f}")
    print(f"  beta[-1]:  {r_gp['beta_last']:.4f}")
    print(f"  drift:     {r_gp['beta_drift']:+.4f}")
    print(f"  elapsed:   {r_gp['elapsed']:.1f}s\n")

    # Comparison
    print("=== Comparison ===")
    print(f"  Constant beta: {r_const['beta_mean']:.4f}")
    print(f"  GP beta start: {r_gp['beta_first']:.4f}")
    print(f"  GP beta end:   {r_gp['beta_last']:.4f}")
    print()
    if abs(r_gp['beta_drift']) > 0.05:
        print(f"  ⚠️  Significant drift: {r_gp['beta_drift']:+.4f}")
        print(f"  → Constant beta model is likely mis-specified")
    else:
        print(f"  ✅ Beta is stable — constant beta may be fine")
    print()


if __name__ == "__main__":
    main()
