"""Bayesian cointegration fit for a single pair."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
from src.hyperliquid_bot.stats.cointegration import fit_cointegration, fit_null_model


def main():
    coin_a, coin_b = "ETH", "BTC"
    print(f"Fetching {coin_a} and {coin_b}...")
    ts, close_a, close_b = aligned_close_prices(
        coin_a, coin_b, interval="1h", lookback_hours=720,
    )
    print(f"Got {len(ts)} aligned hourly bars\n")

    log_a = np.log(np.array(close_a))
    log_b = np.log(np.array(close_b))

    print("Fitting cointegration model (NUTS, 4 chains)...")
    fit = fit_cointegration(log_a, log_b)

    print(f"\n=== Posterior (mean ± sd) ===")
    print(f"  alpha: {fit.alpha_mean:+.4f} ± {fit.alpha_sd:.4f}")
    print(f"  beta:  {fit.beta_mean:+.4f} ± {fit.beta_sd:.4f}")
    print(f"  sigma: {fit.sigma_mean:.5f} ± {fit.sigma_sd:.5f}")
    print(f"  nu:    {fit.nu_mean:.2f}    (Student-t dof; low = fat tails)")
    print(f"  converged: {fit.converged}")
    print(f"  WAIC: {fit.waic:.2f}")

    print("\nFitting null model...")
    null_waic = fit_null_model(log_a)
    delta = fit.waic - null_waic
    print(f"  null WAIC: {null_waic:.2f}")
    print(f"  ΔWAIC:     {delta:+.2f}   (negative = cointegration wins)")

    current_spread = fit.spread[-1]
    mu_spread = fit.spread.mean()
    sd_spread = fit.spread.std()
    z = (current_spread - mu_spread) / (sd_spread + 1e-9)
    print(f"\n=== Current spread ===")
    print(f"  current: {current_spread:+.5f}")
    print(f"  mean:    {mu_spread:+.5f}")
    print(f"  sd:      {sd_spread:.5f}")
    print(f"  z:       {z:+.2f}")


if __name__ == "__main__":
    main()