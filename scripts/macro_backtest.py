"""
Macro-regime-gated directional backtest.

Strategies compared:
  A. Buy-and-hold    — always long
  B. Trend-only      — long if close > 200d MA, flat otherwise
  C. Regime-gated    — long when FAVORABLE & close > MA
                       short when DEFENSIVE & close < MA
                       flat in TRANSITIONAL
  D. Regime-gated + conf sizing — like C, but position size scales
                                   with FLP distance from threshold

FLP data: hardcoded monthly readings extracted from the Skylark sheet
screenshot. Update when we get historical backfill from the friend.

Price data: Hyperliquid daily candles via the existing fetch_candles helper.
"""
from __future__ import annotations
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
import numpy as np
import pandas as pd

from src.hyperliquid_bot.data.ohlcv import fetch_candles
from src.hyperliquid_bot.macro.regime import classify, position_multiplier, Regime


# ---------------------------------------------------------------------------
# FLP monthly history (extracted from Skylark sheet screenshot)
# Format: (YYYY-MM, FLP value)
# Extend when we get proper historical backfill.
# ---------------------------------------------------------------------------
FLP_MONTHLY = [
    ("2025-01", 66.01),
    ("2025-02", 64.74),
    ("2025-03", 62.29),
    ("2025-04", 58.48),
    ("2025-05", 56.83),
    ("2025-06", 58.26),
    ("2025-07", 63.66),
    ("2025-08", 62.83),
    ("2025-09", 64.85),
    ("2025-10", 68.99),
    ("2025-11", 66.60),
    ("2025-12", 66.55),
    ("2026-01", 66.79),
    ("2026-02", 63.39),
    ("2026-03", 64.34),
    ("2026-04", 55.15),
    ("2026-05", 61.37),
    ("2026-06", 64.95),
    ("2026-07", 63.89),
    ("2026-08", 61.53),
    ("2026-09", 60.92),
    ("2026-10", 58.13),
]


def build_flp_series() -> pd.Series:
    """Monthly FLP as a pandas Series indexed by month-end timestamp."""
    idx = []
    vals = []
    for ym, v in FLP_MONTHLY:
        # Last day of month
        y, m = map(int, ym.split("-"))
        if m == 12:
            ts = pd.Timestamp(year=y + 1, month=1, day=1) - pd.Timedelta(days=1)
        else:
            ts = pd.Timestamp(year=y, month=m + 1, day=1) - pd.Timedelta(days=1)
        idx.append(ts)
        vals.append(v)
    s = pd.Series(vals, index=idx, name="flp")
    return s


def fetch_daily(coin: str, days: int = 1200) -> pd.DataFrame:
    candles = fetch_candles(coin, interval="1d", lookback_hours=24 * days)
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame([{
        "t": c.t,
        "open": c.o,
        "high": c.h,
        "low": c.l,
        "close": c.c,
        "volume": c.v,
    } for c in candles])
    df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None)
    df = df.set_index("date").sort_index()
    return df


def compute_atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def simulate(df: pd.DataFrame, flp: pd.Series, mode: str,
             use_conf_sizing: bool = False,
             fee_bps: float = 5.0) -> dict:
    """
    mode: "long_only" | "buy_hold" | "regime"
    Returns dict with equity curve, trades, metrics.
    """
    df = df.copy()
    df["ma200"] = df["close"].rolling(200).mean()
    df["atr"] = compute_atr(df, 14)

    # Forward-fill monthly FLP to daily grid
    flp_daily = flp.reindex(df.index, method="ffill")

    position = 0         # -1, 0, +1
    entry_price = 0.0
    stop_price = 0.0
    entry_size = 0.0
    equity = [1.0]
    trades = []

    for i in range(1, len(df)):
        today = df.index[i]
        close = df["close"].iloc[i]
        ma = df["ma200"].iloc[i]
        atr = df["atr"].iloc[i]
        flp_val = flp_daily.iloc[i] if not pd.isna(flp_daily.iloc[i]) else np.nan

        # --- Exit checks first ---
        if position != 0:
            exit_now = False
            exit_reason = ""
            # Stop hit
            if position == 1 and df["low"].iloc[i] <= stop_price:
                exit_now = True
                exit_reason = "stop"
            elif position == -1 and df["high"].iloc[i] >= stop_price:
                exit_now = True
                exit_reason = "stop"
            # MA cross against
            if not exit_now and position == 1 and close < ma:
                exit_now = True
                exit_reason = "ma_cross"
            if not exit_now and position == -1 and close > ma:
                exit_now = True
                exit_reason = "ma_cross"
            # Regime exit
            if not exit_now and mode == "regime" and not np.isnan(flp_val):
                r = classify(flp_val)
                if position == 1 and r.regime != Regime.FAVORABLE:
                    exit_now = True
                    exit_reason = "regime_exit"
                if position == -1 and r.regime != Regime.DEFENSIVE:
                    exit_now = True
                    exit_reason = "regime_exit"

            if exit_now:
                # Realized pnl (fraction of stake, ignoring size for now — size applied at entry)
                ret = (close - entry_price) / entry_price * position
                gross_pnl = ret * entry_size
                fee = 2 * (fee_bps / 10000) * entry_size  # entry + exit
                net_pnl = gross_pnl - fee
                trades.append({
                    "entry_date": trades[-1]["entry_date"] if trades and "exit_date" in trades[-1] else None,
                    "exit_date": today,
                    "side": "long" if position == 1 else "short",
                    "entry": entry_price,
                    "exit": close,
                    "return": ret,
                    "size": entry_size,
                    "net_pnl": net_pnl,
                    "reason": exit_reason,
                })
                position = 0
                entry_size = 0.0

        # --- Entry checks ---
        if position == 0 and not np.isnan(ma) and not np.isnan(atr):
            if mode == "buy_hold":
                # Always long from day 1 (handled outside loop, but for completeness)
                pass
            elif mode == "long_only":
                if close > ma:
                    position = 1
                    entry_price = close
                    stop_price = close - 2 * atr
                    entry_size = 1.0
            elif mode == "regime":
                if np.isnan(flp_val):
                    pass
                else:
                    r = classify(flp_val)
                    if r.regime == Regime.FAVORABLE and close > ma:
                        position = 1
                        entry_price = close
                        stop_price = close - 2 * atr
                        entry_size = position_multiplier(r) if use_conf_sizing else 1.0
                    elif r.regime == Regime.DEFENSIVE and close < ma:
                        position = -1
                        entry_price = close
                        stop_price = close + 2 * atr
                        entry_size = position_multiplier(r) if use_conf_sizing else 1.0

        # --- Mark-to-market equity update ---
        prev_eq = equity[-1]
        if position == 0:
            equity.append(prev_eq)
        else:
            daily_ret = (close - df["close"].iloc[i - 1]) / df["close"].iloc[i - 1] * position
            equity.append(prev_eq * (1 + daily_ret * entry_size))

    equity = np.array(equity)
    eq_series = pd.Series(equity, index=df.index, name="equity")

    # Metrics
    daily_ret = eq_series.pct_change().dropna()
    if len(daily_ret) < 30:
        return {"error": "insufficient data"}

    ann_ret = (eq_series.iloc[-1] ** (365 / len(eq_series))) - 1
    ann_vol = daily_ret.std() * np.sqrt(365)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0

    # Max drawdown
    peak = eq_series.cummax()
    dd = (eq_series - peak) / peak
    max_dd = float(dd.min())

    # Sortino
    downside = daily_ret[daily_ret < 0]
    down_vol = downside.std() * np.sqrt(365) if len(downside) > 0 else ann_vol
    sortino = ann_ret / down_vol if down_vol > 0 else 0

    total_return = float(eq_series.iloc[-1] - 1)

    return {
        "equity": eq_series,
        "total_return": total_return,
        "ann_return": ann_ret,
        "ann_vol": ann_vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_dd": max_dd,
        "n_trades": len(trades),
        "trades": trades,
    }


