"""Trade-level Sharpe, drawdown, and other performance metrics."""
import pandas as pd
import numpy as np
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
TRADES = REPO / "reports" / "trades.csv"


def main():
    if not TRADES.exists():
        print(f"ERROR: no trades file at {TRADES}")
        return

    t = pd.read_csv(TRADES)
    if len(t) == 0:
        print("No closed trades yet.")
        return

    # Returns per trade as fraction of notional
    returns = t.pnl_pct.values
    durations_hours = t.elapsed_hours.values

    print("=" * 60)
    print("TRADE STATISTICS")
    print("=" * 60)
    print(f"Closed trades:           {len(t)}")
    print(f"Mean return per trade:   {returns.mean()*100:+.3f}%")
    print(f"Std return per trade:    {returns.std()*100:.3f}%")
    print(f"Mean duration:           {durations_hours.mean():.1f}h")
    print(f"Median duration:         {np.median(durations_hours):.1f}h")
    print()

    # Win/loss breakdown
    wins = returns[returns > 0]
    losses = returns[returns < 0]
    print(f"Wins:                    {len(wins)}")
    print(f"Losses:                  {len(losses)}")
    if len(wins) > 0:
        print(f"Avg win:                 {wins.mean()*100:+.3f}%")
    if len(losses) > 0:
        print(f"Avg loss:                {losses.mean()*100:+.3f}%")
    if len(losses) > 0 and len(wins) > 0:
        print(f"Win/Loss ratio:          {abs(wins.mean() / losses.mean()):.2f}")
    print()

    # Trade-level Sharpe (un-annualized)
    if returns.std() > 0:
        sharpe_trade = returns.mean() / returns.std()
        print(f"Sharpe per trade:        {sharpe_trade:.3f}")

        # Annualize using trade frequency
        mean_duration_h = durations_hours.mean()
        trades_per_year = (365 * 24) / mean_duration_h
        print(f"Trades per year (est):   {trades_per_year:.0f}")

        # Annualized Sharpe
        sharpe_annual = sharpe_trade * np.sqrt(trades_per_year)
        print(f"Annualized Sharpe:       {sharpe_annual:.2f}")
    print()

    # Max drawdown
    t_sorted = t.sort_values("exit_time_iso")
    cum = t_sorted.pnl_usd.cumsum()
    peak = cum.cummax()
    dd = cum - peak
    print(f"Max drawdown:            ${dd.min():.2f}")
    if cum.max() > 0:
        print(f"Max drawdown %:          {(dd.min() / cum.max() * 100):.1f}%")
    print()

    print("=" * 60)
    print("VERDICT")
    print("=" * 60)
    print(f"Total PnL:               ${t.pnl_usd.sum():+.2f}")

    if returns.std() > 0:
        if sharpe_annual >= 2.0:
            print("Sharpe > 2.0:  Strong edge. Scale confidently.")
        elif sharpe_annual >= 1.0:
            print("Sharpe 1.0-2.0: Tradeable. Scale carefully.")
        elif sharpe_annual >= 0.5:
            print("Sharpe 0.5-1.0: Marginal. Collect more data.")
        else:
            print("Sharpe < 0.5:  Edge too thin. Do not scale.")


if __name__ == "__main__":
    main()
