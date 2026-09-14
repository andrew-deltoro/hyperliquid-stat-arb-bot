"""Compare pooled (multilevel) vs unpooled OU fits across pairs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
from src.hyperliquid_bot.stats.cointegration import fit_cointegration
from src.hyperliquid_bot.stats.mean_reversion import fit_ou
from src.hyperliquid_bot.stats.multilevel_ou import fit_multilevel_ou


def main():
    reports = Path(__file__).resolve().parent.parent / "reports"
    scans = sorted(reports.glob("scan_*.csv"))
    if not scans:
        print("No scan CSV. Run scripts/scan_all_pairs.py first.")
        return
    scan_path = scans[-1]
    print(f"Using scan: {scan_path.name}")

    df = pd.read_csv(scan_path).sort_values("score").head(5)
    pairs = [tuple(row["pair"].split("/")) for _, row in df.iterrows()]
    print(f"Testing on top {len(pairs)} pairs:\n")
    for a, b in pairs:
        print(f"  {a}/{b}")
    print()

    # Fetch spreads
    spreads = {}
    for a, b in pairs:
        name = f"{a}/{b}"
        print(f"Fitting cointegration for {name}...")
        ts, ca, cb = aligned_close_prices(a, b, interval="1h", lookback_hours=720)
        la, lb = np.log(np.array(ca)), np.log(np.array(cb))
        coint = fit_cointegration(la, lb, progressbar=False)
        spreads[name] = coint.spread - coint.spread.mean()

    # Unpooled: fit each pair independently
    print("\nFitting UNPOOLED OU per pair...")
    unpooled = {}
    for name, s in spreads.items():
        ou = fit_ou(s, draws=500, tune=500, chains=2, progressbar=False)
        unpooled[name] = ou

    # Pooled: fit all pairs together
    print("\nFitting POOLED OU across all pairs...")
    pooled = fit_multilevel_ou(spreads, progressbar=True)

    # Compare
    print(f"\n=== Comparison: unpooled vs pooled ===")
    print(f"{'pair':>12}  {'κ_unpooled':>12}  {'κ_pooled':>12}  "
          f"{'HL_unpooled':>12}  {'HL_pooled':>12}  {'shrinkage':>10}")
    print("-" * 80)
    for name in pooled.pairs:
        ku = unpooled[name].kappa_mean
        kp = pooled.kappa_pair_means[name]
        hlu = unpooled[name].half_life_bars
        hlp = pooled.half_life_pair[name]
        # shrinkage = how far pooled moved toward population
        denom = abs(ku - pooled.kappa_pop_mean) + 1e-9
        shrink = abs(ku - kp) / denom
        print(f"{name:>12}  {ku:>12.4f}  {kp:>12.4f}  "
              f"{hlu:>10.1f}h  {hlp:>10.1f}h  {shrink:>+9.2f}")

    print(f"\n=== Population posterior ===")
    print(f"  κ_pop: {pooled.kappa_pop_mean:.4f} "
          f"± {pooled.kappa_pop_sd:.4f}")
    print(f"  89% HDI: [{pooled.kappa_pop_hdi_low:.4f}, "
          f"{pooled.kappa_pop_hdi_high:.4f}]")
    print(f"  implied half-life: {np.log(0.5)/np.log(1-pooled.kappa_pop_mean):.1f}h")
    print(f"  ν (residual tails): {pooled.nu_mean:.2f}")
    print(f"  converged: {pooled.converged}")


if __name__ == "__main__":
    main()