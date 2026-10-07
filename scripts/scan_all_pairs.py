"""
Multi-pair Bayesian cointegration scanner.

For each candidate pair:
  1. Fetch aligned OHLCV from Hyperliquid
  2. Fit Student-t cointegration model
  3. Fit null model (no relationship)
  4. Compute ΔWAIC = coint_deviance - null_deviance (negative = coint wins)
  5. Score = ΔWAIC / n_observations  (normalized by sample size)

Results ranked by score. More negative score = stronger cointegration.
"""
import sys
import time
import csv
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
from src.hyperliquid_bot.stats.cointegration import (
    fit_cointegration,
    fit_null_model,
)


# ---------------------------------------------------------------------------
# CANDIDATE PAIRS
# ---------------------------------------------------------------------------
# Curated to cover several cointegration regimes:
#   - L2 sector:      ARB/OP, MATIC/ARB
#   - L1 sector:      SOL/AVAX, APT/SUI, SEI/SUI
#   - DeFi:           AAVE/COMP, UNI/SUSHI, MKR/AAVE, CRV/BAL
#   - Oracles:        LINK/PYTH
#   - Meme:           DOGE/SHIB, WIF/PEPE
#   - Modular:        INJ/TIA
#   - Cosmos:         ATOM/OSMO
#   - Control:        ETH/BTC  (already known cointegrated)
# ---------------------------------------------------------------------------
CANDIDATE_PAIRS = [
    # Sector pairs
    ("ARB", "OP"),
    ("SOL", "AVAX"),
    ("LINK", "PYTH"),
    ("AAVE", "COMP"),
    ("UNI", "SUSHI"),
    ("MKR", "AAVE"),
    ("CRV", "BAL"),
    ("DOGE", "SHIB"),
    ("WIF", "PEPE"),
    ("MATIC", "ARB"),
    ("INJ", "TIA"),
    ("SEI", "SUI"),
    ("APT", "SUI"),
    ("ATOM", "OSMO"),
    # Control
    ("ETH", "BTC"),
]

INTERVAL = "1h"
LOOKBACK_HOURS = 720           # 30 days
DRAWS = 500                     # fewer draws for scan speed
TUNE = 500
CHAINS = 4


# ---------------------------------------------------------------------------
def scan_pair(coin_a: str, coin_b: str) -> dict | None:
    """Run the full scan on one pair. Returns a result dict or None on failure."""
    t0 = time.time()
    try:
        ts, close_a, close_b = aligned_close_prices(
            coin_a, coin_b, interval=INTERVAL, lookback_hours=LOOKBACK_HOURS,
        )
    except Exception as e:
        print(f"  fetch failed: {e}")
        return None

    n = len(ts)
    if n < 200:
        print(f"  insufficient data: {n} bars")
        return None

    log_a = np.log(np.array(close_a))
    log_b = np.log(np.array(close_b))

    # --- Fit cointegration model ---
    try:
        fit = fit_cointegration(
            log_a, log_b, draws=DRAWS, tune=TUNE, chains=CHAINS,
            progressbar=False,
        )
    except Exception as e:
        print(f"  coint fit failed: {e}")
        return None

    # --- Fit null model ---
    try:
        null_waic = fit_null_model(
            log_a, draws=DRAWS, tune=TUNE, chains=CHAINS, progressbar=False,
        )
    except Exception as e:
        print(f"  null fit failed: {e}")
        return None

    delta_waic = fit.deviance - null_waic       # negative = coint wins
    score = delta_waic / n                   # normalized by sample size

    # Current signal
    current_spread = fit.spread[-1]
    mu_spread = fit.spread.mean()
    sd_spread = fit.spread.std()
    z = (current_spread - mu_spread) / (sd_spread + 1e-9)

    elapsed = time.time() - t0

    return {
        "pair": f"{coin_a}/{coin_b}",
        "n_obs": n,
        "beta": fit.beta_mean,
        "beta_sd": fit.beta_sd,
        "sigma": fit.sigma_mean,
        "nu": fit.nu_mean,
        "converged": fit.converged,
        "coint_waic": fit.deviance,
        "null_waic": null_waic,
        "delta_waic": delta_waic,
        "score": score,
        "z_current": z,
        "elapsed_s": elapsed,
    }


# ---------------------------------------------------------------------------
def main():
    print(f"Scanning {len(CANDIDATE_PAIRS)} candidate pairs")
    print(f"Interval: {INTERVAL}  Lookback: {LOOKBACK_HOURS}h  "
          f"Draws: {DRAWS}  Tune: {TUNE}  Chains: {CHAINS}\n")

    results = []
    for i, (a, b) in enumerate(CANDIDATE_PAIRS, 1):
        print(f"[{i:2d}/{len(CANDIDATE_PAIRS)}] {a}/{b}")
        r = scan_pair(a, b)
        if r is None:
            print("  SKIPPED\n")
            continue
        results.append(r)
        print(f"  n={r['n_obs']:4d}  "
              f"beta={r['beta']:+.3f}±{r['beta_sd']:.3f}  "
              f"nu={r['nu']:.1f}  "
              f"ΔWAIC={r['delta_waic']:+.1f}  "
              f"score={r['score']:+.3f}  "
              f"z={r['z_current']:+.2f}  "
              f"({r['elapsed_s']:.0f}s)\n")

    if not results:
        print("No pairs completed successfully.")
        return

    # --- Rank by score (most negative = strongest cointegration) ---
    results.sort(key=lambda x: x["score"])

    print("\n" + "=" * 100)
    print("RANKED RESULTS (by ΔWAIC per observation; more negative = stronger cointegration)")
    print("=" * 100)
    print(f"{'rank':>4}  {'pair':>12}  {'n':>4}  {'beta':>16}  "
          f"{'nu':>5}  {'ΔWAIC':>10}  {'score':>8}  {'z_now':>7}  {'conv':>5}")
    print("-" * 100)
    for i, r in enumerate(results, 1):
        print(f"{i:>4}  {r['pair']:>12}  {r['n_obs']:>4}  "
              f"{r['beta']:>+7.3f}±{r['beta_sd']:.3f}  "
              f"{r['nu']:>5.2f}  "
              f"{r['delta_waic']:>+10.1f}  "
              f"{r['score']:>+8.3f}  "
              f"{r['z_current']:>+7.2f}  "
              f"{str(r['converged']):>5}")

    # --- Save to CSV ---
    out_dir = Path(__file__).resolve().parent.parent / "reports"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"scan_{stamp}.csv"

    fieldnames = [
        "pair", "n_obs", "beta", "beta_sd", "sigma", "nu", "converged",
        "coint_waic", "null_waic", "delta_waic", "score", "z_current", "elapsed_s",
    ]
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()