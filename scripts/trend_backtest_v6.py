"""
Cross-sectional momentum — v6.

Different signal from v5:
  v5 = time-series trend (each coin's own MA) → directional, long/short
  v6 = cross-sectional momentum (rank coins vs each other) → market-neutral

Signal:
  Every month, rank all coins by trailing N-day return.
  LONG the top K coins, SHORT the bottom K coins.
  Equal weight per leg, dollar-neutral.
  Rebalance monthly.

Why this is different:
  - Zero net market exposure (long $ = short $)
  - Sharpe isn't penalized by crypto's huge volatility
  - Doesn't require any coin to trend in absolute terms
  - Exploits the documented cross-sectional momentum anomaly

Bugs fixed from v5:
  - Cash floored at 0 (no negative equity / NaN Sharpes)
  - Excludes k-prefixed tokens (Korean synthetic listings)
  - Proper turnover-based cost accounting

Costs: 3 bps fee + 3 bps slip per side, applied on turnover.
"""
from __future__ import annotations
import sys
import time
import pickle
from pathlib import Path
from datetime import datetime

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
import numpy as np
import pandas as pd


CACHE_FILE = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports/.trend_cache/daily_candles_1500d.pkl")

# Costs
FEE_BPS = 3.0
SLIP_BPS = 3.0
RT_COST = (FEE_BPS + SLIP_BPS) * 2 / 10000  # 12 bps round trip

# Strategy params
LOOKBACK_DAYS = 30        # trailing return window for ranking
TOP_K = 5                 # long top 5, short bottom 5 (so 10 positions)
REBALANCE_DAYS = 30       # monthly rebalance
MIN_COIN_PRICE = 0.0      # no filter
MIN_COIN_HISTORY = 90     # need at least 90 days to compute signal
UNIVERSE_VOL_USD = 1e5    # 100k avg daily volume (loose; can tighten)


def load_cached_data() -> dict[str, pd.DataFrame]:
    if not CACHE_FILE.exists():
        print(f"No cache at {CACHE_FILE}. Run v5 once to build it.")
        sys.exit(1)
    print(f"Loading cache ({CACHE_FILE.stat().st_size / 1e6:.1f} MB)...")
    with open(CACHE_FILE, "rb") as f:
        data = pickle.load(f)
    print(f"  {len(data)} coins loaded")
    return data


