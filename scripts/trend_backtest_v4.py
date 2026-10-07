"""
Trend-following v4 — distribution study across the HL universe.

Question: is AVAX's 0.88 Sharpe a real signal or a lottery ticket?
Answer: run Dual-MA on 25+ coins, look at the Sharpe distribution.

Key outputs:
  1. Per-coin Sharpe sorted (spot the outliers)
  2. Distribution stats: median, quartiles, mean
  3. Portfolio Sharpe (equal-weight all coins with Sharpe > threshold)
  4. Random-coin baseline (median across all coins)
  5. Concentration risk: what happens if we drop the top performer?

Costs: 3 bps fee + 3 bps slippage per side = 12 bps round trip
Sizing: fixed-fraction, 1x initial capital per trade, no compounding
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
import numpy as np
import pandas as pd

from src.hyperliquid_bot.data.ohlcv import fetch_candles


COINS = [
    # Majors (definitely have data)
    "BTC", "ETH", "SOL", "BNB", "AVAX",
    # Large alts
    "XRP", "ADA", "DOGE", "LINK", "MATIC", "DOT", "TRX", "LTC",
    # Mid alts
    "ATOM", "NEAR", "APT", "ARB", "OP", "INJ", "SUI", "SEI", "TIA",
    # DeFi / sector
    "AAVE", "UNI", "MKR", "SNX", "CRV", "LDO",
    # Memes / hot
    "WIF", "PEPE", "SHIB",
]

FEE_BPS = 3.0
SLIP_BPS = 3.0
PORTFOLIO_THRESHOLD = 0.5  # only include coins with full-sample Sharpe > this


def fetch_daily(coin: str, days: int = 1500) -> pd.DataFrame | None:
    try:
        candles = fetch_candles(coin, interval="1d", lookback_hours=24 * days)
    except Exception:
        return None
    if not candles or len(candles) < 400:
        return None
    df = pd.DataFrame([{
        "t": c.t, "open": c.o, "high": c.h,
        "low": c.l, "close": c.c, "volume": c.v,
    } for c in candles])
    df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None)
    return df.set_index("date").sort_index()


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def dual_ma_signal(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    score = pd.Series(0.0, index=df.index)
    for w in (50, 100, 200):
        ma = df["close"].rolling(w).mean()
        score += np.sign(df["close"] - ma).fillna(0.0)
    return (score > 0).fillna(False), (score < 0).fillna(False)


def metrics(eq: pd.Series) -> dict:
    if len(eq) < 5:
        return {"error": "short"}
    r = eq.pct_change().dropna()
    if len(r) < 3 or r.std() == 0:
        return {"error": "no returns"}
    ann_ret = (eq.iloc[-1] ** (365 / len(eq))) - 1 if eq.iloc[-1] > 0 else -1.0
    ann_vol = r.std() * np.sqrt(365)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    peak = eq.cummax()
    max_dd = float(((eq - peak) / peak).min())
    downside = r[r < 0]
    down_vol = downside.std() * np.sqrt(365) if len(downside) > 0 else ann_vol
    sortino = ann_ret / down_vol if down_vol > 0 else 0.0
    return {
        "total_return": float(eq.iloc[-1] - 1),
        "ann_return": float(ann_ret),
        "ann_vol": float(ann_vol),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_dd": float(max_dd),
    }


def simulate(df: pd.DataFrame, long_sig: pd.Series, short_sig: pd.Series,
             fee_bps: float = FEE_BPS, slip_bps: float = SLIP_BPS) -> dict:
    df = df.copy()
    df["atr"] = atr(df, 14)
    rt_cost = (fee_bps + slip_bps) * 2 / 10000

    position = 0
    entry_price = 0.0
    stop_price = 0.0
    size_usd = 0.0
    entry_idx = 0
    initial = 1.0
    cash = initial
    equity = [initial]
    trades = []

    for i in range(1, len(df)):
        close = df["close"].iloc[i]
        a = df["atr"].iloc[i]
        want_long = bool(long_sig.iloc[i]) if not pd.isna(long_sig.iloc[i]) else False
        want_short = bool(short_sig.iloc[i]) if not pd.isna(short_sig.iloc[i]) else False

        if position != 0:
            exit_now, reason = False, ""
            if position == 1 and df["low"].iloc[i] <= stop_price:
                exit_now, reason = True, "stop"
            elif position == -1 and df["high"].iloc[i] >= stop_price:
                exit_now, reason = True, "stop"
            if not exit_now and position == 1 and not want_long:
                exit_now, reason = True, "signal_flip"
            if not exit_now and position == -1 and not want_short:
                exit_now, reason = True, "signal_flip"
            if exit_now:
                realized = size_usd * ((close - entry_price) / entry_price) * position
                cost = size_usd * rt_cost
                cash = cash + size_usd + realized - cost
                trades.append({"return": realized / size_usd - rt_cost, "reason": reason})
                position = 0
                size_usd = 0.0

        if position == 0 and not pd.isna(a) and cash > 0.01:
            base_size = min(cash, initial)
            if want_long and base_size > 0:
                position = 1
                entry_price = close
                stop_price = close - 2 * a
                size_usd = base_size
                entry_idx = i
                cash -= size_usd
            elif want_short and base_size > 0:
                position = -1
                entry_price = close
                stop_price = close + 2 * a
                size_usd = base_size
                entry_idx = i
                cash -= size_usd

        if position == 0:
            equity.append(cash)
        else:
            unrealized = size_usd * ((close - entry_price) / entry_price) * position
            equity.append(cash + size_usd + unrealized)

    eq = pd.Series(equity, index=df.index)
    m = metrics(eq)
    m["n_trades"] = len(trades)
    m["equity"] = eq
    return m


def main():
    print("=" * 100)
    print("TREND-FOLLOWING v4 — DISTRIBUTION ACROSS HL UNIVERSE")
    print("=" * 100)
    print(f"Coins to test: {len(COINS)}")
    print(f"Costs: {FEE_BPS} bps fee + {SLIP_BPS} bps slip per side = "
          f"{(FEE_BPS + SLIP_BPS) * 2:.0f} bps per round trip")

    # Load all coins
    print(f"\nFetching price data...")
    data: dict[str, pd.DataFrame] = {}
    failures = []
    for coin in COINS:
        df = fetch_daily(coin, days=1500)
        if df is not None:
            data[coin] = df
            print(f"  {coin:>6}: {len(df):>5} days  "
                  f"({df.index[0].date()} to {df.index[-1].date()})")
        else:
            failures.append(coin)
    if failures:
        print(f"\n  Failed/skipped: {failures}")
    print(f"\nSuccessfully loaded: {len(data)} coins\n")

    if len(data) < 10:
        print("Not enough coins with data. Aborting.")
        return

    # Run Dual-MA on each
    print("=" * 100)
    print("RUNNING DUAL-MA ACROSS UNIVERSE")
    print("=" * 100)

    results = []
    for coin, df in data.items():
        L, S = dual_ma_signal(df)
        r = simulate(df, L, S)
        if "error" in r:
            print(f"  {coin:>6}: {r['error']}")
            continue
        results.append({
            "coin": coin,
            "sharpe": r["sharpe"],
            "sortino": r["sortino"],
            "ann_ret": r["ann_ret"],
            "ann_vol": r["ann_vol"],
            "max_dd": r["max_dd"],
            "total": r["total_return"],
            "n_trades": r["n_trades"],
            "equity": r["equity"],
        })

    if not results:
        print("No coins produced valid results.")
        return

    results.sort(key=lambda x: -x["sharpe"])

    # =============================================================
    # Per-coin table
    # =============================================================
    print(f"\n{'Rank':>4}  {'Coin':>6}  {'Sharpe':>7}  {'Sortino':>8}  "
          f"{'Ann.Ret':>9}  {'Ann.Vol':>9}  {'MaxDD':>8}  {'Total':>10}  {'N':>4}")
    print(f"{'-'*94}")
    for rank, r in enumerate(results, 1):
        print(f"{rank:>4}  {r['coin']:>6}  "
              f"{r['sharpe']:>7.2f}  "
              f"{r['sortino']:>8.2f}  "
              f"{r['ann_ret']*100:>8.1f}%  "
              f"{r['ann_vol']*100:>8.1f}%  "
              f"{r['max_dd']*100:>7.1f}%  "
              f"{r['total']*100:>9.1f}%  "
              f"{r['n_trades']:>4}")

    # =============================================================
    # Distribution stats
    # =============================================================
    sharpes = np.array([r["sharpe"] for r in results])
    sortinos = np.array([r["sortino"] for r in results])
    dds = np.array([r["max_dd"] for r in results])

    print(f"\n{'='*100}")
    print("SHARPE DISTRIBUTION")
    print(f"{'='*100}")
    print(f"  Count:    {len(sharpes)}")
    print(f"  Mean:     {sharpes.mean():>7.3f}")
    print(f"  Median:   {np.median(sharpes):>7.3f}")
    print(f"  Std:      {sharpes.std():>7.3f}")
    print(f"  Min:      {sharpes.min():>7.3f}  ({results[-1]['coin']})")
    print(f"  Max:      {sharpes.max():>7.3f}  ({results[0]['coin']})")
    print(f"  25th pct: {np.percentile(sharpes, 25):>7.3f}")
    print(f"  75th pct: {np.percentile(sharpes, 75):>7.3f}")
    print(f"  Positive: {(sharpes > 0).sum()}/{len(sharpes)} coins")
    print(f"  > 0.5:    {(sharpes > 0.5).sum()}/{len(sharpes)} coins")
    print(f"  > 1.0:    {(sharpes > 1.0).sum()}/{len(sharpes)} coins")

    # =============================================================
    # Concentration risk
    # =============================================================
    print(f"\n{'='*100}")
    print("CONCENTRATION RISK")
    print(f"{'='*100}")
    top_1 = sharpes.max()
    top_5_mean = sharpes[:5].mean() if len(sharpes) >= 5 else sharpes.mean()
    top_10_mean = sharpes[:10].mean() if len(sharpes) >= 10 else sharpes.mean()
    rest_mean = sharpes[10:].mean() if len(sharpes) > 10 else float("nan")
    print(f"  Top 1 coin Sharpe:       {top_1:.3f}")
    print(f"  Top 5 mean Sharpe:       {top_5_mean:.3f}")
    print(f"  Top 10 mean Sharpe:      {top_10_mean:.3f}")
    print(f"  Rest mean Sharpe:        {rest_mean:.3f}")
    print(f"  Median Sharpe:           {np.median(sharpes):.3f}")
    print(f"  Drop top performer:      {sharpes[1:].mean():.3f}")

    # =============================================================
    # Equal-weight portfolio
    # =============================================================
    print(f"\n{'='*100}")
    print("EQUAL-WEIGHT PORTFOLIOS (dollar-cost into N coins at start)")
    print(f"{'='*100}")

    # Align all equity curves on common dates
    all_eq = pd.DataFrame({r["coin"]: r["equity"] for r in results})
    all_eq = all_eq.ffill().dropna(how="all")
    # Normalize each to 1.0 at first shared date
    for c in all_eq.columns:
        first_valid = all_eq[c].first_valid_index()
        all_eq[c] = all_eq[c] / all_eq.loc[first_valid, c]
    all_eq = all_eq.dropna()

    for label, filt in [
        ("All coins", lambda r: True),
        (f"Sharpe > {PORTFOLIO_THRESHOLD}", lambda r: r["sharpe"] > PORTFOLIO_THRESHOLD),
        ("Sharpe > 0.7", lambda r: r["sharpe"] > 0.7),
        ("Top 5 by Sharpe", lambda r: True),  # handled specially below
    ]:
        if label == "Top 5 by Sharpe":
            selected = [r["coin"] for r in results[:5]]
        else:
            selected = [r["coin"] for r in results if filt(r)]
        if not selected:
            print(f"  {label:>20}: no coins pass")
            continue
        port = all_eq[selected].mean(axis=1)
        m = metrics(port)
        if "error" in m:
            print(f"  {label:>20}: {m['error']}")
            continue
        print(f"  {label:>20} ({len(selected):>2} coins):  "
              f"Sharpe={m['sharpe']:>6.2f}  "
              f"Ann={m['ann_return']*100:>6.1f}%  "
              f"Vol={m['ann_vol']*100:>5.1f}%  "
              f"MaxDD={m['max_dd']*100:>6.1f}%  "
              f"Sortino={m['sortino']:>5.2f}")

    # =============================================================
    # Verdict
    # =============================================================
    print(f"\n{'='*100}")
    print("VERDICT")
    print(f"{'='*100}")

    median_s = float(np.median(sharpes))
    if median_s > 0.8:
        print(f"  Median Sharpe {median_s:.2f} — the edge is real and broadly distributed.")
        print(f"  → Tradeable as a diversified portfolio.")
    elif median_s > 0.5:
        print(f"  Median Sharpe {median_s:.2f} — the edge is real but modest.")
        print(f"  → Only trade the top-quartile coins. Diversification may add value.")
    elif median_s > 0.3:
        print(f"  Median Sharpe {median_s:.2f} — edge is marginal.")
        print(f"  → Coin selection is critical. Equal-weight won't work.")
    else:
        print(f"  Median Sharpe {median_s:.2f} — no edge found at this cost level.")
        print(f"  → Reject the strategy.")

    print()
    print(f"  Best single coin: {results[0]['coin']}  Sharpe {results[0]['sharpe']:.2f}")
    print(f"  Worst single coin: {results[-1]['coin']}  Sharpe {results[-1]['sharpe']:.2f}")

    # =============================================================
    # Save
    # =============================================================
    out = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports")
    out.mkdir(exist_ok=True)
    df_save = pd.DataFrame([{
        "coin": r["coin"],
        "sharpe": r["sharpe"],
        "sortino": r["sortino"],
        "ann_ret": r["ann_ret"],
        "ann_vol": r["ann_vol"],
        "max_dd": r["max_dd"],
        "total": r["total_return"],
        "n_trades": r["n_trades"],
    } for r in results])
    csv_path = out / "trend_v4_results.csv"
    df_save.to_csv(csv_path, index=False)
    print(f"\n  Saved: {csv_path}")


if __name__ == "__main__":
    main()
