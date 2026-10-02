"""
mass_screener.py — Hyperliquid cointegration scanner.

Fetches all perps, runs Engle-Granger cointegration test on every pair
that passes a correlation pre-filter, ranks candidates by p-value.

Caches raw candle data so re-runs don't re-fetch.

Usage:
    python mass_screener.py                    # default 90d, corr>=0.5, EG<0.05
    python mass_screener.py --no-cache         # force re-fetch
    python mass_screener.py --corr 0.4 --eg 0.10  # looser thresholds
"""
from __future__ import annotations

import sys
import time
import pickle
import argparse
from pathlib import Path

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")

import numpy as np
import pandas as pd
import requests
from statsmodels.tsa.stattools import coint, adfuller

HL_URL = "https://api.hyperliquid.xyz/info"
REPO = Path("/home/hlbot/hyperliquid-stat-arb-bot")
CACHE_DIR = REPO / "reports" / ".screener_cache"

# Defaults
DEFAULT_LOOKBACK_HOURS = 24 * 90
DEFAULT_INTERVAL = "1h"
DEFAULT_MIN_BARS = 1500
DEFAULT_CORR_THRESHOLD = 0.5
DEFAULT_EG_THRESHOLD = 0.05
DEFAULT_CACHE_TTL_HOURS = 12


def post(req):
    r = requests.post(HL_URL, json=req, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_universe():
    data = post({"type": "meta"})
    return [a["name"] for a in data.get("universe", [])]


def fetch_candles(coin, interval="1h", lookback_hours=24 * 90):
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - lookback_hours * 3600 * 1000
    try:
        data = post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": interval,
            "startTime": start_ms, "endTime": end_ms,
        }})
        if not data:
            return None
        return {int(c["t"]): float(c["c"]) for c in data}
    except Exception as e:
        print(f"    fetch error {coin}: {e}")
        return None


def load_or_fetch_cache(lookback_hours, interval, ttl_hours):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"candles_{interval}_{lookback_hours}h.pkl"

    if cache_file.exists():
        age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
        if age_hours < ttl_hours:
            print(f"Loading cached candles ({age_hours:.1f}h old)")
            with open(cache_file, "rb") as f:
                return pickle.load(f)

    print("Fetching fresh candles...")
    universe = fetch_universe()
    print(f"Universe: {len(universe)} symbols")

    prices = {}
    for i, coin in enumerate(universe):
        if i % 20 == 0:
            print(f"  [{i}/{len(universe)}]...")
        c = fetch_candles(coin, interval, lookback_hours)
        if c and len(c) >= DEFAULT_MIN_BARS:
            prices[coin] = c
        time.sleep(0.05)

    with open(cache_file, "wb") as f:
        pickle.dump(prices, f)

    print(f"Cached {len(prices)} coins")
    return prices


def build_price_matrix(prices, min_overlap=1000):
    """Align all coin prices to common timestamps via union + forward-fill."""
    print("Aligning timestamps...")
    all_ts = set()
    for c in prices.values():
        all_ts.update(c.keys())
    all_ts = sorted(all_ts)
    print(f"  Total unique timestamps: {len(all_ts)}")

    coins = sorted(prices.keys())
    matrix = np.full((len(all_ts), len(coins)), np.nan)
    ts_idx = {t: i for i, t in enumerate(all_ts)}
    for j, coin in enumerate(coins):
        for t, v in prices[coin].items():
            matrix[ts_idx[t], j] = v

    df = pd.DataFrame(matrix, columns=coins)
    df = df.ffill().dropna()
    print(f"  After alignment: {df.shape[0]} bars x {df.shape[1]} coins")
    return df


def find_correlated_pairs(returns_df, threshold):
    print(f"Computing correlation matrix ({returns_df.shape[1]} coins)...")
    corr_matrix = returns_df.corr().values
    coins = returns_df.columns.tolist()
    n = len(coins)

    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            c = corr_matrix[i, j]
            if np.isfinite(c) and c >= threshold:
                pairs.append((coins[i], coins[j], float(c)))

    print(f"  Pairs with corr >= {threshold}: {len(pairs)}")
    return pairs


