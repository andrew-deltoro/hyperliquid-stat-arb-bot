"""Benchmark cointegration fit at various chain/core configurations."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pymc as pm
from src.hyperliquid_bot.data.ohlcv import aligned_close_prices


def fit(log_a, log_b, chains, cores, draws):
    t0 = time.time()
    with pm.Model() as model:
        alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
        beta = pm.Normal("beta", mu=1.0, sigma=1.0)
        sigma = pm.HalfCauchy("sigma", beta=1.0)
        nu = pm.Exponential("nu", lam=1/10.0)
        mu = alpha + beta * log_b
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)
        pm.sample(
            draws=draws, tune=draws, chains=chains, cores=cores,
            progressbar=False, random_seed=0,
        )
    return time.time() - t0


def main():
    print("Fetching ETH/BTC...")
    ts, ca, cb = aligned_close_prices("ETH", "BTC", interval="1h", lookback_hours=720)
    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    print(f"Got {len(ts)} bars\n")

    configs = [
        ("Current   (4 chains, 1 core, 1000 draws)", 4, 1, 1000),
        ("Fast      (2 chains, 2 cores, 500 draws)",  2, 2, 500),
        ("Turbo     (4 chains, 4 cores, 500 draws)",  4, 4, 500),
    ]

    for name, chains, cores, draws in configs:
        print(f"--- {name} ---")
        try:
            elapsed = fit(log_a, log_b, chains, cores, draws)
            print(f"  elapsed: {elapsed:.1f}s\n")
        except Exception as e:
            print(f"  ERROR: {e}\n")


if __name__ == "__main__":
    main()
