"""
Directional perp strategy backtest.

Combines three signals on BTC/ETH/SOL:
  1. Trend:        close > 200d MA  (long) or close < 200d MA  (short)
  2. Funding:      positive funding = crowded long -> favor short
                   negative funding = crowded short -> favor long
  3. Momentum:     recent N-day return has the same sign as the trade

When all three align, open a directional perp at leverage L.
Stop:  2 * ATR (relative to underlying, not levered)
Target: 4 * ATR
Time stop: 30 days

Leverage range: 1x to 10x. Position size fixed at RISK_PCT of equity
per trade (loss at stop = RISK_PCT * equity).

Costs: HL taker fees ~4.5 bps per side, slippage ~3 bps.
Daily funding cost charged for holding (if long and funding positive,
you pay; if short and funding positive, you receive).

Historic funding: fetched from HL's fundingHistory endpoint.
"""
from __future__ import annotations
import sys
import time
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
import numpy as np
import pandas as pd
import requests


HL_URL = "https://api.hyperliquid.xyz/info"
CACHE_DIR = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports/.dir_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

COINS = ["BTC", "ETH", "SOL"]

# ---- Strategy params (deliberately few to avoid overfitting) ----
TREND_MA_DAYS = 200
MOMENTUM_DAYS = 7          # 7-day return must agree with trade direction
ATR_WINDOW = 14
STOP_ATR_MULT = 2.0
TARGET_ATR_MULT = 4.0
TIME_STOP_DAYS = 30
RISK_PCT = 0.02            # 2% of equity at risk per trade

# Costs
TAKER_BPS = 4.5
SLIP_BPS = 3.0
RT_COST_FRAC = (TAKER_BPS + SLIP_BPS) * 2 / 10000

# Data
LOOKBACK_DAYS = 1500


def post(payload: dict, timeout: int = 20) -> dict | None:
    try:
        r = requests.post(HL_URL, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  request failed: {e}")
        return None


def fetch_daily(coin: str) -> pd.DataFrame | None:
    cache = CACHE_DIR / f"{coin}_1d.pkl"
    if cache.exists():
        age = (time.time() - cache.stat().st_mtime) / 3600
        if age < 12:
            return pd.read_pickle(cache)

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - LOOKBACK_DAYS * 24 * 3600 * 1000
    data = post({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1d",
                "startTime": start_ms, "endTime": end_ms},
    })
    if not data:
        return None
    df = pd.DataFrame([{
        "t": int(c["t"]), "open": float(c["o"]), "high": float(c["h"]),
        "low": float(c["l"]), "close": float(c["c"]), "volume": float(c["v"]),
    } for c in data])
    df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None)
    df = df.set_index("date").sort_index()
    df.to_pickle(cache)
    return df


