"""
Prior Predictive Simulation for the cointegration model.
McElreath Ch 2 - Small Worlds and Large Worlds.
"""
from __future__ import annotations

import sys
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")

import numpy as np
import pandas as pd
import pymc as pm
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices

PAIRS = [("ETH", "BTC"), ("SOL", "AVAX"), ("AAVE", "COMP"), ("LINK", "PYTH")]
N_SIM = 500
OUT_PATH = "/home/hlbot/hyperliquid-stat-arb-bot/reports/ppc_prior.png"
CSV_PATH = "/home/hlbot/hyperliquid-stat-arb-bot/reports/ppc_prior.csv"


def prior_check(coin_a, coin_b, ax):
    pair = f"{coin_a}/{coin_b}"
    ts, ca, cb = aligned_close_prices(
        coin_a, coin_b, interval="1h", lookback_hours=24 * 30,
    )
    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    n_obs = len(log_a)

    with pm.Model() as model:
        alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
        beta = pm.Normal("beta", mu=1.0, sigma=1.0)
        sigma = pm.HalfCauchy("sigma", beta=1.0)
        nu = pm.Exponential("nu", lam=1 / 10.0)
        mu = alpha + beta * log_b
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)
        idata = pm.sample_prior_predictive(draws=N_SIM, random_seed=0)

    # Robust extraction: squeeze chain dim, then force shape (N_SIM, n_obs)
    pp = idata.prior_predictive["obs"]
    if "chain" in pp.dims:
        pp = pp.isel(chain=0)
    pp = pp.values
    # If orientation is wrong, transpose
    if pp.shape[0] != N_SIM:
        pp = pp.T
    assert pp.shape == (N_SIM, n_obs), f"unexpected shape {pp.shape}"

    lower = np.percentile(pp, 5.5, axis=0)
    upper = np.percentile(pp, 94.5, axis=0)

    obs_min, obs_max = log_a.min(), log_a.max()
    # Robust range: 5.5 to 94.5 percentiles of prior predictive
    p_lo = np.percentile(pp, 5.5)
    p_hi = np.percentile(pp, 94.5)
    raw_min = pp.min()
    raw_max = pp.max()
    span_robust = p_hi - p_lo
    span_obs = obs_max - obs_min
    ratio = span_robust / span_obs if span_obs > 0 else np.inf

    print(f"--- {pair} ---")
    print(f"  Observed log_a:     [{obs_min:+.3f}, {obs_max:+.3f}]  span={span_obs:.3f}")
    print(f"  Prior 5.5–94.5%:    [{p_lo:+.3f}, {p_hi:+.3f}]  span={span_robust:.3f}")
    print(f"  Prior raw min/max:  [{raw_min:+.2e}, {raw_max:+.2e}]")
    print(f"  Span ratio:         {ratio:.2f}x  (target: 3 to 100)")
    if ratio < 3:
        print(f"  ⚠️  Priors too narrow")
    elif ratio > 100:
        print(f"  ⚠️  Priors too wide")
    else:
        print(f"  ✅ Prior envelope reasonable")
    print()

    # Plot: scatter observed, prior predictive envelope, clip y-axis
    order = np.argsort(log_b)
    ax.scatter(log_b, log_a, s=1, alpha=0.3, color="black", label="observed")
    ax.fill_between(
        log_b[order], lower[order], upper[order],
        alpha=0.3, color="steelblue", label="89% prior predictive",
    )
    ax.set_title(pair)
    ax.set_xlabel("log(price_b)")
    ax.set_ylabel("log(price_a)")
    # Clip to a sane range so the plot is readable
    y_lo = min(obs_min, p_lo) - 1
    y_hi = max(obs_max, p_hi) + 1
    ax.set_ylim(y_lo, y_hi)
    ax.legend(fontsize=7)

    return {
        "pair": pair,
        "n_obs": n_obs,
        "obs_span": float(span_obs),
        "prior_span": float(span_robust),
        "span_ratio": float(ratio),
        "raw_min": float(raw_min),
        "raw_max": float(raw_max),
    }


def main():
    print("=" * 60)
    print("PRIOR PREDICTIVE CHECK (Ch 2)")
    print("=" * 60)

    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    rows = []
    for (coin_a, coin_b), ax in zip(PAIRS, axes.flat):
        try:
            rows.append(prior_check(coin_a, coin_b, ax))
        except Exception as e:
            print(f"  {coin_a}/{coin_b} failed: {e}")
            ax.set_title(f"{coin_a}/{coin_b} FAILED")

    plt.tight_layout()
    plt.savefig(OUT_PATH, dpi=100)
    print(f"Saved plot: {OUT_PATH}")

    if rows:
        df = pd.DataFrame(rows)
        df.to_csv(CSV_PATH, index=False)
        print(f"Saved CSV:  {CSV_PATH}")


if __name__ == "__main__":
    main()
