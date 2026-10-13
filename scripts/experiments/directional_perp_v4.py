"""
Directional perp v4.

Changes from v3:
  1. LOOKBACK_DAYS = 1500 (full history for BTC/ETH/SOL/AVAX/NEAR)
  2. Velocity bug fix: use funding z-score (3d change / rolling 30d std)
     instead of absolute threshold, which never fired.
  3. 5-fold walk-forward (params from fold 1, applied to folds 2-5)
     = 4 OOS folds instead of 2, doubling the effective sample.
  4. Keep the volume variant — it was the only one that held up.
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
CACHE_DIR = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports/.dir_cache_v4")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UNIVERSE = ["BTC", "ETH", "SOL", "AVAX", "NEAR"]

LOOKBACK_DAYS = 1500
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

N_FOLDS = 5


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


def simulate(df, funding, leverage, trend_ma=200, mom_days=7, variant="volume"):
    df = df.copy()
    df["ma"] = df["close"].rolling(trend_ma).mean()
    df["atr"] = atr(df, ATR_WINDOW)
    df["mom"] = df["close"].pct_change(mom_days)
    df["vol_avg"] = df["volume"].rolling(14).mean()
    df["vol_ratio"] = df["volume"] / df["vol_avg"].replace(0, np.nan)

    fund = funding.reindex(df.index, method="ffill").fillna(0.0)
    fund_chg = fund.diff(3)
    fund_std = fund.rolling(30).std().replace(0, np.nan)
    fund_z = (fund_chg / fund_std).fillna(0.0)

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
        fz = float(fund_z.iloc[i])

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

            if variant == "volume":
                long_ok = trend_long and vr > 1.5 and mom_long
                short_ok = trend_short and vr > 1.5 and mom_short
            elif variant == "velocity_z":
                # z-score of funding change: negative z (funding falling) = long
                long_ok = trend_long and fz < -0.5 and mom_long
                short_ok = trend_short and fz > 0.5 and mom_short
            elif variant == "volume_plus_z":
                long_ok = trend_long and vr > 1.3 and fz < -0.3 and mom_long
                short_ok = trend_short and vr > 1.3 and fz > 0.3 and mom_short
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


def walk_forward(coin, df, fund, variant, n_folds=N_FOLDS):
    """n_folds-fold walk-forward. Grid search on fold 1, apply to all others."""
    n = len(df)
    fold_size = n // n_folds
    folds = [(i * fold_size, (i + 1) * fold_size if i < n_folds - 1 else n)
             for i in range(n_folds)]

    # Grid search on fold 1
    d1 = df.iloc[folds[0][0]:folds[0][1]]
    f1 = fund.reindex(d1.index, method="ffill").fillna(0.0)
    best = None
    for ma in MA_GRID:
        for mom in MOM_GRID:
            r = simulate(d1, f1, leverage=1, trend_ma=ma, mom_days=mom,
                         variant=variant)
            if "error" in r:
                continue
            score = r["sharpe"] - 2 * abs(r["max_dd"])
            if best is None or score > best["score"]:
                best = {"ma": ma, "mom": mom, "score": score, "is_sharpe": r["sharpe"]}
    if best is None:
        return {"error": "grid search failed", "oos_folds": 0}

    # Apply to all other folds
    oos_sh, oos_ret, oos_dd, oos_n = [], [], [], []
    for fi in range(1, n_folds):
        d = df.iloc[folds[fi][0]:folds[fi][1]]
        f = fund.reindex(d.index, method="ffill").fillna(0.0)
        r = simulate(d, f, leverage=1, trend_ma=best["ma"], mom_days=best["mom"],
                     variant=variant)
        if "error" not in r:
            oos_sh.append(r["sharpe"])
            oos_ret.append(r["ann_return"])
            oos_dd.append(r["max_dd"])
            oos_n.append(r["n_trades"])

    return {
        "best_ma": best["ma"],
        "best_mom": best["mom"],
        "is_sharpe": best["is_sharpe"],
        "oos_sharpe": float(np.mean(oos_sh)) if oos_sh else 0.0,
        "oos_ret": float(np.mean(oos_ret)) if oos_ret else 0.0,
        "oos_dd": float(np.mean(oos_dd)) if oos_dd else 0.0,
        "oos_folds": len(oos_sh),
        "oos_trades": int(np.sum(oos_n)) if oos_n else 0,
    }


def run_coin(coin):
    df = fetch_daily(coin)
    if df is None or len(df) < MIN_DAYS:
        return {"coin": coin, "error": "insufficient price"}
    fund = fetch_funding_paginated(coin)
    if fund is None or len(fund) < 100:
        return {"coin": coin, "error": "insufficient funding"}

    variants = ["volume", "velocity_z", "volume_plus_z"]
    wf = {v: walk_forward(coin, df, fund, v) for v in variants}
    return {"coin": coin, "price_days": len(df), "funding_days": len(fund), "wf": wf}


def main():
    print("=" * 110)
    print("DIRECTIONAL PERP v4 — 5-fold walk-forward, expanded universe, velocity fix")
    print("=" * 110)
    print(f"  Universe: {UNIVERSE}")
    print(f"  Lookback: {LOOKBACK_DAYS}d  |  Folds: {N_FOLDS} (1 IS, {N_FOLDS-1} OOS)")
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

    # ---- Table: OOS Sharpe per variant per coin ----
    print(f"\n{'='*110}")
    print("OUT-OF-SAMPLE SHARPE BY VARIANT (5-fold walk-forward)")
    print(f"{'='*110}")
    print(f"  {'coin':>6}  {'volume':>9}  {'vel_z':>7}  {'vol+z':>8}  "
          f"{'best':>8}  {'oos_n':>6}  {'oos_trades':>11}")
    print(f"  {'-'*78}")
    by_variant = {"volume": [], "velocity_z": [], "volume_plus_z": []}
    for r in results:
        if "error" in r:
            continue
        row = {}
        for v in ["volume", "velocity_z", "volume_plus_z"]:
            w = r["wf"].get(v, {})
            s = w.get("oos_sharpe", 0.0)
            if "error" not in w and w.get("oos_folds", 0) > 0:
                by_variant[v].append(s)
            row[v] = s
        best_v = max(row, key=row.get)
        best_w = r["wf"].get(best_v, {})
        print(f"  {r['coin']:>6}  "
              f"{row['volume']:>9.2f}  {row['velocity_z']:>7.2f}  "
              f"{row['volume_plus_z']:>8.2f}  {best_v:>8}  "
              f"{best_w.get('oos_folds', 0):>6}  "
              f"{best_w.get('oos_trades', 0):>11}")

    # ---- Variant summary ----
    print(f"\n{'='*110}")
    print("VARIANT SUMMARY (5 coins × {n_folds-1} OOS folds)")
    print(f"{'='*110}")
    for v, vals in by_variant.items():
        if not vals:
            print(f"  {v:>14}: no valid results")
            continue
        mean_s = float(np.mean(vals))
        n_above = sum(1 for s in vals if s > 0.5)
        print(f"  {v:>14}: mean OOS Sharpe = {mean_s:>6.2f}  "
              f"coins > 0.5 = {n_above}/{len(vals)}")

    # ---- Best variant detail ----
    best_overall = max(by_variant, key=lambda v: np.mean(by_variant[v]) if by_variant[v] else -999)
    print(f"\n{'='*110}")
    print(f"DETAIL — variant '{best_overall}'")
    print(f"{'='*110}")
    print(f"  {'coin':>6}  {'MA':>4}  {'mom':>4}  {'IS Sh':>7}  {'OOS Sh':>8}  "
          f"{'OOS Ret':>9}  {'OOS DD':>8}  {'OOS N':>6}")
    print(f"  {'-'*78}")
    for r in results:
        if "error" in r:
            continue
        w = r["wf"].get(best_overall, {})
        if "error" in w or not w:
            continue
        print(f"  {r['coin']:>6}  {w['best_ma']:>4}  {w['best_mom']:>4}  "
              f"{w['is_sharpe']:>7.2f}  {w['oos_sharpe']:>8.2f}  "
              f"{w['oos_ret']*100:>8.1f}%  {w['oos_dd']*100:>7.1f}%  "
              f"{w['oos_folds']:>6}")

    # ---- Verdict ----
    print(f"\n{'='*110}")
    print("VERDICT")
    print(f"{'='*110}")
    for v, vals in by_variant.items():
        if not vals:
            continue
        mean_s = float(np.mean(vals))
        n_above = sum(1 for s in vals if s > 0.5)
        if mean_s > 0.6 and n_above >= 4:
            print(f"  ✅ {v.upper()}: DEPLOYABLE (mean {mean_s:.2f}, {n_above}/{len(vals)} coins >0.5)")
        elif mean_s > 0.3:
            print(f"  ⚠️  {v.upper()}: marginal (mean {mean_s:.2f}, {n_above}/{len(vals)} coins >0.5)")
        else:
            print(f"  ❌ {v.upper()}: reject (mean {mean_s:.2f})")


if __name__ == "__main__":
    main()
