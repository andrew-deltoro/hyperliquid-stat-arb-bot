"""
Directional perp v3.

Two changes from v2:
  B. LOOKBACK 1500 -> 900 days. Universe expands from 4 to 12+ coins.
  A. Replace funding-level filter with TWO faster signals:
     - Funding velocity: 3-day change in funding rate
     - Volume spike: volume / rolling-14d avg volume
     These are proxies for "smart money repositioning," closer in spirit
     to order flow imbalance than funding level.

Signal variants tested per coin (walk-forward, params from fold 1):
  V1: trend + funding_level  (baseline, same as v2)
  V2: trend + funding_velocity
  V3: trend + volume_spike
  V4: trend + funding_velocity + volume_spike
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
CACHE_DIR = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports/.dir_cache_v3")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UNIVERSE = [
    "BTC", "ETH", "SOL",
    "AVAX", "NEAR", "INJ", "APT", "SUI", "SEI",
    "TIA", "ARB", "OP", "LINK", "BNB",
]

LOOKBACK_DAYS = 900     # <- B: shorter so more coins qualify
MIN_DAYS = 500
ATR_WINDOW = 14
STOP_ATR_MULT = 2.0
TARGET_ATR_MULT = 4.0
TIME_STOP_DAYS = 30
RISK_PCT = 0.02

TAKER_BPS = 4.5
SLIP_BPS = 3.0
RT_COST_FRAC = (TAKER_BPS + SLIP_BPS) * 2 / 10000

MA_GRID = [100, 150, 200]
MOM_GRID = [5, 7, 14]


def post(payload: dict, timeout: int = 20):
    try:
        r = requests.post(HL_URL, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
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
    cache = CACHE_DIR / f"{coin}_funding.pkl"
    if cache.exists() and (time.time() - cache.stat().st_mtime) / 3600 < 12:
        return pd.read_pickle(cache)
    end_ms = int(time.time() * 1000)
    total_start = end_ms - days * 24 * 3600 * 1000
    all_recs = []
    cursor = total_start
    chunk_ms = 30 * 24 * 3600 * 1000
    n = 0
    while cursor < end_ms:
        chunk_end = min(cursor + chunk_ms, end_ms)
        data = post({"type": "fundingHistory", "coin": coin,
                     "startTime": cursor, "endTime": chunk_end})
        if data and isinstance(data, list):
            all_recs.extend(data)
        cursor = chunk_end
        n += 1
        time.sleep(0.1)
        if n > 60:
            break
    if not all_recs:
        return None
    df = pd.DataFrame([{"t": int(x["time"]), "funding": float(x["fundingRate"])}
                       for x in all_recs])
    df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None)
    df = df.drop_duplicates(subset=["date"]).set_index("date").sort_index()
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


def simulate(df, funding, leverage, trend_ma=200, mom_days=7, signal_variant="level"):
    df = df.copy()
    df["ma"] = df["close"].rolling(trend_ma).mean()
    df["atr"] = atr(df, ATR_WINDOW)
    df["mom"] = df["close"].pct_change(mom_days)
    df["vol_avg"] = df["volume"].rolling(14).mean()
    df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, np.nan)

    fund = funding.reindex(df.index, method="ffill").fillna(0.0)
    fund_vel = fund.diff(3).fillna(0.0)   # 3-day funding change

    equity = 1.0
    curve = [equity]
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
        close, high, low = row["close"], row["high"], row["low"]
        ma, a, mom = row["ma"], row["atr"], row["mom"]
        vr = row["vol_ratio"]
        f = float(fund.iloc[i])
        fv = float(fund_vel.iloc[i])

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
                gross = price_ret * notional
                funding_pnl = -position * funding_accum * notional
                cost = notional * RT_COST_FRAC
                net = gross + funding_pnl - cost
                equity += net
                trades.append({
                    "hold_days": hold_days, "net_pnl": net,
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
            mom_long = mom > 0.01
            mom_short = mom < -0.01

            if signal_variant == "level":
                long_ok = trend_long and f < 0 and mom_long
                short_ok = trend_short and f > 0 and mom_short
            elif signal_variant == "velocity":
                long_ok = trend_long and fv < -0.0001 and mom_long
                short_ok = trend_short and fv > 0.0001 and mom_short
            elif signal_variant == "volume":
                long_ok = trend_long and vr > 1.5 and mom_long
                short_ok = trend_short and vr > 1.5 and mom_short
            elif signal_variant == "combined":
                long_ok = trend_long and fv < -0.0001 and vr > 1.3 and mom_long
                short_ok = trend_short and fv > 0.0001 and vr > 1.3 and mom_short
            else:
                long_ok = short_ok = False

            if long_ok:
                position = 1
            elif short_ok:
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
                base_notional = (RISK_PCT * equity) / stop_frac
                notional = min(base_notional * leverage, equity * leverage * 5)
                funding_accum = 0.0

        if position != 0:
            funding_accum += f

        if position != 0:
            unreal = (close - entry_price) / entry_price * position * notional
            fu = -position * funding_accum * notional
            cu = notional * RT_COST_FRAC
            curve.append(equity + unreal + fu - cu)
        else:
            curve.append(equity)

    eq = pd.Series(curve, index=df.index)
    return _metrics(eq, trades, leverage)


def _metrics(eq, trades, leverage):
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
    pnls = np.array([t["net_pnl"] for t in trades])
    return {
        "leverage": leverage,
        "n_trades": len(trades),
        "total_return": float(eq.iloc[-1] - 1),
        "ann_return": float(ann_ret),
        "sharpe": float(sharpe),
        "max_dd": float(max_dd),
        "win_rate": float((pnls > 0).mean()),
        "avg_hold": float(np.mean([t["hold_days"] for t in trades])),
    }


def walk_forward(coin, df, fund, variant):
    n = len(df)
    folds = [(0, n // 3), (n // 3, 2 * n // 3), (2 * n // 3, n)]
    d1 = df.iloc[folds[0][0]:folds[0][1]]
    f1 = fund.reindex(d1.index, method="ffill").fillna(0.0)
    best = None
    for ma in MA_GRID:
        for mom in MOM_GRID:
            r = simulate(d1, f1, leverage=1, trend_ma=ma, mom_days=mom,
                         signal_variant=variant)
            if "error" in r:
                continue
            score = r["sharpe"] - 2 * abs(r["max_dd"])
            if best is None or score > best["score"]:
                best = {"ma": ma, "mom": mom, "score": score, "is_sharpe": r["sharpe"]}
    if best is None:
        return {"error": "grid search failed"}

    oos_sh = []
    oos_ret = []
    oos_dd = []
    for fi in (1, 2):
        d = df.iloc[folds[fi][0]:folds[fi][1]]
        f = fund.reindex(d.index, method="ffill").fillna(0.0)
        r = simulate(d, f, leverage=1, trend_ma=best["ma"], mom_days=best["mom"],
                     signal_variant=variant)
        if "error" not in r:
            oos_sh.append(r["sharpe"])
            oos_ret.append(r["ann_return"])
            oos_dd.append(r["max_dd"])

    return {
        "best_ma": best["ma"],
        "best_mom": best["mom"],
        "is_sharpe": best["is_sharpe"],
        "oos_sharpe": float(np.mean(oos_sh)) if oos_sh else 0.0,
        "oos_ret": float(np.mean(oos_ret)) if oos_ret else 0.0,
        "oos_dd": float(np.mean(oos_dd)) if oos_dd else 0.0,
    }


def run_coin(coin):
    df = fetch_daily(coin)
    if df is None or len(df) < MIN_DAYS:
        return {"coin": coin, "error": "insufficient price"}
    fund = fetch_funding_paginated(coin)
    if fund is None or len(fund) < 100:
        return {"coin": coin, "error": "insufficient funding"}

    variants = ["level", "velocity", "volume", "combined"]
    wf_results = {}
    for v in variants:
        wf_results[v] = walk_forward(coin, df, fund, v)

    return {
        "coin": coin,
        "price_days": len(df),
        "funding_days": len(fund),
        "wf": wf_results,
    }


def main():
    print("=" * 110)
    print("DIRECTIONAL PERP v3 — expanded universe + signal variants")
    print("=" * 110)
    print(f"  Lookback: {LOOKBACK_DAYS}d  |  Universe: {len(UNIVERSE)} coins")
    print(f"  Variants: level (funding), velocity (funding ROC), volume (spike), combined")
    print()

    results = []
    for coin in UNIVERSE:
        print(f"  {coin:>6}...", end="", flush=True)
        r = run_coin(coin)
        results.append(r)
        if "error" in r:
            print(f" SKIP: {r['error']}")
        else:
            print(f" price={r['price_days']}d funding={r['funding_days']}d")

    # ---- Results table: OOS Sharpe per variant per coin ----
    print(f"\n{'='*110}")
    print("OUT-OF-SAMPLE SHARPE BY VARIANT (walk-forward)")
    print(f"{'='*110}")
    print(f"  {'coin':>6}  {'level':>8}  {'velocity':>10}  {'volume':>8}  {'combined':>10}  {'best':>8}")
    print(f"  {'-'*76}")
    oos_by_variant = {"level": [], "velocity": [], "volume": [], "combined": []}
    for r in results:
        if "error" in r:
            continue
        wf = r["wf"]
        row = {}
        for v in ["level", "velocity", "volume", "combined"]:
            w = wf.get(v, {})
            s = w.get("oos_sharpe", 0.0)
            if "error" not in w and w:
                oos_by_variant[v].append(s)
            row[v] = s
        best_v = max(row, key=row.get)
        print(f"  {r['coin']:>6}  "
              f"{row['level']:>8.2f}  {row['velocity']:>10.2f}  "
              f"{row['volume']:>8.2f}  {row['combined']:>10.2f}  "
              f"{best_v:>8}")

    print(f"\n{'='*110}")
    print("MEAN OOS SHARPE BY VARIANT")
    print(f"{'='*110}")
    for v, vals in oos_by_variant.items():
        if not vals:
            continue
        mean_s = float(np.mean(vals))
        n_pos = sum(1 for s in vals if s > 0.5)
        print(f"  {v:>10}: mean={mean_s:>6.2f}  coins>0.5 = {n_pos}/{len(vals)}")

    # ---- Detail of the best variant ----
    best_variant = max(oos_by_variant, key=lambda v: np.mean(oos_by_variant[v]) if oos_by_variant[v] else -999)
    print(f"\n{'='*110}")
    print(f"DETAIL — variant '{best_variant}'")
    print(f"{'='*110}")
    print(f"  {'coin':>6}  {'MA':>4}  {'mom':>4}  {'IS Sh':>7}  {'OOS Sh':>8}  {'OOS Ret':>9}  {'OOS DD':>8}")
    print(f"  {'-'*68}")
    for r in results:
        if "error" in r:
            continue
        w = r["wf"].get(best_variant, {})
        if "error" in w or not w:
            continue
        print(f"  {r['coin']:>6}  {w['best_ma']:>4}  {w['best_mom']:>4}  "
              f"{w['is_sharpe']:>7.2f}  {w['oos_sharpe']:>8.2f}  "
              f"{w['oos_ret']*100:>8.1f}%  {w['oos_dd']*100:>7.1f}%")

    print(f"\n{'='*110}")
    print("VERDICT")
    print(f"{'='*110}")
    for v, vals in oos_by_variant.items():
        if not vals:
            continue
        mean_s = float(np.mean(vals))
        n_pos = sum(1 for s in vals if s > 0.5)
        if mean_s > 0.6 and n_pos >= 4:
            print(f"  {v.upper()}: DEPLOYABLE (mean {mean_s:.2f}, {n_pos} coins >0.5)")
        elif mean_s > 0.3:
            print(f"  {v.upper()}: marginal (mean {mean_s:.2f})")
        else:
            print(f"  {v.upper()}: reject (mean {mean_s:.2f})")


if __name__ == "__main__":
    main()
