"""Fetch two coins, fit cointegration, plot the spread with mean & bands."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import matplotlib
matplotlib.use("Agg")  # non-interactive backend
import matplotlib.pyplot as plt

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
from src.hyperliquid_bot.stats.cointegration import fit_cointegration


def main(coin_a: str, coin_b: str, lookback_hours: int = 720):
    ts, close_a, close_b = aligned_close_prices(
        coin_a, coin_b, interval="1h", lookback_hours=lookback_hours,
    )
    log_a = np.log(np.array(close_a))
    log_b = np.log(np.array(close_b))

    print(f"Fitting {coin_a}/{coin_b}...")
    fit = fit_cointegration(log_a, log_b, progressbar=False)

    # Spread as a time series
    spread = fit.spread
    mu = spread.mean()
    sd = spread.std()

    # Build time axis (hours from start)
    t = np.arange(len(spread))

    # Plot
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(t, spread, color="black", linewidth=1.2, label="spread")

    # Mean and 1/2 sigma bands
    ax.axhline(mu, color="blue", linestyle="--", label=f"mean = {mu:+.4f}")
    for k, alpha in [(1, 0.25), (2, 0.15), (3, 0.08)]:
        ax.axhline(mu + k * sd, color="red", alpha=alpha)
        ax.axhline(mu - k * sd, color="red", alpha=alpha)

    # Current point
    ax.scatter([t[-1]], [spread[-1]], color="red", s=80, zorder=5,
               label=f"now: z = {(spread[-1] - mu) / sd:+.2f}")

    ax.set_title(f"{coin_a}/{coin_b} spread  —  β = {fit.beta_mean:.3f}, "
                 f"ν = {fit.nu_mean:.1f}, half-life unknown (see OU fit)")
    ax.set_xlabel("hours ago → now")
    ax.set_ylabel(f"log({coin_a}) − α − β·log({coin_b})")
    ax.legend(loc="best")
    ax.grid(alpha=0.3)

    out = Path(__file__).resolve().parent.parent / "reports" / \
          f"spread_{coin_a}_{coin_b}.png"
    out.parent.mkdir(exist_ok=True)
    plt.tight_layout()
    plt.savefig(out, dpi=120)
    print(f"Saved: {out}")

    # Also print summary
    print(f"Current spread: {spread[-1]:+.5f}")
    print(f"Mean:           {mu:+.5f}")
    print(f"SD:             {sd:.5f}")
    print(f"z-score:        {(spread[-1] - mu) / sd:+.2f}")


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else "LINK"
    b = sys.argv[2] if len(sys.argv) > 2 else "PYTH"
    hours = int(sys.argv[3]) if len(sys.argv) > 3 else 720
    main(a, b, hours)