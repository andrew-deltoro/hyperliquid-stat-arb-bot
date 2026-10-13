"""
Directional perp strategy — v2.

Fixes:
  1. Funding history: paginate in 30-day chunks (HL caps ~500 records/req)
  2. Universe: 13 coins (BTC, ETH, SOL + 10 alts)
  3. Walk-forward: 3-fold split with parameter grid search on fold 1

Signal (same as v1):
  LONG  when close > MA(N) AND funding < 0 AND return_7d > +1%
  SHORT when close < MA(N) AND funding > 0 AND return_7d < -1%

Position sizing: ATR-based, dollar risk per trade = 2% of equity.
Leverage multiplies notional.
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
import numpy as np
import pandas as pd
import requests


HL_URL = "https://api.hyperliquid.xyz/info"
CACHE_DIR = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports/.dir_cache_v2")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UNIVERSE = [
    "BTC", "ETH", "SOL",
    "AVAX", "NEAR", "INJ", "APT", "SUI", "SEI",
    "TIA", "ARB", "OP", "LINK", "BNB",
]

LOOKBACK_DAYS = 1500
ATR_WINDOW = 14
STOP_ATR_MULT = 2.0
TARGET_ATR_MULT = 4.0
TIME_STOP_DAYS = 30
RISK_PCT = 0.02

TAKER_BPS = 4.5
SLIP_BPS = 3.0
RT_COST_FRAC = (TAKER_BPS + SLIP_BPS) * 2 / 10000

# Grid search space
MA_GRID = [100, 150, 200]
MOM_GRID = [5, 7, 14]


def post(payload: dict, timeout: int = 20) -> dict | None:
    try:
        r = requests.post(HL_URL, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return None


def fetch_daily(coin: str) -> pd.DataFrame | None:
    cache = CACHE_DIR / f"{coin}_1d.pkl"
    if cache.exists() and (time.time() - cache.stat().st_mtime) / 3600 < 12:
        return pd.read_pickle(cache)

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - LOOKBACK_DAYS * 24 * 3600 * 1000
    data = post({"type": "candleSnapshot",
                 "req": {"coin": coin, "interval": "1d",
                         "startTime": start_ms, "endTime": end_ms}})
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


def fetch_funding_paginated(coin: str, days: int = LOOKBACK_DAYS) -> pd.Series | None:
    """Fetch full funding history by paginating 30-day chunks."""
    cache = CACHE_DIR / f"{coin}_funding.pkl"
    if cache.exists() and (time.time() - cache.stat().st_mtime) / 3600 < 12:
        return pd.read_pickle(cache)

    end_ms = int(time.time() * 1000)
    total_start_ms = end_ms - days * 24 * 3600 * 1000

    all_records = []
    chunk_ms = 30 * 24 * 3600 * 1000  # 30 days
    cursor = total_start_ms
    n_chunks = 0

    while cursor < end_ms:
        chunk_end = min(cursor + chunk_ms, end_ms)
        data = post({
            "type": "fundingHistory",
            "coin": coin,
            "startTime": cursor,
            "endTime": chunk_end,
        })
        if data and isinstance(data, list):
            all_records.extend(data)
        cursor = chunk_end
        n_chunks += 1
        time.sleep(0.1)
        if n_chunks > 60:  # safety
            break

    if not all_records:
        return None

    df = pd.DataFrame([{
        "t": int(x["time"]),
        "funding": float(x["fundingRate"]),
    } for x in all_records])
    df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None)
    df = df.drop_duplicates(subset=["date"]).set_index("date").sort_index()
    # Resample to daily mean
    daily = df["funding"].resample("1D").mean().fillna(0.0)
    daily.to_pickle(cache)
    return daily


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def simulate(df: pd.DataFrame, funding: pd.Series,
             leverage: float,
             trend_ma: int = 200,
             mom_days: int = 7,
             risk_pct: float = RISK_PCT) -> dict:
    """Directional perp simulation. Returns metrics dict."""
    df = df.copy()
    df["ma"] = df["close"].rolling(trend_ma).mean()
    df["atr"] = atr(df, ATR_WINDOW)
    df["mom"] = df["close"].pct_change(mom_days)

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
        f = float(fund.iloc[i]) if i < len(fund) else 0.0

        # ---- EXITS ----
        if position != 0:
            hold_days = i - entry_idx
            exit_reason = None
            exit_price = close

            if position == 1 and low <= stop:
                exit_price, exit_reason = stop, "stop"
            elif position == -1 and high >= stop:
                exit_price, exit_reason = stop, "stop"
            elif position == 1 and high >= target:
                exit_price, exit_reason = target, "target"
            elif position == -1 and low <= target:
                exit_price, exit_reason = target, "target"
            elif hold_days >= TIME_STOP_DAYS:
                exit_price, exit_reason = close, "time"
            elif position == 1 and close < ma:
                exit_price, exit_reason = close, "trend_flip"
            elif position == -1 and close > ma:
                exit_price, exit_reason = close, "trend_flip"

            if exit_reason:
                price_ret = (exit_price - entry_price) / entry_price * position
                gross_pnl = price_ret * notional
                funding_pnl = -position * funding_accum * notional
                cost = notional * RT_COST_FRAC
                net = gross_pnl + funding_pnl - cost
                equity += net
                trades.append({
                    "exit_i": i, "hold_days": hold_days,
                    "side": "long" if position == 1 else "short",
                    "net_pnl": net,
                    "return_on_equity": net / (equity - net) if (equity - net) > 0 else 0,
                    "exit_reason": exit_reason,
                })
                position = 0
                notional = 0.0
                funding_accum = 0.0

        # ---- ENTRIES ----
        if position == 0 and not (pd.isna(ma) or pd.isna(a) or pd.isna(mom)):
            trend_long = close > ma
            trend_short = close < ma
            fund_long_bias = f < 0
            fund_short_bias = f > 0
            mom_long = mom > 0.01
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
                stop_frac = stop_dist / close
                base_notional = (risk_pct * equity) / stop_frac
                notional = min(base_notional * leverage, equity * leverage * 5)
                funding_accum = 0.0

        if position != 0:
            funding_accum += f

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
        return {"error": "no trades", "leverage": leverage}
    r = eq.pct_change().dropna()
    if len(r) < 5 or r.std() == 0:
        return {"error": "no variance", "leverage": leverage}

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
        "avg_pnl": float(pnls.mean()),
        "avg_ret": float(rets.mean()),
        "best_trade": float(pnls.max()),
        "worst_trade": float(pnls.min()),
        "avg_hold": float(np.mean([t["hold_days"] for t in trades])),
        "equity": eq,
    }


def walk_forward(coin: str, df: pd.DataFrame, funding: pd.Series) -> dict:
    """3-fold walk-forward with grid search on fold 1."""
    n = len(df)
    folds = [(0, n // 3), (n // 3, 2 * n // 3), (2 * n // 3, n)]

    # Fold 1: grid search
    d1 = df.iloc[folds[0][0]:folds[0][1]]
    f1 = funding.reindex(d1.index, method="ffill").fillna(0.0)
    best = None
    for ma in MA_GRID:
        for mom in MOM_GRID:
            r = simulate(d1, f1, leverage=1, trend_ma=ma, mom_days=mom)
            if "error" in r:
                continue
            # Minimize drawdown, then maximize Sharpe
            score = r["sharpe"] - 2 * abs(r["max_dd"])
            if best is None or score > best["score"]:
                best = {"ma": ma, "mom": mom, "score": score, "sharpe": r["sharpe"]}

    if best is None:
        return {"error": "grid search failed"}

    # Apply best params to folds 2 and 3 (OOS)
    oos_sharpes = []
    oos_returns = []
    oos_dds = []
    for fi in (1, 2):
        d = df.iloc[folds[fi][0]:folds[fi][1]]
        f = funding.reindex(d.index, method="ffill").fillna(0.0)
        r = simulate(d, f, leverage=1, trend_ma=best["ma"], mom_days=best["mom"])
        if "error" not in r:
            oos_sharpes.append(r["sharpe"])
            oos_returns.append(r["ann_return"])
            oos_dds.append(r["max_dd"])

    return {
        "best_ma": best["ma"],
        "best_mom": best["mom"],
        "is_sharpe": best["sharpe"],
        "oos_sharpe_mean": float(np.mean(oos_sharpes)) if oos_sharpes else 0.0,
        "oos_ret_mean": float(np.mean(oos_returns)) if oos_returns else 0.0,
        "oos_dd_mean": float(np.mean(oos_dds)) if oos_dds else 0.0,
        "n_oos_folds": len(oos_sharpes),
    }


def run_coin(coin: str):
    df = fetch_daily(coin)
    if df is None or len(df) < 500:
        return {"coin": coin, "error": "insufficient data"}

    fund = fetch_funding_paginated(coin)
    if fund is None:
        return {"coin": coin, "error": "no funding"}

    full_len = len(fund)
    fund_coverage_days = (fund.index[-1] - fund.index[0]).days if full_len > 1 else 0

    # Full-sample sweep at multiple leverage levels (in-sample context)
    leverage_results = {}
    for L in [1, 2, 3, 5]:
        r = simulate(df, fund, leverage=L)
        leverage_results[L] = r

    # Walk-forward
    wf = walk_forward(coin, df, fund)

    return {
        "coin": coin,
        "price_days": len(df),
        "funding_points": full_len,
        "funding_coverage_days": fund_coverage_days,
        "leverage_results": leverage_results,
        "wf": wf,
    }


def main():
    print("=" * 100)
    print("DIRECTIONAL PERP STRATEGY — v2 (paginated funding + walk-forward)")
    print("=" * 100)
    print(f"  Universe: {len(UNIVERSE)} coins")
    print(f"  Lookback: {LOOKBACK_DAYS} days")
    print(f"  Grid search (fold 1): MA in {MA_GRID}, mom in {MOM_GRID}")
    print(f"  Costs: {TAKER_BPS}+{SLIP_BPS} bps per side")
    print()

    results = []
    for coin in UNIVERSE:
        print(f"  fetching {coin}...", end="", flush=True)
        r = run_coin(coin)
        results.append(r)
        if "error" in r:
            print(f" FAILED: {r['error']}")
        else:
            print(f" price={r['price_days']}d, funding={r['funding_points']}pts "
                  f"({r['funding_coverage_days']}d)")

    # ---- Table 1: in-sample Sharpe at 1x and 3x per coin ----
    print(f"\n{'='*100}")
    print("IN-SAMPLE PERFORMANCE (full sample, no walk-forward)")
    print(f"{'='*100}")
    print(f"  {'coin':>6}  {'price':>5}  {'fund':>5}  "
          f"{'L=1 Sh':>7}  {'L=1 Ret':>8}  {'L=1 DD':>7}  {'N':>4}  "
          f"{'L=3 Sh':>7}  {'L=3 Ret':>8}  {'L=3 DD':>7}  {'WR':>5}")
    print(f"  {'-'*94}")
    for r in results:
        if "error" in r:
            print(f"  {r['coin']:>6}  {r['error']}")
            continue
        lr = r["leverage_results"]
        r1 = lr.get(1, {})
        r3 = lr.get(3, {})
        if "error" in r1 or "error" in r3:
            continue
        print(f"  {r['coin']:>6}  {r['price_days']:>5}  {r['funding_points']:>5}  "
              f"{r1['sharpe']:>7.2f}  {r1['ann_return']*100:>7.1f}%  "
              f"{r1['max_dd']*100:>6.1f}%  {r1['n_trades']:>4}  "
              f"{r3['sharpe']:>7.2f}  {r3['ann_return']*100:>7.1f}%  "
              f"{r3['max_dd']*100:>6.1f}%  {r3['win_rate']*100:>4.0f}%")

    # ---- Table 2: walk-forward OOS ----
    print(f"\n{'='*100}")
    print("WALK-FORWARD VALIDATION (params from fold 1, applied to folds 2-3)")
    print(f"{'='*100}")
    print(f"  {'coin':>6}  {'best MA':>8}  {'best mom':>8}  "
          f"{'IS Sharpe':>10}  {'OOS Sharpe':>11}  {'OOS Ret':>9}  {'OOS DD':>8}")
    print(f"  {'-'*80}")
    for r in results:
        if "error" in r:
            continue
        wf = r["wf"]
        if "error" in wf:
            print(f"  {r['coin']:>6}  {wf['error']}")
            continue
        print(f"  {r['coin']:>6}  {wf['best_ma']:>8}  {wf['best_mom']:>8}  "
              f"{wf['is_sharpe']:>10.2f}  {wf['oos_sharpe_mean']:>11.2f}  "
              f"{wf['oos_ret_mean']*100:>8.1f}%  "
              f"{wf['oos_dd_mean']*100:>7.1f}%")

    # ---- Verdict ----
    print(f"\n{'='*100}")
    print("VERDICT")
    print(f"{'='*100}")
    oos_sharpes = []
    for r in results:
        if "error" in r or "error" in r.get("wf", {}):
            continue
        oos_sharpes.append(r["wf"]["oos_sharpe_mean"])

    if oos_sharpes:
        mean_oos = np.mean(oos_sharpes)
        positive = sum(1 for s in oos_sharpes if s > 0.5)
        print(f"  Mean OOS Sharpe: {mean_oos:.2f}")
        print(f"  Coins with OOS Sharpe > 0.5: {positive}/{len(oos_sharpes)}")
        if positive >= len(oos_sharpes) * 0.4 and mean_oos > 0.5:
            print(f"  → Deployable. Directional perp strategy has OOS edge.")
        elif mean_oos > 0.2:
            print(f"  → Marginal. Coin selection critical.")
        else:
            print(f"  → No OOS edge. Reject.")


if __name__ == "__main__":
    main()
