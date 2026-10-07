"""
Trend-following backtest with walk-forward validation and spinoff experiments.

Baselines and spinoffs tested:
  A. Trend long-short        — long above MA, short below (baseline)
  B. Trend long-only         — long above MA, flat otherwise
  C. Trend short-only        — short below MA, flat otherwise
  D. Dual-MA ensemble        — average of 50/100/200d MA signals
  E. Trend + vol-targeting   — position size = target_vol / realized_vol
  F. Trend + FLP sizing      — full size FLP<60, half 60-65, flat >65
  G. Quantile-1y FLP only    — long bottom tercile, short top tercile
  H. Trend + FLP gate        — trend signal AND FLP in directional zone

Walk-forward validation:
  - Split price history into 3 equal folds
  - Grid-search MA window on fold 1 (IS)
  - Apply best window to folds 2-3 (OOS)
  - Report IS vs OOS metrics to detect overfitting

All results are net of 5 bps round-trip fees.
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

COINS = ["BTC", "ETH", "SOL", "AVAX", "BNB"]
FEE_BPS = 5.0
TRADING_DAYS = 365


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def build_flp_daily(index: pd.DatetimeIndex) -> pd.Series:
    idx, vals = [], []
    for ym, v in FLP_MONTHLY:
        y, m = map(int, ym.split("-"))
        if m == 12:
            ts = pd.Timestamp(year=y + 1, month=1, day=1) - pd.Timedelta(days=1)
        else:
            ts = pd.Timestamp(year=y, month=m + 1, day=1) - pd.Timedelta(days=1)
        idx.append(ts); vals.append(v)
    s = pd.Series(vals, index=idx, name="flp")
    return s.reindex(index, method="ffill")


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


# ---------------------------------------------------------------------------
# Signal generators
# ---------------------------------------------------------------------------
def sig_trend(df: pd.DataFrame, ma_window: int = 200) -> tuple[pd.Series, pd.Series]:
    ma = df["close"].rolling(ma_window).mean()
    long = (df["close"] > ma).fillna(False)
    short = (df["close"] < ma).fillna(False)
    return long, short


def sig_dual_ma(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Average of three MA windows."""
    score = pd.Series(0.0, index=df.index)
    for w in (50, 100, 200):
        ma = df["close"].rolling(w).mean()
        score += np.sign(df["close"] - ma).fillna(0.0)
    long = (score > 0).fillna(False)
    short = (score < 0).fillna(False)
    return long, short


def sig_quantile_flp(flp: pd.Series, window: int = 365,
                      lo: float = 0.33, hi: float = 0.67) -> tuple[pd.Series, pd.Series]:
    low = flp.rolling(window, min_periods=120).quantile(lo)
    high = flp.rolling(window, min_periods=120).quantile(hi)
    return (flp <= low).fillna(False), (flp >= high).fillna(False)


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------
def simulate(df: pd.DataFrame, long_sig: pd.Series, short_sig: pd.Series,
             ma_gate_window: int | None = 200,
             vol_target: float | None = None,
             flp_daily: pd.Series | None = None,
             flp_sizing: bool = False,
             flp_gate: bool = False,
             fee_bps: float = FEE_BPS) -> dict:
    """Simulate long-short with optional overlays. Returns equity + trades."""
    df = df.copy()
    df["atr"] = atr(df, 14)
    if ma_gate_window:
        df["ma_gate"] = df["close"].rolling(ma_gate_window).mean()
    else:
        df["ma_gate"] = np.nan

    # Realized vol for vol targeting (30d)
    df["realized_vol"] = df["close"].pct_change().rolling(30).std() * np.sqrt(365)

    position = 0
    entry_price = 0.0
    stop_price = 0.0
    entry_size = 0.0
    entry_idx = 0
    equity = [1.0]
    trades = []

    for i in range(1, len(df)):
        close = df["close"].iloc[i]
        gate = df["ma_gate"].iloc[i]
        a = df["atr"].iloc[i]
        rv = df["realized_vol"].iloc[i]
        want_long = bool(long_sig.iloc[i]) if not pd.isna(long_sig.iloc[i]) else False
        want_short = bool(short_sig.iloc[i]) if not pd.isna(short_sig.iloc[i]) else False

        # Optional FLP gate
        if flp_gate and flp_daily is not None:
            fv = flp_daily.iloc[i]
            if not pd.isna(fv):
                if want_long and fv >= 60:
                    want_long = False
                if want_short and fv <= 55:
                    want_short = False

        # Exits
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
                fee = 2 * (fee_bps / 10000) * entry_size
                trades.append({
                    "entry_date": df.index[entry_idx],
                    "exit_date": df.index[i],
                    "side": "long" if position == 1 else "short",
                    "return": ret - fee,
                    "reason": reason,
                })
                position = 0
                entry_size = 0.0

        # Entries
        if position == 0 and not pd.isna(a):
            # Gate check: only enter if price on correct side of gate MA
            gate_ok_long = (not ma_gate_window) or (not pd.isna(gate) and close > gate)
            gate_ok_short = (not ma_gate_window) or (not pd.isna(gate) and close < gate)

            if want_long and gate_ok_long:
                position = 1
                entry_price = close
                stop_price = close - 2 * a
                entry_idx = i
                entry_size = 1.0
                if vol_target and not pd.isna(rv) and rv > 0:
                    entry_size *= min(2.0, vol_target / rv)
                if flp_sizing and flp_daily is not None:
                    fv = flp_daily.iloc[i]
                    if not pd.isna(fv):
                        if fv < 60: entry_size *= 1.0
                        elif fv < 65: entry_size *= 0.5
                        else: entry_size *= 0.0
            elif want_short and gate_ok_short:
                position = -1
                entry_price = close
                stop_price = close + 2 * a
                entry_idx = i
                entry_size = 1.0
                if vol_target and not pd.isna(rv) and rv > 0:
                    entry_size *= min(2.0, vol_target / rv)
                if flp_sizing and flp_daily is not None:
                    fv = flp_daily.iloc[i]
                    if not pd.isna(fv):
                        if fv > 65: entry_size *= 1.0
                        elif fv > 60: entry_size *= 0.5
                        else: entry_size *= 0.0

        # Mark to market
        if position == 0:
            equity.append(equity[-1])
        else:
            daily_ret = (close - df["close"].iloc[i - 1]) / df["close"].iloc[i - 1] * position
            equity.append(equity[-1] * (1 + daily_ret * entry_size))

    eq = pd.Series(equity, index=df.index)
    m = metrics(eq)
    m["n_trades"] = len(trades)
    m["trades"] = trades
    m["equity"] = eq
    return m


