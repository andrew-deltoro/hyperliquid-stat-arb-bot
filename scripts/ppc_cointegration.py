"""
Posterior Predictive Check for the cointegration model.
McElreath Ch 3 - Sampling the Imaginary.

For each pair:
  1. Fetch recent hourly data
  2. Fit cointegration with full MCMC (Student-t likelihood, Ch 11)
  3. Generate posterior predictive samples of log_a
  4. Check what fraction of observed log_a falls within the 89% PPC interval

If coverage is far from 89%, the model is misspecified.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")

import numpy as np
import pandas as pd
import pymc as pm
import arviz as az

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices

PAIRS = [
    ("ETH", "BTC"),
    ("SOL", "AVAX"),
    ("AAVE", "COMP"),
    ("LINK", "PYTH"),
]

INTERVAL = "1h"
LOOKBACK_HOURS = 24 * 60
COVERAGE = 0.89


def ppc_for_pair(coin_a, coin_b):
    pair = f"{coin_a}/{coin_b}"
    print(f"--- {pair} ---")

    ts, ca, cb = aligned_close_prices(
        coin_a, coin_b, interval=INTERVAL, lookback_hours=LOOKBACK_HOURS,
    )
    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    print(f"  Obs:        {len(log_a)}")

    with pm.Model() as model:
        alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
        beta = pm.Normal("beta", mu=1.0, sigma=1.0)
        sigma = pm.HalfCauchy("sigma", beta=1.0)
        nu = pm.Exponential("nu", lam=1 / 10.0)
        mu = alpha + beta * log_b
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)

        idata = pm.sample(
            draws=1000, tune=1000, chains=2, cores=2,
            target_accept=0.95, random_seed=0, progressbar=False,
        )
        pm.sample_posterior_predictive(
            idata, extend_inferencedata=True, random_seed=0,
        )

    ppc = idata.posterior_predictive["obs"].stack(s=("chain", "draw")).values
    lower = np.percentile(ppc, 100 * (1 - COVERAGE) / 2, axis=1)
    upper = np.percentile(ppc, 100 * (1 + COVERAGE) / 2, axis=1)

    inside = (log_a >= lower) & (log_a <= upper)
    coverage = float(inside.mean())

    rhat = az.rhat(idata)
    max_rhat = max(
        float(rhat[v].values) for v in ["alpha", "beta", "sigma", "nu"]
    )

    print(f"  Coverage:   {coverage:.3f}  (target {COVERAGE})")
    print(f"  Max R-hat:  {max_rhat:.3f}")

    if max_rhat > 1.05:
        print(f"  ⚠️  R-hat above 1.05, chain did not converge")
    if coverage < 0.80:
        print(f"  ⚠️  UNDERCOVERED - model misspecified (residuals too small)")
    elif coverage > 0.96:
        print(f"  ⚠️  OVERCOVERED - model too uncertain (residuals too large)")
    else:
        print(f"  ✅ Coverage OK")

    return {
        "pair": pair,
        "n_obs": len(log_a),
        "coverage": coverage,
        "max_rhat": max_rhat,
        "nu_mean": float(idata.posterior["nu"].mean().values),
        "sigma_mean": float(idata.posterior["sigma"].mean().values),
    }


def main():
    print("=" * 60)
    print("POSTERIOR PREDICTIVE CHECK - Cointegration (Ch 3)")
    print("=" * 60)
    print(f"Pairs:      {len(PAIRS)}")
    print(f"Lookback:   {LOOKBACK_HOURS // 24} days")
    print(f"Coverage:   {COVERAGE:.0%}")
    print()

    results = []
    for coin_a, coin_b in PAIRS:
        try:
            results.append(ppc_for_pair(coin_a, coin_b))
            print()
        except Exception as e:
            print(f"  failed: {e}")
            print()

    if not results:
        print("No results.")
        return

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    df = pd.DataFrame(results)
    print(df.to_string(index=False))

    out_path = Path(
        "/home/hlbot/hyperliquid-stat-arb-bot/reports/ppc_cointegration.csv"
    )
    out_path.parent.mkdir(exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
