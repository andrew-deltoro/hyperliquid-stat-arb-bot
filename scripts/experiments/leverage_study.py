"""
Leverage study.

Loads historical walk-forward trade data, simulates the pairs bot at
leverage levels from 1x to 10x, reports Sharpe / max DD / liquidation.

Assumptions:
  - Position notional = base_notional × L (fixed dollars per trade)
  - Equity compounds after each trade
  - No intra-trade liquidation (2×ATR stops prevent it)
  - Liquidation threshold: account equity < maintenance margin for next position
  - Sharpe measured from per-trade returns, annualized at TRADES_PER_YEAR
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
import numpy as np
import pandas as pd


REPORTS = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports")

BASE_NOTIONAL = 500.0          # current live notional per trade
BASE_EQUITY = 3100.0           # current live equity
TRADES_PER_YEAR = 40.0         # from historical avg
HL_MAINTENANCE = 0.025         # 2.5% of notional, HL maintenance margin
MAX_CONCURRENT = 10            # from config


def load_trades() -> pd.DataFrame:
    """Load all walk-forward ensemble CSVs."""
    dfs = []
    for f in sorted(REPORTS.glob("walk_forward_*_ensemble.csv")):
        try:
            df = pd.read_csv(f)
            if "pnl_pct" not in df.columns:
                continue
            df["source"] = f.stem
            dfs.append(df)
        except Exception as e:
            print(f"  skip {f.name}: {e}")
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


def simulate(trades: pd.DataFrame, L: float,
              base_notional: float = BASE_NOTIONAL,
              base_equity: float = BASE_EQUITY) -> dict:
    """Simulate account at leverage L."""
    trade_notional = base_notional * L
    # Approximate liquidation threshold: maintenance on max concurrent book
    liq_threshold = MAX_CONCURRENT * trade_notional * HL_MAINTENANCE
    # But also can't open a position without margin for one trade
    min_open_margin = trade_notional * HL_MAINTENANCE

    equity = base_equity
    curve = [equity]
    pnls = []
    liquidated = False
    liq_idx = None

    for i, row in trades.iterrows():
        pnl_pct = row.get("pnl_pct", 0.0)
        if pd.isna(pnl_pct):
            continue

        # Check margin
        if equity < max(min_open_margin, liq_threshold):
            liquidated = True
            liq_idx = i
            break

        dollar_pnl = trade_notional * (pnl_pct / 100.0)
        equity += dollar_pnl
        pnls.append(dollar_pnl)
        curve.append(equity)

        if equity <= 0:
            equity = 0.0
            liquidated = True
            liq_idx = i
            curve[-1] = 0.0
            break

    curve = np.array(curve)
    pnls = np.array(pnls)
    n = len(pnls)
    if n < 5:
        return {"L": L, "error": "insufficient trades"}

    # Per-trade returns relative to initial equity
    per_trade_ret = pnls / base_equity
    mean_r = per_trade_ret.mean()
    std_r = per_trade_ret.std()
    sharpe = mean_r / std_r * np.sqrt(TRADES_PER_YEAR) if std_r > 0 else 0.0

    peak = np.maximum.accumulate(curve)
    dd = (curve - peak) / np.maximum(peak, 1e-9)
    max_dd = float(dd.min())

    total_ret = float(curve[-1] / base_equity - 1)
    years = n / TRADES_PER_YEAR
    if curve[-1] > 0 and years > 0:
        ann_ret = float((curve[-1] / base_equity) ** (1 / years) - 1)
    else:
        ann_ret = -1.0

    return {
        "L": L,
        "n_trades": n,
        "final_equity": float(curve[-1]),
        "total_return": total_ret,
        "ann_return": ann_ret,
        "sharpe": float(sharpe),
        "max_dd": max_dd,
        "liquidated": liquidated,
        "liq_idx": liq_idx,
        "worst_trade": float(pnls.min()) if n else 0.0,
        "best_trade": float(pnls.max()) if n else 0.0,
        "avg_trade": float(pnls.mean()),
        "liq_threshold_usd": liq_threshold,
    }


def main():
    print("=" * 100)
    print("LEVERAGE STUDY — PAIRS BOT AT 1x THROUGH 10x")
    print("=" * 100)

    trades = load_trades()
    if trades.empty:
        print("No walk-forward data found.")
        print("Run `python scripts/walk_forward_ensemble.py` first.")
        return

    print(f"Loaded {len(trades)} trades from {trades['source'].nunique()} pair-files")
    print(f"Base equity: ${BASE_EQUITY:.0f}")
    print(f"Base notional per trade: ${BASE_NOTIONAL:.0f}")
    print(f"HL maintenance margin: {HL_MAINTENANCE*100:.1f}% of notional")
    print(f"Max concurrent positions: {MAX_CONCURRENT}")
    print()

    # Header
    print(f"{'L':>3}  {'N':>5}  {'Notional':>10}  {'Final$':>10}  "
          f"{'Total':>8}  {'AnnRet':>8}  {'Sharpe':>7}  {'MaxDD':>8}  "
          f"{'Worst':>8}  {'Liq?':>5}")
    print("-" * 100)

    results = []
    for L in [1, 2, 3, 4, 5, 7, 10]:
        r = simulate(trades, L)
        results.append(r)
        if "error" in r:
            print(f"{L:>3}  {r['error']}")
            continue
        print(f"{L:>3}  {r['n_trades']:>5}  "
              f"${BASE_NOTIONAL*L:>9.0f}  "
              f"${r['final_equity']:>9.2f}  "
              f"{r['total_return']*100:>7.1f}%  "
              f"{r['ann_return']*100:>7.1f}%  "
              f"{r['sharpe']:>7.2f}  "
              f"{r['max_dd']*100:>7.1f}%  "
              f"${r['worst_trade']:>7.2f}  "
              f"{'YES' if r['liquidated'] else ' no':>5}")

    valid = [r for r in results if "error" not in r]
    if not valid:
        return

    print()
    print("=" * 100)
    print("ANALYSIS")
    print("=" * 100)

    # Sharpe across L
    sharpe_spread = max(r["sharpe"] for r in valid) - min(r["sharpe"] for r in valid)
    print(f"  Sharpe spread across L=1..10: {sharpe_spread:.3f}")
    print(f"  (Near zero = Sharpe is leverage-invariant, as expected)")

    # Kelly
    best_growth = max(valid, key=lambda x: np.log(max(x["final_equity"], 1e-9)))
    best_sharpe = max(valid, key=lambda x: x["sharpe"])

    print(f"\n  Highest Sharpe:            L={best_sharpe['L']}x  "
          f"Sharpe={best_sharpe['sharpe']:.2f}")
    print(f"  Best geometric growth:     L={best_growth['L']}x  "
          f"Final=${best_growth['final_equity']:.0f}  "
          f"AnnRet={best_growth['ann_return']*100:.1f}%  "
          f"MaxDD={best_growth['max_dd']*100:.1f}%")

    # DD-constrained best
    dd_20 = [r for r in valid if r["max_dd"] > -0.20 and not r["liquidated"]]
    dd_30 = [r for r in valid if r["max_dd"] > -0.30 and not r["liquidated"]]
    if dd_20:
        b = max(dd_20, key=lambda x: x["ann_return"])
        print(f"  Best under -20% DD:        L={b['L']}x  "
              f"AnnRet={b['ann_return']*100:.1f}%  "
              f"MaxDD={b['max_dd']*100:.1f}%")
    if dd_30:
        b = max(dd_30, key=lambda x: x["ann_return"])
        print(f"  Best under -30% DD:        L={b['L']}x  "
              f"AnnRet={b['ann_return']*100:.1f}%  "
              f"MaxDD={b['max_dd']*100:.1f}%")

    # Liquidation summary
    liq = [r for r in valid if r["liquidated"]]
    if liq:
        print(f"\n  Liquidations detected at: "
              f"{', '.join(f'{r[chr(76)]}x' for r in liq)}")
    else:
        print(f"\n  No liquidations at any tested L level")

    # Recommendation
    print()
    print("=" * 100)
    print("RECOMMENDATION")
    print("=" * 100)
    print("""
  Theoretical Kelly for Sharpe ~0.6 at this vol:
    Full Kelly:  ~2-3x
    Half Kelly:  ~1-1.5x

  Practical leverage depends on your drawdown tolerance:
    - Conservative (accept -20% DD):    L=1x or 2x
    - Moderate (accept -30% DD):        L=2x or 3x
    - Aggressive (accept -50%+ DD):     L=3x to 5x
    - Reckless (may liquidate):         L>5x

  Key insight: Sharpe does NOT improve with leverage. What improves
  is the absolute return per unit of time. What worsens is the
  drawdown, in the same proportion. This is arithmetic, not preference.
""")


if __name__ == "__main__":
    main()
