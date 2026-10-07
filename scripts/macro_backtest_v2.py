"""
Macro-regime directional backtest v2.

Fixes v1's core flaw: FLP never dropped below 50 in the 2023-2026 data,
so the FAVORABLE gate never fired and the strategy barely traded.

v2 uses:
  A. FLP VELOCITY      30d change; positive = tightening, negative = loosening
  B. FLP QUANTILES     rolling 12-month terciles; entry in bottom/top tercile
  C. COMBINED          velocity sign AND quantile agreement

Also compares to v1 baselines (buy&hold, trend-only) for context.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
import numpy as np
import pandas as pd

from src.hyperliquid_bot.data.ohlcv import fetch_candles


FLP_MONTHLY = [
    ("2025-01", 66.01), ("2025-02", 64.74), ("2025-03", 62.29),
    ("2025-04", 58.48), ("2025-05", 56.83), ("2025-06", 58.26),
    ("2025-07", 63.66), ("2025-08", 62.83), ("2025-09", 64.85),
    ("2025-10", 68.99), ("2025-11", 66.60), ("2025-12", 66.55),
    ("2026-01", 66.79), ("2026-02", 63.39), ("2026-03", 64.34),
    ("2026-04", 55.15), ("2026-05", 61.37), ("2026-06", 64.95),
    ("2026-07", 63.89), ("2026-08", 61.53), ("2026-09", 60.92),
    ("2026-10", 58.13),
]


def build_flp_series() -> pd.Series:
    idx, vals = [], []
    for ym, v in FLP_MONTHLY:
        y, m = map(int, ym.split("-"))
        if m == 12:
            ts = pd.Timestamp(year=y + 1, month=1, day=1) - pd.Timedelta(days=1)
        else:
            ts = pd.Timestamp(year=y, month=m + 1, day=1) - pd.Timedelta(days=1)
        idx.append(ts); vals.append(v)
    return pd.Series(vals, index=idx, name="flp")


def fetch_daily(coin: str, days: int = 1200) -> pd.DataFrame:
    candles = fetch_candles(coin, interval="1d", lookback_hours=24 * days)
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame([{
        "t": c.t, "open": c.o, "high": c.h,
        "low": c.l, "close": c.c, "volume": c.v,
    } for c in candles])
    df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None)
    df = df.set_index("date").sort_index()
    return df


def compute_atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def metrics_from_equity(eq: pd.Series) -> dict:
    if len(eq) < 30:
        return {"error": "insufficient"}
    daily_ret = eq.pct_change().dropna()
    ann_ret = (eq.iloc[-1] ** (365 / len(eq))) - 1
    ann_vol = daily_ret.std() * np.sqrt(365)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    peak = eq.cummax()
    max_dd = float(((eq - peak) / peak).min())
    downside = daily_ret[daily_ret < 0]
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


def simulate_signal(df: pd.DataFrame, signal_long: pd.Series,
                    signal_short: pd.Series, fee_bps: float = 5.0,
                    label: str = "") -> dict:
    """
    signal_long/short: boolean Series indexed like df.
    Long when signal_long[i] and flat otherwise (unless short).
    Exits: MA cross against, stop at 2*ATR, or signal flip.
    """
    df = df.copy()
    df["ma200"] = df["close"].rolling(200).mean()
    df["atr"] = compute_atr(df, 14)

    position = 0
    entry_price = 0.0
    stop_price = 0.0
    entry_idx = 0
    equity = [1.0]
    trades = []

    for i in range(1, len(df)):
        close = df["close"].iloc[i]
        ma = df["ma200"].iloc[i]
        atr = df["atr"].iloc[i]
        want_long = bool(signal_long.iloc[i]) if not pd.isna(signal_long.iloc[i]) else False
        want_short = bool(signal_short.iloc[i]) if not pd.isna(signal_short.iloc[i]) else False

        # --- Exits ---
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
                ret = (close - entry_price) / entry_price * position
                fee = 2 * (fee_bps / 10000)
                trades.append({
                    "entry_date": df.index[entry_idx],
                    "exit_date": df.index[i],
                    "side": "long" if position == 1 else "short",
                    "return": ret - fee,
                    "reason": reason,
                })
                position = 0

        # --- Entries ---
        if position == 0 and not pd.isna(ma) and not pd.isna(atr):
            if want_long and close > ma:
                position = 1
                entry_price = close
                stop_price = close - 2 * atr
                entry_idx = i
            elif want_short and close < ma:
                position = -1
                entry_price = close
                stop_price = close + 2 * atr
                entry_idx = i

        # --- Equity mark ---
        if position == 0:
            equity.append(equity[-1])
        else:
            daily_ret = (close - df["close"].iloc[i - 1]) / df["close"].iloc[i - 1] * position
            equity.append(equity[-1] * (1 + daily_ret))

    eq = pd.Series(equity, index=df.index, name=f"eq_{label}")
    m = metrics_from_equity(eq)
    m["n_trades"] = len(trades)
    m["trades"] = trades
    m["equity"] = eq
    return m


def strategy_velocity(df: pd.DataFrame, flp_daily: pd.Series,
                      window: int = 60, thresh: float = 1.0) -> tuple[pd.Series, pd.Series]:
    """Long when FLP falling (loosening), short when FLP rising (tightening)."""
    vel = flp_daily.diff(window)
    long_sig = vel < -thresh
    short_sig = vel > thresh
    return long_sig.fillna(False), short_sig.fillna(False)


def strategy_quantile(df: pd.DataFrame, flp_daily: pd.Series,
                      window: int = 365, low_q: float = 0.33,
                      high_q: float = 0.67) -> tuple[pd.Series, pd.Series]:
    """Long in bottom tercile, short in top tercile of trailing-1y FLP."""
    low = flp_daily.rolling(window, min_periods=120).quantile(low_q)
    high = flp_daily.rolling(window, min_periods=120).quantile(high_q)
    long_sig = (flp_daily <= low).fillna(False)
    short_sig = (flp_daily >= high).fillna(False)
    return long_sig, short_sig


def strategy_combined(df: pd.DataFrame, flp_daily: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Both velocity AND quantile must agree."""
    v_long, v_short = strategy_velocity(df, flp_daily, window=60, thresh=1.0)
    q_long, q_short = strategy_quantile(df, flp_daily)
    return v_long & q_long, v_short & q_short


