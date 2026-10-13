#!/usr/bin/env python3
"""
ES-1 falsification: Weekly Skylark FLP → BTC.

Strategies
  A  buy_hold     — always long
  B  es1_flat     — long iff FLP < 50 (Favorable); else flat  [primary]
  C  es1_ma       — long iff Favorable AND close > 200d MA
  D  trend_only   — long iff close > 200d MA (no FLP)

Also scores Favorable *episodes* like the friend's PDF (forward 3/6/12M
from regime-exit) for comparison — research only.

Price: HL BTC-PERP daily (2021-04 → now). Costs: round-trip fee_bps.
"""
from __future__ import annotations

import csv
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.hyperliquid_bot.data.ohlcv import fetch_candles

SKYLARK_CSV = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vT0wFeLanhrsgYBVRth-g5VBYTvODZZWKyv1TtuoZfyFRBoUWWmHE48rbs-22K9VgBNkQYROQU83xVS"
    "/pub?output=csv"
)
FEE_BPS = 9.0  # ~2 × 4.5 bps taker on enter+exit; conservative


def load_weekly_flp() -> pd.Series:
    r = requests.get(SKYLARK_CSV, timeout=30)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    pts = []
    for row in rows:
        d = (row.get("Date") or "").strip()
        f = (row.get("WoW FLP") or "").strip()
        if not d or not f:
            continue
        try:
            flp = float(f)
        except ValueError:
            continue
        dt = None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                dt = datetime.strptime(d, fmt)
                break
            except ValueError:
                pass
        if dt is None:
            continue
        pts.append((pd.Timestamp(dt), flp))
    pts.sort()
    s = pd.Series([v for _, v in pts], index=pd.DatetimeIndex([t for t, _ in pts]),
                  name="flp")
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s


def load_btc_hl(days: int = 2000) -> pd.DataFrame:
    candles = fetch_candles("BTC", interval="1d", lookback_hours=24 * days)
    df = pd.DataFrame([{
        "t": c.t, "open": c.o, "high": c.h, "low": c.l, "close": c.c, "volume": c.v,
    } for c in candles])
    df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None)
    return df.set_index("date").sort_index()


def metrics(eq: pd.Series) -> dict:
    daily = eq.pct_change().dropna()
    n = max(len(eq), 1)
    years = n / 365.25
    total = float(eq.iloc[-1] / eq.iloc[0] - 1)
    cagr = float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 and eq.iloc[-1] > 0 else float("nan")
    vol = float(daily.std() * np.sqrt(365)) if len(daily) else float("nan")
    sharpe = cagr / vol if vol and vol > 0 else float("nan")
    peak = eq.cummax()
    max_dd = float(((eq - peak) / peak).min())
    return {
        "total": total, "cagr": cagr, "vol": vol, "sharpe": sharpe, "max_dd": max_dd,
        "end": float(eq.iloc[-1]),
    }


def simulate_position(df: pd.DataFrame, want_long: pd.Series, fee_bps: float) -> tuple[pd.Series, int]:
    """Daily MTM with position in {0,1}; fee charged on position changes."""
    pos = want_long.astype(float).reindex(df.index).fillna(0.0)
    # Align signal to next bar open/close: use today's close decision for tomorrow
    pos = pos.shift(1).fillna(0.0)
    ret = df["close"].pct_change().fillna(0.0)
    turnover = pos.diff().abs().fillna(pos.abs())
    fee = turnover * (fee_bps / 10000.0)
    strat_ret = pos * ret - fee
    eq = (1.0 + strat_ret).cumprod()
    n_entries = int((pos.diff().fillna(0) > 0).sum())
    return eq, n_entries