def fetch_funding(coin: str) -> pd.DataFrame | None:
    """Fetch historical funding rates (daily aggregation)."""
    cache = CACHE_DIR / f"{coin}_funding.pkl"
    if cache.exists():
        age = (time.time() - cache.stat().st_mtime) / 3600
        if age < 12:
            return pd.read_pickle(cache)

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - LOOKBACK_DAYS * 24 * 3600 * 1000
    data = post({
        "type": "fundingHistory",
        "coin": coin,
        "startTime": start_ms,
        "endTime": end_ms,
    })
    if not data:
        return None
    df = pd.DataFrame([{
        "t": int(x["time"]),
        "funding": float(x["fundingRate"]),
    } for x in data])
    df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None)
    df = df.set_index("date").sort_index()
    # Aggregate to daily mean
    daily = df["funding"].resample("1D").mean()
    daily.to_pickle(cache)
    return daily.to_frame()


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def simulate(df: pd.DataFrame, funding: pd.Series,
             leverage: float,
             risk_pct: float = RISK_PCT,
             trend_ma: int = TREND_MA_DAYS,
             mom_days: int = MOMENTUM_DAYS) -> dict:
    """
    Directional perp simulation.

    Position sizing: notional such that (STOP_ATR_MULT * ATR / price) * notional
    = risk_pct * equity. That means if stop hits, we lose exactly risk_pct.
    Leverage L multiplies notional by L. High L means more notional per trade
    but the same dollar loss at stop (because stop is closer in % terms).
    """
    df = df.copy()
    df["ma"] = df["close"].rolling(trend_ma).mean()
    df["atr"] = atr(df, ATR_WINDOW)
    df["mom"] = df["close"].pct_change(mom_days)

    # Align funding to daily index (forward fill, treat NaN as 0)
    fund = funding.reindex(df.index, method="ffill").fillna(0.0)

    equity = 1.0
    equity_curve = [equity]
    trades = []

    position = 0
    entry_price = 0.0
    entry_idx = 0
    stop = 0.0
    target = 0.0
    notional = 0.0
    funding_accum = 0.0

    for i in range(1, len(df)):
        row = df.iloc[i]
        close = row["close"]
        high = row["high"]
        low = row["low"]
        ma = row["ma"]
        a = row["atr"]
        mom = row["mom"]
        f = fund.iloc[i] if i < len(fund) else 0.0

        # ----- CHECK EXITS -----
        if position != 0:
            hold_days = i - entry_idx
            exit_reason = None
            exit_price = close

            # Stop
            if position == 1 and low <= stop:
                exit_price = stop
                exit_reason = "stop"
            elif position == -1 and high >= stop:
                exit_price = stop
                exit_reason = "stop"
            # Target
            elif position == 1 and high >= target:
                exit_price = target
                exit_reason = "target"
            elif position == -1 and low <= target:
                exit_price = target
                exit_reason = "target"
            # Time stop
            elif hold_days >= TIME_STOP_DAYS:
                exit_price = close
                exit_reason = "time"
            # Trend flip
            elif position == 1 and close < ma:
                exit_price = close
                exit_reason = "trend_flip"
            elif position == -1 and close > ma:
                exit_price = close
                exit_reason = "trend_flip"

            if exit_reason:
                # PnL on notional
                price_ret = (exit_price - entry_price) / entry_price * position
                gross_pnl = price_ret * notional
                # Funding cost over hold period
                # Long pays when funding positive, short receives
                funding_pnl = -position * funding_accum * notional
                # Trade cost (round trip)
                cost = notional * RT_COST_FRAC
                net = gross_pnl + funding_pnl - cost
                equity += net
                trades.append({
                    "entry_i": entry_idx,
                    "exit_i": i,
                    "side": "long" if position == 1 else "short",
                    "entry": entry_price,
                    "exit": exit_price,
                    "hold_days": hold_days,
                    "notional": notional,
                    "gross_pnl": gross_pnl,
                    "funding_pnl": funding_pnl,
                    "cost": cost,
                    "net_pnl": net,
                    "return_on_equity": net / (equity - net) if (equity - net) > 0 else 0,
                    "exit_reason": exit_reason,
                    "leverage": leverage,
                })
                position = 0
                notional = 0.0
                funding_accum = 0.0

        # ----- CHECK ENTRY -----
        if position == 0 and not (pd.isna(ma) or pd.isna(a) or pd.isna(mom)):
            # Trend direction
            trend_long = close > ma
            trend_short = close < ma
            # Funding: positive funding = crowded long = favor short
            # negative funding = crowded short = favor long
            fund_long_bias = f < 0
            fund_short_bias = f > 0
            # Momentum: recent return has to agree with direction
            mom_long = mom > 0.01   # at least 1% over momentum window
            mom_short = mom < -0.01

            if trend_long and fund_long_bias and mom_long:
                position = 1
            elif trend_short and fund_short_bias and mom_short:
                position = -1

            if position != 0:
                entry_price = close
                entry_idx = i
                stop_dist = STOP_ATR_MULT * a
                target_dist = TARGET_ATR_MULT * a
                if position == 1:
                    stop = close - stop_dist
                    target = close + target_dist
                else:
                    stop = close + stop_dist
                    target = close - target_dist
                # Notional so that stop loss = risk_pct * equity
                stop_frac = stop_dist / close
                # Apply leverage to notional cap
                base_notional = (risk_pct * equity) / stop_frac
                notional = min(base_notional * leverage,
                               equity * leverage * 5)  # hard cap 5x equity
                funding_accum = 0.0

        # Accumulate funding for open position
        if position != 0:
            funding_accum += f

        # Track equity (mark-to-market for open pos)
        if position != 0:
            unreal = (close - entry_price) / entry_price * position * notional
            funding_u = -position * funding_accum * notional
            cost_u = notional * RT_COST_FRAC
            equity_curve.append(equity + unreal + funding_u - cost_u)
        else:
            equity_curve.append(equity)

    eq = pd.Series(equity_curve, index=df.index)
    return _metrics(eq, trades, leverage)


