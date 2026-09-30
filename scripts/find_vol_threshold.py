"""Find the BTC vol threshold that separates good and bad trade regimes."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from src.hyperliquid_bot.data.ohlcv import fetch_candles


def realized_vol_24h(closes: np.ndarray) -> float:
    """Annualized 24h realized vol from hourly closes."""
    returns = np.diff(np.log(closes))
    if len(returns) < 24:
        return 0.0
    last_24 = returns[-24:]
    return float(last_24.std() * np.sqrt(24 * 365))


def main():
    print("Fetching BTC hourly candles...")
    candles = fetch_candles("BTC", interval="1h", lookback_hours=24 * 180)
    closes = np.array([c.c for c in candles])
    print(f"Got {len(closes)} bars\n")

    # Compute rolling 24h vol every 24 hours
    vols = []
    for i in range(24, len(closes), 24):
        v = realized_vol_24h(closes[:i])
        vols.append(v)

    vols = np.array(vols)
    print("=== BTC 24h Realized Vol Distribution (180 days) ===")
    for p in [10, 25, 50, 75, 90, 95]:
        print(f"  {p:>2}th percentile: {np.percentile(vols, p)*100:.1f}%")
    print(f"  mean: {vols.mean()*100:.1f}%")
    print(f"  max:  {vols.max()*100:.1f}%")
    print()

    # Suggest thresholds
    p75 = np.percentile(vols, 75)
    p90 = np.percentile(vols, 90)
    print("=== Suggested thresholds ===")
    print(f"  Conservative (25% of days skipped): {p75*100:.1f}%")
    print(f"  Strict      (10% of days skipped): {p90*100:.1f}%")


if __name__ == "__main__":
    main()
