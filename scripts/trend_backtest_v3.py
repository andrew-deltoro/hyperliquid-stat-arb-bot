"""
Trend-following v3 — fixed cash accounting.

Fixes vs v2:
  1. Fees and slippage deducted from cash on every entry and exit
  2. Fixed-fraction position sizing (default 1.0x notional against INITIAL
     capital, not compounded), with optional compounding mode
  3. Realistic costs: 3 bps fee + 3 bps slippage per side (round trip = 12 bps)
  4. Chained equity computed in log-space to avoid overflow
  5. Reports Sharpe/return for both non-compounded and compounded variants

Strategy: Dual-MA (50/100/200 average) with 2*ATR stops.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
import numpy as np
import pandas as pd

from src.hyperliquid_bot.data.ohlcv import fetch_candles


COINS = ["BTC", "ETH", "SOL", "AVAX", "BNB"]

# Costs (bps per side)
FEE_BPS = 3.0
SLIP_BPS = 3.0
ROUND_TRIP_COST = (FEE_BPS + SLIP_BPS) * 2 / 10000  # 0.0012 = 12 bps

SUB_PERIODS = [
    ("post-FTX bear",  "2022-08-29", "2023-01-01"),
    ("recovery rally", "2023-01-01", "2024-01-01"),
    ("bull run",       "2024-01-01", "2025-01-01"),
    ("chop/consolid",  "2025-01-01", "2026-10-07"),
]


def fetch_daily(coin: str, days: int = 1500) -> pd.DataFrame:
    candles = fetch_candles(coin, interval="1d", lookback_hours=24 * days)
    if not candles:
        return pd.DataFrame()
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
    ann_factor = 365 / len(eq)
    end_val = eq.iloc[-1]
    if end_val <= 0:
        return {"error": "wiped out"}
    ann_ret = (end_val ** ann_factor) - 1
    ann_vol = r.std() * np.sqrt(365)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    peak = eq.cummax()
    max_dd = float(((eq - peak) / peak).min())
    downside = r[r < 0]
    down_vol = downside.std() * np.sqrt(365) if len(downside) > 0 else ann_vol
    sortino = ann_ret / down_vol if down_vol > 0 else 0.0
    return {
        "total_return": float(end_val - 1),
        "ann_return": float(ann_ret),
        "ann_vol": float(ann_vol),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_dd": float(max_dd),
        "n_days": len(eq),
    }


def simulate(df: pd.DataFrame, long_sig: pd.Series, short_sig: pd.Series,
             initial_capital: float = 1.0,
             position_frac: float = 1.0,
             compounding: bool = False,
             fee_bps: float = FEE_BPS,
             slip_bps: float = SLIP_BPS) -> dict:
    """
    Cash-accounted simulation.

    initial_capital: starting balance (default 1.0 for normalized returns)
    position_frac:   fraction of capital deployed per trade (1.0 = all-in)
    compounding:     if True, position size scales with current equity;
                     if False, uses position_frac of initial_capital always
    """
    df = df.copy()
    df["atr"] = atr(df, 14)
    rt_cost = (fee_bps + slip_bps) * 2 / 10000  # round trip in fraction

    position = 0          # -1, 0, +1
    entry_price = 0.0
    stop_price = 0.0
    size_usd = 0.0        # dollar notional of position
    entry_idx = 0
    cash = initial_capital
    equity = [initial_capital]
    trades = []

    for i in range(1, len(df)):
        close = df["close"].iloc[i]
        a = df["atr"].iloc[i]
        want_long = bool(long_sig.iloc[i]) if not pd.isna(long_sig.iloc[i]) else False
        want_short = bool(short_sig.iloc[i]) if not pd.isna(short_sig.iloc[i]) else False

        # Compute mark-to-market equity BEFORE deciding to exit/enter
        if position != 0:
            # Current position value
            unrealized = size_usd * ((close - entry_price) / entry_price) * position
            current_equity = cash + size_usd + unrealized  # cash + position cost + unrealized
        else:
            current_equity = cash

        # ---- EXIT ----
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
                # Deduct round-trip cost
                cost = size_usd * rt_cost
                cash = cash + size_usd + realized - cost
                trades.append({
                    "entry_date": df.index[entry_idx],
                    "exit_date": df.index[i],
                    "side": "long" if position == 1 else "short",
                    "gross_pct": realized / size_usd,
                    "net_usd": realized - cost,
                    "reason": reason,
                })
                position = 0
                size_usd = 0.0

        # ---- ENTRY ----
        if position == 0 and not pd.isna(a):
            base_size = (cash if compounding else initial_capital) * position_frac
            if base_size <= 0:
                equity.append(current_equity)
                continue

            if want_long and base_size > 0:
                position = 1
                entry_price = close
                stop_price = close - 2 * a
                size_usd = base_size
                entry_idx = i
                cash -= size_usd   # deploy capital
            elif want_short and base_size > 0:
                position = -1
                entry_price = close
                stop_price = close + 2 * a
                size_usd = base_size
                entry_idx = i
                cash -= size_usd

        # ---- MARK EQUITY ----
        if position == 0:
            equity.append(cash)
        else:
            unrealized = size_usd * ((close - entry_price) / entry_price) * position
            equity.append(cash + size_usd + unrealized)

    eq = pd.Series(equity, index=df.index)
    m = metrics(eq)
    m["n_trades"] = len(trades)
    m["trades"] = trades
    m["equity"] = eq
    m["final_cash"] = float(cash)
    return m


def run_full(coin: str, df: pd.DataFrame) -> dict:
    """Run Dual-MA on full sample, both compounding modes."""
    L, S = dual_ma_signal(df)
    fixed = simulate(df, L, S, compounding=False)
    comp = simulate(df, L, S, compounding=True)
    return {"fixed": fixed, "compounding": comp}


def run_subperiod(df: pd.DataFrame, start: str, end: str) -> dict | None:
    mask = (df.index >= start) & (df.index < end)
    dsub = df[mask]
    if len(dsub) < 60:
        return None
    L, S = dual_ma_signal(dsub)
    fixed = simulate(dsub, L, S, compounding=False)
    if "error" in fixed:
        return None
    return fixed


def main():
    print("=" * 110)
    print("TREND-FOLLOWING v3 — CASH-ACCOUNTED, FEES APPLIED, NON-COMPOUNDING")
    print("=" * 110)
    print(f"Costs assumed: {FEE_BPS} bps fee + {SLIP_BPS} bps slippage per side "
          f"= {ROUND_TRIP_COST*10000:.0f} bps per round trip")

    # Load data
    data = {}
    for coin in COINS:
        df = fetch_daily(coin, days=1500)
        if not df.empty:
            data[coin] = df
    print(f"Loaded {len(data)} coins × {len(next(iter(data.values())))} days")

    # =========================================================
    # FULL SAMPLE
    # =========================================================
    print(f"\n{'='*110}")
    print("FULL SAMPLE: fixed-fraction sizing (1x initial capital per trade, no compounding)")
    print(f"{'='*110}")

    print(f"\n  {'Coin':>6}  {'Total':>12}  {'Ann.Ret':>9}  {'Ann.Vol':>9}  "
          f"{'Sharpe':>7}  {'Sortino':>8}  {'MaxDD':>8}  {'N':>4}  {'Final$':>8}")
    print(f"  {'-'*100}")

    fixed_summary = {}
    for coin, df in data.items():
        r = run_full(coin, df)["fixed"]
        fixed_summary[coin] = r
        if "error" in r:
            print(f"  {coin:>6}  {r['error']}")
            continue
        print(f"  {coin:>6}  "
              f"{r['total_return']*100:>11.1f}%  "
              f"{r['ann_return']*100:>8.1f}%  "
              f"{r['ann_vol']*100:>8.1f}%  "
              f"{r['sharpe']:>7.2f}  "
              f"{r['sortino']:>8.2f}  "
              f"{r['max_dd']*100:>7.1f}%  "
              f"{r['n_trades']:>4}  "
              f"{r['final_cash']:>8.3f}")

    # =========================================================
    # COMPOUNDING MODE (for comparison)
    # =========================================================
    print(f"\n{'='*110}")
    print("FULL SAMPLE: compounding sizing (position scales with equity)")
    print(f"{'='*110}")

    print(f"\n  {'Coin':>6}  {'Total':>12}  {'Ann.Ret':>9}  {'Ann.Vol':>9}  "
          f"{'Sharpe':>7}  {'MaxDD':>8}  {'N':>4}")
    print(f"  {'-'*80}")

    for coin, df in data.items():
        r = run_full(coin, df)["compounding"]
        if "error" in r:
            print(f"  {coin:>6}  {r['error']}")
            continue
        print(f"  {coin:>6}  "
              f"{r['total_return']*100:>11.1f}%  "
              f"{r['ann_return']*100:>8.1f}%  "
              f"{r['ann_vol']*100:>8.1f}%  "
              f"{r['sharpe']:>7.2f}  "
              f"{r['max_dd']*100:>7.1f}%  "
              f"{r['n_trades']:>4}")

    # =========================================================
    # SUB-PERIOD (fixed-fraction only, honest accounting)
    # =========================================================
    print(f"\n{'='*110}")
    print("SUB-PERIOD ANALYSIS (fixed-fraction, fees applied)")
    print(f"{'='*110}")

    for coin, df in data.items():
        print(f"\n  === {coin} ===")
        print(f"  {'Period':>18}  {'Days':>5}  {'Ann.Ret':>9}  {'Sharpe':>7}  "
              f"{'Sortino':>8}  {'MaxDD':>8}  {'N':>4}")
        print(f"  {'-'*80}")
        for pname, start, end in SUB_PERIODS:
            r = run_subperiod(df, start, end)
            if r is None:
                print(f"  {pname:>18}  insufficient")
                continue
            print(f"  {pname:>18}  {r['n_days']:>5}  "
                  f"{r['ann_return']*100:>8.1f}%  "
                  f"{r['sharpe']:>7.2f}  "
                  f"{r['sortino']:>8.2f}  "
                  f"{r['max_dd']*100:>7.1f}%  "
                  f"{r['n_trades']:>4}")

    # =========================================================
    # WALK-FORWARD (log-space chaining, no overflow)
    # =========================================================
    print(f"\n{'='*110}")
    print("WALK-FORWARD (3-fold, log-space chaining, Dual-MA fixed)")
    print(f"{'='*110}")

    for coin, df in data.items():
        n = len(df)
        folds = [(0, n // 3), (n // 3, 2 * n // 3), (2 * n // 3, n)]
        print(f"\n  === {coin} === ({n} days)")

        log_returns = []
        for fi, (a, b) in enumerate(folds):
            dsub = df.iloc[a:b]
            if len(dsub) < 60:
                continue
            L, S = dual_ma_signal(dsub)
            r = simulate(dsub, L, S, compounding=False)
            if "error" in r:
                print(f"    fold {fi}: {r['error']}")
                continue
            # Log return of this fold
            log_ret = np.log(r["equity"].iloc[-1] / r["equity"].iloc[0])
            log_returns.append(log_ret)
            print(f"    fold {fi} ({dsub.index[0].date()} to {dsub.index[-1].date()}): "
                  f"ann={r['ann_return']*100:>7.1f}%  Sharpe={r['sharpe']:>6.2f}  "
                  f"MaxDD={r['max_dd']*100:>6.1f}%  n={r['n_trades']}")

        # Chained return via log-space
        total_log = sum(log_returns)
        total_ret = np.exp(total_log) - 1
        n_days_total = n  # approximate
        ann_ret = (1 + total_ret) ** (365 / n_days_total) - 1
        print(f"    CHAINED OOS: total={total_ret*100:+.1f}%  "
              f"ann={ann_ret*100:+.1f}%")

    # =========================================================
    # VERDICT
    # =========================================================
    print(f"\n{'='*110}")
    print("VERDICT")
    print(f"{'='*110}")

    sharpes = [r["sharpe"] for r in fixed_summary.values() if "error" not in r]
    if sharpes:
        median_sharpe = float(np.median(sharpes))
        print(f"  Median full-sample Sharpe (fixed-fraction, after fees): {median_sharpe:.2f}")
        if median_sharpe > 1.5:
            print("  → Strong. Dual-MA is a real strategy at this cost level.")
        elif median_sharpe > 1.0:
            print("  → Solid. Tradeable but not spectacular.")
        elif median_sharpe > 0.5:
            print("  → Marginal. Only trade with low capital and clear edge.")
        else:
            print("  → Weak. Not worth deploying.")

    print(f"\n  Recommendation: deploy as SEPARATE bot with fixed-fraction sizing")
    print(f"  and 1x leverage. Start with $500. Add coins once live Sharpe is measured.")


if __name__ == "__main__":
    main()