def trend_only_signals(df: pd.DataFrame, ma_window: int = 200) -> tuple[pd.Series, pd.Series]:
    ma = df["close"].rolling(ma_window).mean()
    long_sig = df["close"] > ma
    short_sig = df["close"] < ma
    return long_sig.fillna(False), short_sig.fillna(False)


def main():
    print("=" * 90)
    print("MACRO-REGIME DIRECTIONAL BACKTEST v2  (velocity + quantiles)")
    print("=" * 90)

    flp = build_flp_series()
    print(f"\nFLP range: {flp.min():.2f} to {flp.max():.2f}, n={len(flp)} monthly readings")

    for coin in ["BTC", "ETH", "SOL"]:
        print(f"\n{'='*90}")
        print(f"  {coin}")
        print(f"{'='*90}")

        df = fetch_daily(coin, days=1200)
        if df.empty:
            print(f"  no data")
            continue
        print(f"  price history: {len(df)} days ({df.index[0].date()} to {df.index[-1].date()})")

        # Forward-fill FLP to daily
        flp_daily = flp.reindex(df.index, method="ffill")

        # Collect strategies
        results = {}

        # Baseline: trend-only
        tl, ts = trend_only_signals(df)
        results["Trend Only"] = simulate_signal(df, tl, ts, label="trend")

        # Velocity (60d change)
        vl, vs = strategy_velocity(df, flp_daily, window=60, thresh=1.0)
        results["Velocity 60d"] = simulate_signal(df, vl, vs, label="vel60")

        # Velocity (90d change)
        vl2, vs2 = strategy_velocity(df, flp_daily, window=90, thresh=1.0)
        results["Velocity 90d"] = simulate_signal(df, vl2, vs2, label="vel90")

        # Quantile (rolling 1y)
        ql, qs = strategy_quantile(df, flp_daily)
        results["Quantile 1y"] = simulate_signal(df, ql, qs, label="quant")

        # Combined
        cl, cs = strategy_combined(df, flp_daily)
        results["Combined"] = simulate_signal(df, cl, cs, label="comb")

        # Buy & hold for reference
        eq_bh = df["close"] / df["close"].iloc[0]
        m_bh = metrics_from_equity(eq_bh)
        m_bh["n_trades"] = 1
        results["Buy & Hold"] = m_bh

        # Print table
        print(f"\n  {'Strategy':>14}  {'Total':>9}  {'Ann.Ret':>9}  "
              f"{'Ann.Vol':>9}  {'Sharpe':>7}  {'Sortino':>8}  {'MaxDD':>8}  {'Trades':>7}")
        print(f"  {'-'*88}")
        for name in ["Buy & Hold", "Trend Only", "Velocity 60d", "Velocity 90d",
                     "Quantile 1y", "Combined"]:
            r = results[name]
            if "error" in r:
                print(f"  {name:>14}  {r['error']}")
                continue
            print(f"  {name:>14}  "
                  f"{r['total_return']*100:>8.1f}%  "
                  f"{r['ann_return']*100:>8.1f}%  "
                  f"{r['ann_vol']*100:>8.1f}%  "
                  f"{r['sharpe']:>7.2f}  "
                  f"{r['sortino']:>8.2f}  "
                  f"{r['max_dd']*100:>7.1f}%  "
                  f"{r.get('n_trades', 0):>7}")

        # Diagnostic: signal firing counts
        print(f"\n  Signal firing counts:")
        for label, sig in [
            ("Velocity 60d", vl | vs),
            ("Velocity 90d", vl2 | vs2),
            ("Quantile 1y", ql | qs),
            ("Combined", cl | cs),
        ]:
            n = int(sig.sum())
            pct = 100 * sig.mean()
            print(f"    {label:>14}: {n:>5} days ({pct:.1f}%)")

    print(f"\n{'='*90}")
    print("Done.")


if __name__ == "__main__":
    main()