def favorable_episodes(flp_w: pd.Series, px: pd.Series) -> list[dict]:
    """PDF-style: episodes where FLP < 50; forward returns after exit."""
    fav = flp_w < 50.0
    episodes = []
    i = 0
    idx = flp_w.index
    while i < len(fav):
        if not fav.iloc[i]:
            i += 1
            continue
        start = idx[i]
        j = i
        while j < len(fav) and fav.iloc[j]:
            j += 1
        end = idx[j - 1]
        # exit = first week after favorable run
        exit_dt = idx[j] if j < len(idx) else end
        # DCA avg cost over favorable weeks (use weekly close ≈ daily close asof)
        weeks = idx[i:j]
        costs = []
        for w in weeks:
            # price on/after week date
            loc = px.index.searchsorted(w)
            if loc >= len(px):
                continue
            costs.append(float(px.iloc[loc]))
        if not costs:
            i = j
            continue
        avg = float(np.mean(costs))
        row = {
            "entry": start.date().isoformat(),
            "exit": exit_dt.date().isoformat(),
            "weeks": len(weeks),
            "flp0": float(flp_w.iloc[i]),
            "avg_cost": avg,
        }
        for label, months in [("r3m", 3), ("r6m", 6), ("r12m", 12)]:
            tgt = exit_dt + pd.DateOffset(months=months)
            loc = px.index.searchsorted(tgt)
            if loc >= len(px):
                row[label] = None
            else:
                row[label] = float(px.iloc[min(loc, len(px) - 1)] / avg - 1)
        episodes.append(row)
        i = j
    return episodes


def summarize_eps(eps: list[dict], key: str) -> str:
    xs = [e[key] for e in eps if e.get(key) is not None]
    if not xs:
        return f"{key}: n=0"
    arr = np.array(xs, dtype=float)
    return (f"{key}: n={len(arr)} med={np.median(arr):+.1%} "
            f"avg={np.mean(arr):+.1%} win={(arr > 0).mean():.0%}")


