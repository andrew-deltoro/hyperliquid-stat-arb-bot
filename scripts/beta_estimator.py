"""
beta_estimator.py — Compute hedge ratios for funding candidates.

For each alt, regress its log returns against BTC and ETH returns
over the last 60 days of hourly candles. Report beta and R².
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")

import numpy as np
import pandas as pd
from src.hyperliquid_bot.data.ohlcv import aligned_close_prices

# Candidates from funding scan (top 5 by liquidity + magnitude)
CANDIDATES = ["TRX", "ADA", "ZRO", "PONS", "FARTCOIN", "USELESS"]
HEDGES = ["BTC", "ETH"]

LOOKBACK_HOURS = 24 * 60  # 60 days


def compute_beta(alt_log_ret: np.ndarray, hedge_log_ret: np.ndarray) -> tuple[float, float]:
    """OLS: alt_ret = alpha + beta * hedge_ret + eps. Return (beta, R^2)."""
    if len(alt_log_ret) != len(hedge_log_ret) or len(alt_log_ret) < 100:
        return float("nan"), float("nan")
    x = hedge_log_ret
    y = alt_log_ret
    x_mean = x.mean()
    y_mean = y.mean()
    cov = ((x - x_mean) * (y - y_mean)).sum() / len(x)
    var_x = ((x - x_mean) ** 2).sum() / len(x)
    if var_x <= 0:
        return float("nan"), float("nan")
    beta = cov / var_x
    # R²
    y_hat = y_mean + beta * (x - x_mean)
    ss_res = ((y - y_hat) ** 2).sum()
    ss_tot = ((y - y_mean) ** 2).sum()
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    return float(beta), float(r2)


def main():
    print("=" * 78)
    print("HEDGE β ESTIMATOR (60-day hourly log returns)")
    print("=" * 78)
    print()

    # Pre-load hedge price series
    hedge_data = {}
    for h in HEDGES:
        try:
            _, close_btc, _ = aligned_close_prices(h, "USDC", interval="1h", lookback_hours=LOOKBACK_HOURS)
            hedge_data[h] = None  # placeholder
        except Exception:
            pass

    # Simpler: for each (alt, hedge), fetch aligned series directly
    print(f"{'alt':>12}  {'hedge':>6}  {'beta':>8}  {'R²':>8}  {'n':>6}  {'verdict':>12}")
    print("-" * 62)

    results = []
    for alt in CANDIDATES:
        for hedge in HEDGES:
            try:
                _, ca, cb = aligned_close_prices(alt, hedge, interval="1h",
                                                  lookback_hours=LOOKBACK_HOURS)
                if len(ca) < 500:
                    print(f"{alt:>12}  {hedge:>6}  (insufficient: {len(ca)} bars)")
                    continue
                log_alt = np.diff(np.log(np.array(ca)))
                log_hedge = np.diff(np.log(np.array(cb)))
                beta, r2 = compute_beta(log_alt, log_hedge)
                # Verdict: hedge is good if R² >= 0.4
                verdict = "GOOD" if r2 >= 0.4 else ("OK" if r2 >= 0.2 else "WEAK")
                print(f"{alt:>12}  {hedge:>6}  {beta:>+8.4f}  {r2:>8.4f}  {len(ca):>6}  {verdict:>12}")
                results.append({"alt": alt, "hedge": hedge, "beta": beta, "r2": r2, "n": len(ca)})
            except Exception as e:
                print(f"{alt:>12}  {hedge:>6}  ERROR: {str(e)[:40]}")

    print()
    # Save to CSV
    if results:
        df = pd.DataFrame(results)
        out = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports/hedge_betas.csv")
        df.to_csv(out, index=False)
        print(f"Saved {len(df)} beta estimates to {out}")


if __name__ == "__main__":
    main()
