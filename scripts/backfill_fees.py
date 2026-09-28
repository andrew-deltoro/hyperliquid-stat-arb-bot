"""Apply fee + slippage model to existing trades.

Reads reports/trades.csv, computes gross PnL (from pnl_usd column),
subtracts round-trip fees and slippage, and prints the net result.

This does NOT modify the CSV — it just reports what the numbers
would look like with costs included.
"""
import pandas as pd
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
TRADES = REPO / "reports" / "trades.csv"

# Hyperliquid fee schedule (per side)
FEE_PCT_TAKER = 0.00045    # 0.045%
FEE_PCT_MAKER = 0.00015    # 0.015%
SLIPPAGE_BPS = 3.0         # 3 bps = 0.03% per side

# Which fee to use in the simulation
FEE_PCT = FEE_PCT_TAKER    # assume worst case (taker)


def main():
    t = pd.read_csv(TRADES)
    if len(t) == 0:
        print("No trades.")
        return

    notional = t.notional_usd.values

    # Round-trip cost per trade: 2 sides × (fee + slippage) × notional
    cost_per_trade = notional * ((FEE_PCT + SLIPPAGE_BPS / 10000.0) * 2.0)

    gross = t.pnl_usd.values
    net = gross - cost_per_trade

    print("=" * 70)
    print("FEE + SLIPPAGE ANALYSIS (backfill on existing dry-run trades)")
    print("=" * 70)
    print(f"Trades:              {len(t)}")
    print(f"Total notional:      ${notional.sum():.2f}")
    print(f"Avg trade notional:  ${notional.mean():.2f}")
    print()
    print(f"Gross PnL:           ${gross.sum():+.2f}")
    print(f"Total costs:         ${cost_per_trade.sum():.2f}")
    print(f"  (fees:             ${(notional * FEE_PCT * 2).sum():.2f})")
    print(f"  (slippage:         ${(notional * SLIPPAGE_BPS / 10000.0 * 2).sum():.2f})")
    print()
    print(f"NET PnL:             ${net.sum():+.2f}")
    print(f"Avg cost per trade:  ${cost_per_trade.mean():.2f}")
    print()
    print("=" * 70)
    print("PER-TRADE BREAKDOWN")
    print("=" * 70)
    print(f"{'pair':16s}  {'gross':>8s}  {'cost':>7s}  {'net':>8s}  {'exit':>16s}")
    print("-" * 70)
    for i in range(len(t)):
        print(f"{t.pair.iloc[i]:16s}  "
              f"${gross[i]:+7.2f}  "
              f"${cost_per_trade[i]:6.2f}  "
              f"${net[i]:+7.2f}  "
              f"{t.exit_reason.iloc[i]:>16s}")

    print()
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)

    # Adjusted metrics
    net_returns = net / notional
    wins = (net > 0).sum()
    total = len(net)

    print(f"Net win rate:        {wins}/{total} = {wins/total:.1%}")
    print(f"Net avg return:      {net_returns.mean()*100:+.3f}%")

    if net.sum() > 0:
        print(f"Edge survives costs: YES (net PnL ${net.sum():+.2f})")
    else:
        print(f"Edge survives costs: NO (net PnL ${net.sum():+.2f})")
        print("  → Do NOT scale until edge covers costs.")


if __name__ == "__main__":
    main()