def main():
    print("=" * 72)
    print("ES-1 BACKTEST  |  Weekly FLP → BTC  |  fee_bps =", FEE_BPS)
    print("=" * 72)

    flp_w = load_weekly_flp()
    print(f"FLP weekly: {len(flp_w)} pts  {flp_w.index[0].date()} → {flp_w.index[-1].date()}")
    print(f"  Favorable weeks (FLP<50): {(flp_w < 50).sum()}  "
          f"Trans: {((flp_w >= 50) & (flp_w < 65)).sum()}  "
          f"Defensive: {(flp_w >= 65).sum()}")

    btc = load_btc_hl(2000)
    print(f"BTC HL daily: {len(btc)} days  {btc.index[0].date()} → {btc.index[-1].date()}")

    # Restrict FLP to price window; ffill weekly → daily
    flp_d = flp_w.reindex(btc.index, method="ffill")
    # Drop days before first FLP observation in window
    mask = flp_d.notna()
    btc = btc.loc[mask]
    flp_d = flp_d.loc[mask]
    print(f"Aligned sample: {btc.index[0].date()} → {btc.index[-1].date()}  ({len(btc)} days)")

    ma200 = btc["close"].rolling(200).mean()
    fav = flp_d < 50.0
    def_ = flp_d >= 65.0
    print(f"In sample Favorable days: {int(fav.sum())} ({100*fav.mean():.1f}%)  "
          f"Defensive days: {int(def_.sum())} ({100*def_.mean():.1f}%)")

    strategies = {
        "A buy_hold": pd.Series(1.0, index=btc.index),
        "B es1_flat (PRIMARY)": fav.astype(float),
        "C es1_ma": (fav & (btc["close"] > ma200)).astype(float),
        "D trend_only": (btc["close"] > ma200).astype(float),
        "E always_flat": pd.Series(0.0, index=btc.index),
    }

    print(f"\n{'Strategy':28s} {'Total':>8} {'CAGR':>8} {'Vol':>8} {'Sharpe':>7} "
          f"{'MaxDD':>8} {'InMkt':>7} {'Entries':>8}")
    print("-" * 90)
    results = {}
    for name, want in strategies.items():
        # For buy_hold, still charge negligible fees once
        eq, n_ent = simulate_position(btc, want, FEE_BPS if name != "A buy_hold" else 0.0)
        if name == "A buy_hold":
            # one-time enter fee
            eq = eq * (1 - FEE_BPS / 10000)
            n_ent = 1
        m = metrics(eq)
        in_mkt = float(want.fillna(0).mean())
        results[name] = m
        print(f"{name:28s} {m['total']*100:7.1f}% {m['cagr']*100:7.1f}% "
              f"{m['vol']*100:7.1f}% {m['sharpe']:7.2f} {m['max_dd']*100:7.1f}% "
              f"{in_mkt*100:6.1f}% {n_ent:8d}")

    # Pass/kill vs buy&hold
    prim = results["B es1_flat (PRIMARY)"]
    bh = results["A buy_hold"]
    print("\n--- ES-1 PRIMARY vs buy&hold (kill criteria) ---")
    beat_sharpe = prim["sharpe"] > bh["sharpe"]
    dd_improve = prim["max_dd"] > bh["max_dd"] * 0.8  # less negative by ≥20% relative
    # "cut DD by ≥20% relative" means |es1_dd| <= 0.8 * |bh_dd|
    dd_cut = abs(prim["max_dd"]) <= 0.8 * abs(bh["max_dd"])
    print(f"  Sharpe es1={prim['sharpe']:.2f} vs bh={bh['sharpe']:.2f}  "
          f"{'PASS' if beat_sharpe else 'FAIL'} (need es1 > bh)")
    print(f"  MaxDD  es1={prim['max_dd']*100:.1f}% vs bh={bh['max_dd']*100:.1f}%  "
          f"{'PASS' if dd_cut else 'FAIL'} (need |es1| ≤ 80% of |bh|)")
    print(f"  Combined keep if Sharpe OR DD rule: "
          f"{'KEEP (provisional)' if (beat_sharpe or dd_cut) else 'KILL'}")

    # Bucket forward weekly returns (does Favorable next-week beat Transitional?)
    flp_aligned = flp_w.reindex(flp_w.index)
    px_w = btc["close"].reindex(flp_w.index, method="ffill")
    # restrict to HL era
    flp_hl = flp_w[(flp_w.index >= btc.index[0]) & (flp_w.index <= btc.index[-1])]
    fwd = px_w.reindex(flp_hl.index).pct_change().shift(-1)  # next week return
    buckets = {
        "Favorable": flp_hl < 50,
        "Transitional": (flp_hl >= 50) & (flp_hl < 65),
        "Defensive": flp_hl >= 65,
    }
    print("\n--- Next-week BTC return by FLP regime (HL era) ---")
    for name, msk in buckets.items():
        x = fwd[msk].dropna()
        if len(x) == 0:
            print(f"  {name}: n=0")
            continue
        print(f"  {name:14s} n={len(x):3d}  mean={x.mean()*100:+.2f}%  "
              f"med={x.median()*100:+.2f}%  win={(x > 0).mean()*100:.0f}%")

    # PDF-style episodes on HL-era weekly FLP
    eps = favorable_episodes(flp_hl, btc["close"])
    print(f"\n--- Favorable episodes (PDF-style) on HL-era: N={len(eps)} ---")
    for e in eps:
        def fmt(v):
            return "  n/a " if v is None else f"{v*100:+6.1f}%"
        print(f"  {e['entry']} → {e['exit']}  {e['weeks']:2d}w  FLP0={e['flp0']:.1f}  "
              f"3M{fmt(e['r3m'])} 6M{fmt(e['r6m'])} 12M{fmt(e['r12m'])}")
    print("  " + summarize_eps(eps, "r3m"))
    print("  " + summarize_eps(eps, "r6m"))
    print("  " + summarize_eps(eps, "r12m"))

    print("\n" + "=" * 72)
    print("Done.")
    print("=" * 72)


if __name__ == "__main__":
    main()