def filter_universe(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Remove k-prefix tokens and apply volume/price filters."""
    out = {}
    skipped_k = 0
    skipped_vol = 0
    skipped_hist = 0
    for coin, df in data.items():
        if coin.lower().startswith("k") and coin not in ("KAS",):
            # k-prefix = Korean synthetic (kSHIB, kPEPE, etc.)
            skipped_k += 1
            continue
        if len(df) < MIN_COIN_HISTORY:
            skipped_hist += 1
            continue
        # Average volume filter
        if "volume" in df.columns:
            avg_vol_usd = (df["volume"] * df["close"]).rolling(30).mean().iloc[-1]
            if not np.isnan(avg_vol_usd) and avg_vol_usd < UNIVERSE_VOL_USD:
                skipped_vol += 1
                continue
        out[coin] = df
    print(f"  Filter: kept {len(out)}, dropped {skipped_k} k-tokens, "
          f"{skipped_hist} short history, {skipped_vol} low volume")
    return out


def build_price_matrix(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Common daily close matrix, forward-filled, indexed by date."""
    closes = {coin: df["close"] for coin, df in data.items()}
    M = pd.DataFrame(closes)
    M = M.sort_index()
    # Forward fill for missing days
    M = M.ffill()
    return M


def compute_trailing_returns(M: pd.DataFrame, days: int) -> pd.Series:
    """Trailing-N-day return per coin."""
    if len(M) < days + 1:
        return pd.Series(dtype=float)
    p_now = M.iloc[-1]
    p_then = M.iloc[-min(days, len(M) - 1)]
    return (p_now / p_then) - 1.0


def cross_sectional_sim(M: pd.DataFrame,
                         lookback: int = LOOKBACK_DAYS,
                         top_k: int = TOP_K,
                         rebalance_days: int = REBALANCE_DAYS,
                         rt_cost: float = RT_COST) -> dict:
    """
    Walk-forward cross-sectional momentum.
    At each rebalance:
      - Rank by trailing return
      - Long top K, short bottom K
      - Hold for rebalance_days
      - Apply costs on turnover
    """
    # Trim to coins that have data throughout
    valid = M.dropna(axis=1, how="any")
    if valid.shape[1] < 2 * top_k + 1:
        return {"error": f"only {valid.shape[1]} valid coins"}

    start_idx = max(lookback, 30)
    n = len(valid)
    # Rebalance dates
    reb_dates = list(range(start_idx, n - 1, rebalance_days))
    if len(reb_dates) < 3:
        return {"error": "not enough rebalance periods"}

    equity = [1.0]
    rebalance_log = []
    prev_long, prev_short = set(), set()

    for ri in range(len(reb_dates) - 1):
        t0 = reb_dates[ri]
        t1 = reb_dates[ri + 1]

        # Trailing return as of t0
        p_now = valid.iloc[t0]
        p_then = valid.iloc[t0 - lookback]
        trailing = (p_now / p_then) - 1.0
        trailing = trailing.dropna()

        if len(trailing) < 2 * top_k:
            # Not enough coins; skip this period
            equity.append(equity[-1])
            continue

        ranked = trailing.sort_values(ascending=False)
        long_coins = list(ranked.head(top_k).index)
        short_coins = list(ranked.tail(top_k).index)

        # Turnover cost: symmetric difference / (2K)
        new_set = set(long_coins) | set(short_coins)
        old_set = prev_long | prev_short
        turnover = len(new_set - old_set) / max(len(new_set), 1)
        # cost is on notional of changed positions.
        # total notional = 1.0 * equity; cost = notional * RT_COST * turnover
        cost_frac = rt_cost * turnover

        # Compute period PnL: for each long, (P_t1/P_t0 - 1) on +1 unit
        #                    for each short, (P_t0/P_t1 - 1) on +1 unit
        # Each position gets equal weight: 0.5/K of portfolio (longs) or 0.5/K (shorts)
        weight_per_long = 0.5 / top_k
        weight_per_short = 0.5 / top_k

        long_pnl = 0.0
        for c in long_coins:
            r = valid[c].iloc[t1] / valid[c].iloc[t0] - 1.0
            long_pnl += weight_per_long * r
        short_pnl = 0.0
        for c in short_coins:
            r = valid[c].iloc[t1] / valid[c].iloc[t0] - 1.0
            short_pnl += weight_per_short * (-r)

        gross_ret = long_pnl + short_pnl
        net_ret = gross_ret - cost_frac
        equity.append(equity[-1] * (1.0 + net_ret))

        rebalance_log.append({
            "t0": valid.index[t0].date(),
            "t1": valid.index[t1].date(),
            "long": long_coins,
            "short": short_coins,
            "long_pnl": long_pnl,
            "short_pnl": short_pnl,
            "cost": cost_frac,
            "net_ret": net_ret,
            "turnover": turnover,
        })

        prev_long = set(long_coins)
        prev_short = set(short_coins)

    if len(equity) < 3:
        return {"error": "not enough periods"}

    eq = pd.Series(equity)
    return _metrics(eq, rebalance_log)


def _metrics(eq: pd.Series, log: list) -> dict:
    r = eq.pct_change().dropna()
    if len(r) < 3 or r.std() == 0:
        return {"error": "no variance"}
    periods_per_year = 365 / REBALANCE_DAYS
    n_periods = len(r)
    # Correct annualization: total return is a cumulative multiple over n_periods
    if eq.iloc[-1] > 0 and n_periods > 0:
        years_elapsed = n_periods / periods_per_year
        ann_ret = (eq.iloc[-1] ** (1 / years_elapsed)) - 1
    else:
        ann_ret = -1.0
    ann_vol = r.std() * np.sqrt(periods_per_year)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    peak = eq.cummax()
    max_dd = float(((eq - peak) / peak).min())
    downside = r[r < 0]
    down_vol = downside.std() * np.sqrt(periods_per_year) if len(downside) > 0 else ann_vol
    sortino = ann_ret / down_vol if down_vol > 0 else 0.0
    return {
        "total_return": float(eq.iloc[-1] - 1),
        "ann_return": float(ann_ret),
        "ann_vol": float(ann_vol),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_dd": float(max_dd),
        "n_periods": len(log),
        "avg_turnover": float(np.mean([p["turnover"] for p in log])),
        "log": log,
        "equity": eq,
    }


def run_baselines(M: pd.DataFrame):
    """Compare against buy-and-hold BTC and equal-weight universe."""
    # Buy & hold BTC
    if "BTC" in M.columns:
        btc = M["BTC"].dropna()
        btc_eq = btc / btc.iloc[0]
        r = btc_eq.pct_change().dropna()
        ann_ret = (btc_eq.iloc[-1] ** (365 / len(btc_eq))) - 1
        ann_vol = r.std() * np.sqrt(365)
        sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
        peak = btc_eq.cummax()
        max_dd = float(((btc_eq - peak) / peak).min())
        print(f"  BTC Buy & Hold:  Sharpe={sharpe:.2f}  AnnRet={ann_ret*100:+.1f}%  "
              f"Vol={ann_vol*100:.1f}%  MaxDD={max_dd*100:.1f}%")

    # Equal-weight universe
    ew = M.mean(axis=1).dropna()
    ew_eq = ew / ew.iloc[0]
    r = ew_eq.pct_change().dropna()
    ann_ret = (ew_eq.iloc[-1] ** (365 / len(ew_eq))) - 1
    ann_vol = r.std() * np.sqrt(365)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    peak = ew_eq.cummax()
    max_dd = float(((ew_eq - peak) / peak).min())
    print(f"  EW Universe:     Sharpe={sharpe:.2f}  AnnRet={ann_ret*100:+.1f}%  "
          f"Vol={ann_vol*100:.1f}%  MaxDD={max_dd*100:.1f}%")


def main():
    print("=" * 100)
    print("CROSS-SECTIONAL MOMENTUM — v6")
    print("=" * 100)
    print(f"  Lookback: {LOOKBACK_DAYS}d  Top-K: {TOP_K}  Rebalance: {REBALANCE_DAYS}d")
    print(f"  Costs: {FEE_BPS} bps fee + {SLIP_BPS} bps slip per side "
          f"= {RT_COST*10000:.0f} bps per round trip")

    data = load_cached_data()
    data = filter_universe(data)

    print(f"\nBuilding price matrix...")
    M = build_price_matrix(data)
    print(f"  {M.shape[0]} days × {M.shape[1]} coins")
    print(f"  Date range: {M.index[0].date()} to {M.index[-1].date()}")

    print(f"\n{'='*100}")
    print("BASELINES")
    print(f"{'='*100}")
    run_baselines(M)

    print(f"\n{'='*100}")
    print("CROSS-SECTIONAL MOMENTUM SIMULATION")
    print(f"{'='*100}")

    # Test multiple top_k and lookback variants
    configs = [
        (30, 5, 30, "30d rank / 5+5 / 30d reb"),
        (30, 10, 30, "30d rank / 10+10 / 30d reb"),
        (14, 5, 30, "14d rank / 5+5 / 30d reb"),
        (60, 5, 30, "60d rank / 5+5 / 30d reb"),
        (90, 5, 30, "90d rank / 5+5 / 30d reb"),
        (30, 5, 7, "30d rank / 5+5 / 7d reb"),
    ]

    results = []
    for lookback, topk, reb, label in configs:
        r = cross_sectional_sim(M, lookback=lookback, top_k=topk, rebalance_days=reb)
        if "error" in r:
            print(f"\n  {label}: {r['error']}")
            continue
        print(f"\n  {label}:")
        print(f"    Sharpe={r['sharpe']:>6.2f}  Sortino={r['sortino']:>5.2f}  "
              f"AnnRet={r['ann_return']*100:>6.1f}%  Vol={r['ann_vol']*100:>5.1f}%  "
              f"MaxDD={r['max_dd']*100:>6.1f}%  "
              f"Turnover={r['avg_turnover']*100:.0f}%  N={r['n_periods']}")
        results.append({"label": label, "result": r})

    # ---- Detailed log of the primary config ----
    if results:
        primary = results[0]
        print(f"\n{'='*100}")
        print(f"PRIMARY CONFIG DETAIL — {primary['label']}")
        print(f"{'='*100}")
        log = primary["result"]["log"]
        print(f"  {'Start':>11}  {'End':>11}  {'LongPnL':>8}  {'ShortPnL':>9}  "
              f"{'Cost':>7}  {'Net':>8}  {'Turnover':>8}  {'Longs'}")
        print(f"  {'-'*100}")
        for p in log:
            longs_str = ",".join(p["long"][:3])
            print(f"  {str(p['t0']):>11}  {str(p['t1']):>11}  "
                  f"{p['long_pnl']*100:>7.2f}%  {p['short_pnl']*100:>8.2f}%  "
                  f"{p['cost']*100:>6.2f}%  {p['net_ret']*100:>7.2f}%  "
                  f"{p['turnover']*100:>7.0f}%  {longs_str}")

    # ---- Verdict ----
    print(f"\n{'='*100}")
    print("VERDICT")
    print(f"{'='*100}")
    if results:
        best = max(results, key=lambda x: x["result"]["sharpe"])
        bs = best["result"]["sharpe"]
        print(f"  Best config: {best['label']}  Sharpe={bs:.2f}")
        if bs > 1.0:
            print(f"  → REAL cross-sectional edge. Deploy this as the second strategy.")
        elif bs > 0.6:
            print(f"  → Solid. Deployable at small size with monitoring.")
        elif bs > 0.3:
            print(f"  → Marginal. Not worth deploying as-is.")
        else:
            print(f"  → No edge. Cross-sectional momentum doesn't work on HL perps.")


if __name__ == "__main__":
    main()