def metrics(eq: pd.Series) -> dict:
    if len(eq) < 30:
        return {"error": "short"}
    r = eq.pct_change().dropna()
    ann_ret = (eq.iloc[-1] ** (365 / len(eq))) - 1
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


# ---------------------------------------------------------------------------
# Strategy runner
# ---------------------------------------------------------------------------
def run_strategies(df: pd.DataFrame, flp_daily: pd.Series,
                    ma_window: int = 200) -> dict[str, dict]:
    """Run all strategies on the given df. Returns {name: metrics}."""
    out = {}

    # A. Baseline long-short
    L, S = sig_trend(df, ma_window)
    out["Trend LS"] = simulate(df, L, S, ma_gate_window=ma_window, flp_daily=flp_daily)

    # B. Long-only
    out["Trend Long-only"] = simulate(df, L, S & False, ma_gate_window=ma_window, flp_daily=flp_daily)

    # C. Short-only
    out["Trend Short-only"] = simulate(df, L & False, S, ma_gate_window=ma_window, flp_daily=flp_daily)

    # D. Dual-MA ensemble
    DL, DS = sig_dual_ma(df)
    out["Dual-MA"] = simulate(df, DL, DS, ma_gate_window=ma_window, flp_daily=flp_daily)

    # E. Vol-targeted
    out["Trend vol-target"] = simulate(df, L, S, ma_gate_window=ma_window,
                                        vol_target=0.30, flp_daily=flp_daily)

    # F. FLP sizing overlay
    out["Trend + FLP sizing"] = simulate(df, L, S, ma_gate_window=ma_window,
                                          flp_daily=flp_daily, flp_sizing=True)

    # G. Quantile FLP standalone
    QL, QS = sig_quantile_flp(flp_daily)
    out["FLP quantile only"] = simulate(df, QL, QS, ma_gate_window=None, flp_daily=flp_daily)

    # H. Trend + FLP gate
    out["Trend + FLP gate"] = simulate(df, L, S, ma_gate_window=ma_window,
                                        flp_daily=flp_daily, flp_gate=True)

    return out


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------
def walk_forward(df: pd.DataFrame, flp_daily: pd.Series,
                 ma_grid: list[int] = [50, 75, 100, 125, 150, 175, 200]):
    """3-fold walk-forward. Fold 1 = IS grid search. Folds 2-3 = OOS."""
    n = len(df)
    folds = [(0, n // 3), (n // 3, 2 * n // 3), (2 * n // 3, n)]

    # IS: grid search best MA on fold 1
    is_scores = {}
    for w in ma_grid:
        d_is = df.iloc[folds[0][0]:folds[0][1]]
        f_is = flp_daily.iloc[folds[0][0]:folds[0][1]]
        res = run_strategies(d_is, f_is, ma_window=w)
        is_scores[w] = res["Trend LS"]["sharpe"]

    best_w = max(is_scores, key=is_scores.get)
    print(f"    IS grid search: best MA = {best_w} (Sharpe {is_scores[best_w]:.2f})")
    print(f"    All IS Sharpes: " + "  ".join(f"{w}:{s:.2f}" for w, s in is_scores.items()))

    # OOS: apply best MA to folds 2-3
    oos_returns = []
    for i in (1, 2):
        d_oos = df.iloc[folds[i][0]:folds[i][1]]
        f_oos = flp_daily.iloc[folds[i][0]:folds[i][1]]
        res = run_strategies(d_oos, f_oos, ma_window=best_w)
        oos_returns.append(res["Trend LS"])

    # Concatenate OOS equity
    combined_eq = pd.concat([r["equity"] for r in oos_returns])
    combined_eq = combined_eq / combined_eq.iloc[0]
    oos_m = metrics(combined_eq)
    oos_m["n_trades"] = sum(r["n_trades"] for r in oos_returns)

    return best_w, oos_m, is_scores


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 100)
    print("TREND-FOLLOWING BACKTEST WITH SPINOFFS")
    print("=" * 100)

    print(f"\n{'Coin':>6}  {'Period':>24}  {'Days':>6}")
    print(f"{'-'*44}")
    for coin in COINS:
        df = fetch_daily(coin, days=1500)
        if df.empty:
            print(f"{coin:>6}  no data")
            continue
        print(f"{coin:>6}  {df.index[0].date()} to {df.index[-1].date()}  {len(df):>6}")

    print(f"\n{'='*100}")
    print("FULL-SAMPLE STRATEGY COMPARISON (in-sample, no walk-forward split)")
    print(f"{'='*100}")

    summary = {}
    for coin in COINS:
        df = fetch_daily(coin, days=1500)
        if df.empty or len(df) < 400:
            continue
        flp_daily = build_flp_daily(df.index)

        print(f"\n  --- {coin} ({len(df)} days) ---")
        results = run_strategies(df, flp_daily, ma_window=200)

        print(f"  {'Strategy':>22}  {'Total':>9}  {'Ann.Ret':>9}  {'Ann.Vol':>9}  "
              f"{'Sharpe':>7}  {'Sortino':>8}  {'MaxDD':>8}  {'N':>5}")
        print(f"  {'-'*94}")
        for name, r in results.items():
            if "error" in r:
                print(f"  {name:>22}  {r['error']}")
                continue
            print(f"  {name:>22}  "
                  f"{r['total_return']*100:>8.1f}%  "
                  f"{r['ann_return']*100:>8.1f}%  "
                  f"{r['ann_vol']*100:>8.1f}%  "
                  f"{r['sharpe']:>7.2f}  "
                  f"{r['sortino']:>8.2f}  "
                  f"{r['max_dd']*100:>7.1f}%  "
                  f"{r['n_trades']:>5}")

        summary[coin] = {name: r["sharpe"] for name, r in results.items() if "error" not in r}

    # Summary matrix
    print(f"\n{'='*100}")
    print("SHARPE SUMMARY (all coins × all strategies)")
    print(f"{'='*100}")
    strat_names = list(next(iter(summary.values())).keys())
    print(f"  {'Strategy':>22}  " + "  ".join(f"{c:>7}" for c in summary))
    print(f"  {'-'*94}")
    for s in strat_names:
        row = "  ".join(f"{summary[c].get(s, float('nan')):>7.2f}" for c in summary)
        print(f"  {s:>22}  {row}")

    # Walk-forward
    print(f"\n{'='*100}")
    print("WALK-FORWARD VALIDATION (3-fold, MA grid search on fold 1)")
    print(f"{'='*100}")
    for coin in COINS:
        df = fetch_daily(coin, days=1500)
        if df.empty or len(df) < 600:
            continue
        flp_daily = build_flp_daily(df.index)
        print(f"\n  --- {coin} ---")
        best_w, oos_m, is_scores = walk_forward(df, flp_daily)
        print(f"    OOS: total={oos_m['total_return']*100:+.1f}%  "
              f"ann={oos_m['ann_return']*100:+.1f}%  "
              f"Sharpe={oos_m['sharpe']:.2f}  "
              f"MaxDD={oos_m['max_dd']*100:.1f}%  "
              f"n_trades={oos_m['n_trades']}")

    print(f"\n{'='*100}")
    print("Done.")


if __name__ == "__main__":
    main()