def main():
    print("=" * 80)
    print("MACRO-REGIME DIRECTIONAL BACKTEST")
    print("=" * 80)

    flp = build_flp_series()
    print(f"\nFLP history: {len(flp)} monthly readings "
          f"({flp.index[0].date()} to {flp.index[-1].date()})")
    print(f"Current FLP: {flp.iloc[-1]:.2f}")

    coins = ["BTC", "ETH", "SOL"]
    for coin in coins:
        print(f"\n{'='*80}")
        print(f"  {coin}")
        print(f"{'='*80}")

        df = fetch_daily(coin, days=1200)
        if df.empty:
            print(f"  no data for {coin}")
            continue
        print(f"  price history: {len(df)} days "
              f"({df.index[0].date()} to {df.index[-1].date()})")

        results = {}
        for mode, label in [
            ("buy_hold", "Buy & Hold"),
            ("long_only", "Trend Only"),
            ("regime", "Regime-Gated"),
        ]:
            if mode == "buy_hold":
                # Simple buy-and-hold: equity = price / price[0]
                eq = df["close"] / df["close"].iloc[0]
                daily_ret = eq.pct_change().dropna()
                ann_ret = (eq.iloc[-1] ** (365 / len(eq))) - 1
                ann_vol = daily_ret.std() * np.sqrt(365)
                sharpe = ann_ret / ann_vol if ann_vol > 0 else 0
                peak = eq.cummax()
                max_dd = float(((eq - peak) / peak).min())
                downside = daily_ret[daily_ret < 0]
                down_vol = downside.std() * np.sqrt(365) if len(downside) > 0 else ann_vol
                sortino = ann_ret / down_vol if down_vol > 0 else 0
                results[mode] = {
                    "total_return": float(eq.iloc[-1] - 1),
                    "ann_return": ann_ret,
                    "ann_vol": ann_vol,
                    "sharpe": sharpe,
                    "sortino": sortino,
                    "max_dd": max_dd,
                    "n_trades": 1,
                }
            else:
                results[mode] = simulate(df, flp, mode)

        print(f"\n  {'Strategy':>18}  {'Total':>9}  {'Ann.Ret':>9}  "
              f"{'Ann.Vol':>9}  {'Sharpe':>7}  {'Sortino':>8}  {'MaxDD':>8}  {'Trades':>7}")
        print(f"  {'-'*90}")
        for mode in ["buy_hold", "long_only", "regime"]:
            r = results[mode]
            if "error" in r:
                print(f"  {mode:>18}  {r['error']}")
                continue
            print(f"  {mode:>18}  "
                  f"{r['total_return']*100:>8.1f}%  "
                  f"{r['ann_return']*100:>8.1f}%  "
                  f"{r['ann_vol']*100:>8.1f}%  "
                  f"{r['sharpe']:>7.2f}  "
                  f"{r['sortino']:>8.2f}  "
                  f"{r['max_dd']*100:>7.1f}%  "
                  f"{r.get('n_trades', 0):>7}")

    print(f"\n{'='*80}")
    print("Done.")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