def _metrics(eq: pd.Series, trades: list, leverage: float) -> dict:
    if not trades:
        return {"error": "no trades"}
    r = eq.pct_change().dropna()
    if len(r) < 5 or r.std() == 0:
        return {"error": "no variance"}

    ann_ret = (eq.iloc[-1] ** (365 / len(eq))) - 1 if eq.iloc[-1] > 0 else -1.0
    ann_vol = r.std() * np.sqrt(365)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0

    peak = eq.cummax()
    max_dd = float(((eq - peak) / peak).min())

    downside = r[r < 0]
    down_vol = downside.std() * np.sqrt(365) if len(downside) > 0 else ann_vol
    sortino = ann_ret / down_vol if down_vol > 0 else 0.0

    pnls = np.array([t["net_pnl"] for t in trades])
    rets = np.array([t["return_on_equity"] for t in trades])

    return {
        "leverage": leverage,
        "n_trades": len(trades),
        "total_return": float(eq.iloc[-1] - 1),
        "ann_return": float(ann_ret),
        "ann_vol": float(ann_vol),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_dd": float(max_dd),
        "win_rate": float((pnls > 0).mean()),
        "avg_pnl_usd": float(pnls.mean()),
        "avg_ret": float(rets.mean()),
        "best_trade": float(pnls.max()),
        "worst_trade": float(pnls.min()),
        "avg_hold": float(np.mean([t["hold_days"] for t in trades])),
        "trades": trades,
        "equity": eq,
    }


def run_coin(coin: str):
    print(f"\n{'='*90}")
    print(f"  {coin}")
    print(f"{'='*90}")

    df = fetch_daily(coin)
    if df is None or len(df) < 400:
        print(f"  insufficient data")
        return None

    print(f"  price: {len(df)} days ({df.index[0].date()} to {df.index[-1].date()})")

    fund_df = fetch_funding(coin)
    if fund_df is None:
        print(f"  funding: fetch failed, using zero funding")
        fund = pd.Series(0.0, index=df.index)
    else:
        fund = fund_df["funding"]
        print(f"  funding: {len(fund)} points")

    # Sweep leverage
    results = []
    for L in [1, 2, 3, 5, 7, 10]:
        r = simulate(df, fund, L)
        results.append(r)

    print(f"\n  {'L':>3}  {'N':>4}  {'Total':>8}  {'AnnRet':>8}  {'Sharpe':>7}  "
          f"{'Sortino':>8}  {'MaxDD':>8}  {'Win%':>6}  {'AvgHold':>8}")
    print(f"  {'-'*82}")
    for r in results:
        if "error" in r:
            print(f"  {r.get('leverage', '?'):>3}  {r['error']}")
            continue
        print(f"  {r['leverage']:>3}  {r['n_trades']:>4}  "
              f"{r['total_return']*100:>7.1f}%  "
              f"{r['ann_return']*100:>7.1f}%  "
              f"{r['sharpe']:>7.2f}  "
              f"{r['sortino']:>8.2f}  "
              f"{r['max_dd']*100:>7.1f}%  "
              f"{r['win_rate']*100:>5.1f}%  "
              f"{r['avg_hold']:>7.1f}d")

    return results


def main():
    print("=" * 90)
    print("DIRECTIONAL PERP STRATEGY — BACKTEST")
    print("=" * 90)
    print(f"  Trend MA:        {TREND_MA_DAYS}d")
    print(f"  Momentum window: {MOMENTUM_DAYS}d")
    print(f"  Stop/Target:     {STOP_ATR_MULT}x / {TARGET_ATR_MULT}x ATR")
    print(f"  Time stop:       {TIME_STOP_DAYS}d")
    print(f"  Risk per trade:  {RISK_PCT*100:.1f}% of equity")
    print(f"  Costs:           {TAKER_BPS} bps taker + {SLIP_BPS} bps slippage (per side)")
    print()
    print(f"  Signal: long if (close > 200d MA) AND (funding < 0) AND (7d return > 1%)")
    print(f"          short if (close < 200d MA) AND (funding > 0) AND (7d return < -1%)")

    all_results = {}
    for coin in COINS:
        try:
            all_results[coin] = run_coin(coin)
        except Exception as e:
            print(f"  {coin} failed: {e}")

    print(f"\n{'='*90}")
    print("SUMMARY")
    print(f"{'='*90}")
    print(f"  {'coin':>6}  {'L':>3}  {'Sharpe':>7}  {'AnnRet':>8}  {'MaxDD':>8}  {'N':>5}")
    print(f"  {'-'*50}")
    for coin, results in all_results.items():
        if not results:
            continue
        best = max((r for r in results if "error" not in r),
                   key=lambda x: x["sharpe"], default=None)
        if best:
            print(f"  {coin:>6}  {best['leverage']:>3}  "
                  f"{best['sharpe']:>7.2f}  "
                  f"{best['ann_return']*100:>7.1f}%  "
                  f"{best['max_dd']*100:>7.1f}%  "
                  f"{best['n_trades']:>5}")


if __name__ == "__main__":
    main()
