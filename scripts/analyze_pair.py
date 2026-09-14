"""
Full pipeline for one pair:
  1. Fetch aligned candles
  2. Fit Bayesian cointegration (Phase 1 model)
  3. Extract spread series
  4. Fit OU mean-reversion model on the spread
  5. Compute posterior reversion probability
  6. Compute position size
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import arviz as az

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
from src.hyperliquid_bot.stats.cointegration import fit_cointegration
from src.hyperliquid_bot.stats.mean_reversion import (
    fit_ou,
    simulate_reversion_probability,
    position_size,
)


import sys as _sys
COIN_A = _sys.argv[1] if len(_sys.argv) > 1 else "LINK"
COIN_B = _sys.argv[2] if len(_sys.argv) > 2 else "PYTH"


def main():
    print(f"=== Analyzing {COIN_A}/{COIN_B} ===\n")

    print(f"Fetching {COIN_A} and {COIN_B}...")
    ts, close_a, close_b = aligned_close_prices(
        COIN_A, COIN_B, interval="1h", lookback_hours=720,
    )
    print(f"Got {len(ts)} aligned hourly bars\n")

    log_a = np.log(np.array(close_a))
    log_b = np.log(np.array(close_b))

    # --- Phase 1: cointegration ---
    print("Fitting cointegration model...")
    coint = fit_cointegration(log_a, log_b, progressbar=True)

    print(f"\nCointegration posterior:")
    print(f"  alpha: {coint.alpha_mean:+.4f} ± {coint.alpha_sd:.4f}")
    print(f"  beta:  {coint.beta_mean:+.4f} ± {coint.beta_sd:.4f}")
    print(f"  sigma: {coint.sigma_mean:.5f} ± {coint.sigma_sd:.5f}")
    print(f"  nu:    {coint.nu_mean:.2f}")
    print(f"  converged: {coint.converged}")

    # Current spread
    current_spread = float(coint.spread[-1])
    mu_spread = float(coint.spread.mean())
    sd_spread = float(coint.spread.std())

    # Center the spread for OU fit
    spread_centered = coint.spread - mu_spread
    current_spread_centered = current_spread - mu_spread

    print(f"\nSpread snapshot (log scale):")
    print(f"  current:  {current_spread:+.5f}")
    print(f"  mean:     {mu_spread:+.5f}")
    print(f"  sd:       {sd_spread:.5f}")
    print(f"  naive z:  {current_spread_centered / (sd_spread + 1e-9):+.2f}")

    # --- Phase 2: OU mean reversion ---
    print(f"\nFitting OU mean-reversion model...")
    ou = fit_ou(spread_centered, progressbar=True)

    print(f"\nOU posterior:")
    print(f"  kappa:     {ou.kappa_mean:.4f} ± {ou.kappa_sd:.4f}")
    print(f"  half-life: {ou.half_life_bars:.1f} bars "
          f"({ou.half_life_bars:.1f} hours)")
    print(f"  sigma:     {ou.sigma_mean:.5f}")
    print(f"  nu:        {ou.nu_mean:.2f}")
    print(f"  converged: {ou.converged}")

    # Extract posterior samples for the simulation
    kappa_s = ou.idata.posterior["kappa"].stack(s=("chain", "draw")).values
    sigma_s = ou.idata.posterior["sigma"].stack(s=("chain", "draw")).values
    nu_s = ou.idata.posterior["nu"].stack(s=("chain", "draw")).values

        # --- Reversion probability at horizons scaled to the half-life ---
    print(f"\nPosterior predictive reversion simulation...")
    print(f"  (half-life = {ou.half_life_bars:.1f}h; "
          f"scaling horizons to it)")

    # Horizons: 0.5×, 1×, 2×, 3× the half-life
    hl = ou.half_life_bars
    horizons = [int(hl * f) for f in (0.5, 1.0, 2.0, 3.0)]

    np.random.seed(0)
    reversion_curves = {}
    for horizon in horizons:
        p = simulate_reversion_probability(
            current_spread=current_spread_centered,
            kappa_post=kappa_s,
            sigma_post=sigma_s,
            nu_post=nu_s,
            horizon=horizon,
            target_frac=0.5,
            paths_per_sample=5,
        )
        reversion_curves[horizon] = p
        print(f"  P(revert to 50% of dev | {horizon:3d}h = "
              f"{horizon/hl:.1f}× half-life) = {p:.3f}")

    # --- Position sizing: use 2× half-life as the trade horizon ---
    # Rationale: at 2× half-life, we expect ~75% of mean reversion complete
    trade_horizon = int(hl * 2.0)
    p_trade = reversion_curves.get(trade_horizon, 0.0)
    print(f"\n  Using trade horizon = {trade_horizon}h (2× half-life)")

    size = position_size(
        p_revert=p_trade,
        sigma=ou.sigma_mean,
        nu=ou.nu_mean,
        base_notional=1000.0,
    )

    # --- Signal output ---
    print(f"\n{'=' * 60}")
    print(f"SIGNAL")
    print(f"{'=' * 60}")
    direction = "SHORT" if current_spread_centered > 0 else "LONG"
    opposite = "LONG" if direction == "SHORT" else "SHORT"

    print(f"  Pair:              {COIN_A}/{COIN_B}")
    print(f"  Direction:         {direction} {COIN_A}, "
          f"{opposite} {COIN_B}")
    print(f"  Hedge ratio (β):   {coint.beta_mean:.4f}")
    print(f"  Half-life:         {hl:.1f} hours")
    print(f"  Trade horizon:     {trade_horizon}h "
          f"({trade_horizon/24:.1f} days)")
    print(f"  P(revert by {trade_horizon}h): {p_trade:.3f}")

    if size > 0:
        print(f"  Suggested notional: ${size:.2f}")
        print(f"    {COIN_A} leg: ${size/2:.2f}")
        print(f"    {COIN_B} leg: ${size/2*coint.beta_mean:.2f}")
        print(f"  Expected duration: ~{hl:.0f}h to hit +50% target")
    else:
        print(f"  Suggested notional: $0.00 (edge below threshold)")
        print(f"  Reason: P(revert) = {p_trade:.3f} < 0.55 at any tradeable horizon")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()