def test_cointegration(log_a, log_b):
    try:
        eg_stat, eg_pval, _ = coint(log_a, log_b)
        coefs = np.polyfit(log_b, log_a, 1)
        beta, alpha = float(coefs[0]), float(coefs[1])
        residual = log_a - beta * log_b - alpha
        adf_result = adfuller(residual, maxlag=24, autolag=None,
                              result_object=False)
        adf_pval = float(adf_result[1])

        # OU half-life
        lag = residual[:-1]
        diff = np.diff(residual)
        if lag.std() > 0:
            theta = float(np.polyfit(lag, diff, 1)[0])
            half_life = float(-np.log(2) / theta) if theta < 0 else float("inf")
        else:
            half_life = float("inf")

        return {
            "eg_pval": float(eg_pval),
            "adf_pval": adf_pval,
            "beta": beta,
            "alpha": alpha,
            "half_life_hours": half_life,
        }
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    parser.add_argument("--corr", type=float, default=DEFAULT_CORR_THRESHOLD)
    parser.add_argument("--eg", type=float, default=DEFAULT_EG_THRESHOLD)
    parser.add_argument("--ttl", type=int, default=DEFAULT_CACHE_TTL_HOURS)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--out", default=str(REPO / "reports" / "mass_screener_results.csv"))
    args = parser.parse_args()

    print("=" * 70)
    print("HYPERLIQUID MASS COINTEGRATION SCREENER")
    print("=" * 70)
    print(f"Lookback:  {args.lookback_hours // 24} days")
    print(f"Interval:  {args.interval}")
    print(f"Corr filter: >= {args.corr}")
    print(f"EG threshold: < {args.eg}")
    print()

    if args.no_cache:
        cf = CACHE_DIR / f"candles_{args.interval}_{args.lookback_hours}h.pkl"
        if cf.exists():
            cf.unlink()
            print("Cache cleared")

    prices = load_or_fetch_cache(args.lookback_hours, args.interval, args.ttl)
    print(f"Loaded {len(prices)} coins")
    print()

    price_df = build_price_matrix(prices)
    log_prices = np.log(price_df)
    returns_df = log_prices.diff().dropna()

    pairs = find_correlated_pairs(returns_df, args.corr)
    print()

    if not pairs:
        print("No pairs passed the correlation filter. Try lowering --corr.")
        return

    print(f"Running Engle-Granger on {len(pairs)} pairs...")
    results = []
    log_price_arr = log_prices.values
    coin_to_idx = {c: i for i, c in enumerate(log_prices.columns)}

    for k, (a, b, corr) in enumerate(pairs):
        if k % 500 == 0 and k > 0:
            print(f"  [{k}/{len(pairs)}] {len(results)} pass so far")
        la = log_price_arr[:, coin_to_idx[a]]
        lb = log_price_arr[:, coin_to_idx[b]]
        res = test_cointegration(la, lb)
        if res and res["eg_pval"] < args.eg:
            res["pair"] = f"{a}/{b}"
            res["coin_a"] = a
            res["coin_b"] = b
            res["corr"] = corr
            results.append(res)

    print(f"\n{len(results)} pairs pass EG p < {args.eg}")

    if not results:
        print("No candidates. Try relaxing --corr or --eg.")
        return

    df = pd.DataFrame(results)

    # Composite rank_score: weight cointegration strength against
    # half-life (20-60h sweet spot) and beta proximity to 1.
    def hl_penalty(hl):
        if hl is None or not np.isfinite(hl) or hl <= 0:
            return 100.0
        if hl < 20:
            return 5.0 + (20 - hl) / 20
        if hl <= 60:
            return 1.0
        if hl <= 96:
            return 1.0 + (hl - 60) / 36
        return 100.0

    df["hl_penalty"] = df["half_life_hours"].apply(hl_penalty)
    df["beta_penalty"] = (df["beta"] - 1.0).abs() + 1.0
    df["rank_score"] = (1.0 / df["eg_pval"]) / (df["hl_penalty"] * df["beta_penalty"])

    df = df.sort_values("rank_score", ascending=False).reset_index(drop=True)
    df = df[["rank_score", "pair", "coin_a", "coin_b", "eg_pval", "adf_pval",
             "beta", "corr", "half_life_hours", "hl_penalty"]]
    df.insert(0, "scanned_at_utc", pd.Timestamp.utcnow().isoformat())

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False)
    print(f"\nSaved {len(df)} candidates to {args.out}")

    print()
    print(f"{'pair':>24}  {'EG p':>8}  {'ADF p':>8}  {'corr':>7}  {'beta':>7}  {'HL(h)':>8}")
    print("-" * 76)
    for _, row in df.head(40).iterrows():
        hl = row["half_life_hours"]
        hl_str = f"{hl:.1f}" if hl < 1e4 else "inf"
        print(f"{row['pair']:>24}  {row['eg_pval']:>8.4f}  {row['adf_pval']:>8.4f}  {row['corr']:>7.4f}  {row['beta']:>7.3f}  {hl_str:>8}")


if __name__ == "__main__":
    main()
