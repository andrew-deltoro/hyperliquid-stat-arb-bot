#!/usr/bin/env python
"""Is the RSR signature a class we can hunt, or a one-off?

RSR/TAO made 99% of the book's P&L. The mechanism was mechanical, not
predictive: the bot was long RSR (mean reversion said RSR was cheap vs TAO)
when RSR repriced +48% in ~2h with OI up 6.7x and funding at -2616% annualized.

Two questions, kept separate:
  A) PREDICTABLE? Does the state *before* a big move look different from normal?
  B) TRADEABLE? Conditional on the dislocation signature appearing, does price
     continue or revert, net of round-trip cost?

B is asked on all 234 coins, not just the one that worked. Answering only the
latter is how a one-off becomes a strategy.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path("/home/hlbot/hlbot-runtime/live/state/funding_history.db")

# Round-trip cost actually measured from exchange fills, not modelled.
ROUND_TRIP_COST = 0.000656

HORIZONS_H = [1, 4, 12, 24]


def load() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "select ts_utc, coin, funding_hourly, annualized_pct, mark_px, "
        "oracle_px, premium_pct, oi_notional, day_volume_usd "
        "from funding_snapshots where mark_px > 0 order by coin, ts_utc",
        con,
    )
    con.close()
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True, format="mixed")
    df = df.drop(columns=["ts_utc"])
    return df


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("coin", sort=False)
    df["ret_1"] = g["mark_px"].pct_change()
    # OI change over 1h (4 snapshots at 15-min cadence) and 24h.
    df["d_oi_1h"] = g["oi_notional"].pct_change(4)
    df["d_oi_24h"] = g["oi_notional"].pct_change(96)
    df["premium"] = df["premium_pct"]
    df["fund_ann"] = df["annualized_pct"]
    # Forward returns net of a measured round trip.
    for h in HORIZONS_H:
        fwd = g["mark_px"].shift(-4 * h) / df["mark_px"] - 1.0
        df[f"fwd_{h}h"] = fwd - ROUND_TRIP_COST
    return df


def describe(df: pd.DataFrame) -> None:
    print("=" * 72)
    print("DATA")
    print("=" * 72)
    print(f"  rows           : {len(df):,}")
    print(f"  coins          : {df['coin'].nunique()}")
    print(f"  span           : {df['ts'].min()} -> {df['ts'].max()}")
    print(f"  cadence        : {len(df) / df['coin'].nunique():.0f} snaps/coin")

    big = df[df["ret_1"].abs() >= 0.10]
    print(f"\n  15-min moves >= 10%: {len(big)} across {big['coin'].nunique()} coins")
    top = big.reindex(big["ret_1"].abs().sort_values(ascending=False).index).head(8)
    print(f"  {'coin':<10} {'ts':<21} {'ret_1':>8} {'d_oi_1h':>9} {'fund_ann':>12}")
    for _, r in top.iterrows():
        print(f"  {r['coin']:<10} {str(r['ts'])[:19]:<21} {r['ret_1']*100:>7.1f}% "
              f"{r['d_oi_1h']*100 if pd.notna(r['d_oi_1h']) else float('nan'):>8.1f}% "
              f"{r['fund_ann']:>12.0f}")


def predictable(df: pd.DataFrame) -> None:
    """A) Does pre-move state separate big moves from normal ones?"""
    print()
    print("=" * 72)
    print("A) IS A BIG MOVE PREDICTABLE FROM THE PRIOR STATE?")
    print("=" * 72)
    d = df.dropna(subset=["d_oi_1h", "fwd_1h"]).copy()
    d["big"] = (d["ret_1"].abs() >= 0.10).astype(int)

    n_big = int(d["big"].sum())
    print(f"  events >= 10%: {n_big}  (base rate {100*n_big/len(d):.3f}%)")
    if n_big < 20:
        print("  !! too few events to separate signal from noise")

    # Compare prior-state distributions, big vs rest.
    print(f"\n  {'feature':<14} {'big mean':>12} {'rest mean':>12} {'big p50':>10} {'rest p50':>10}")
    for feat in ("d_oi_1h", "d_oi_24h", "premium", "fund_ann"):
        b = d.loc[d["big"] == 1, feat].dropna()
        r = d.loc[d["big"] == 0, feat].dropna()
        if len(b) and len(r):
            print(f"  {feat:<14} {b.mean():>12.5f} {r.mean():>12.5f} "
                  f"{b.median():>10.5f} {r.median():>10.5f}")

    # Does an OI surge flag the next 1h?
    print("\n  Conditional on an OI surge (top 2% of d_oi_1h):")
    thr = d["d_oi_1h"].quantile(0.98)
    for lo, hi, label in ((0.0, 0.02, "top 2% OI surge"),
                          (0.98, 1.01, "bottom 2% OI flush")):
        pass
    hi = d[d["d_oi_1h"] >= thr]
    print(f"    OI surge rows   : {len(hi)}  mean |ret_1| = "
          f"{hi['ret_1'].abs().mean()*100:.2f}%")
    print(f"    unconditional   :           mean |ret_1| = "
          f"{d['ret_1'].abs().mean()*100:.2f}%")


def tradeable(df: pd.DataFrame) -> None:
    """B) Conditional on the signature, does price continue or revert?"""
    print()
    print("=" * 72)
    print("B) IS THE DISLOCATION TRADEABLE? (net of measured cost)")
    print("=" * 72)

    d = df.dropna(subset=["d_oi_1h"]).copy()

    # The RSR signature: a violent repricing bar, large OI jump, funding spike.
    def report(mask: pd.Series, label: str) -> None:
        sub = d[mask]
        n = len(sub)
        print(f"\n  {label}  (n={n})")
        if n < 10:
            print("    too few observations")
            return
        for h in HORIZONS_H:
            col = f"fwd_{h}h"
            v = sub[col].dropna()
            base = d[col].dropna()
            if len(v) < 10:
                continue
            t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if v.std(ddof=1) > 0 else 0.0
            print(f"    +{h:>2}h: mean {v.mean()*100:>7.3f}%  "
                  f"median {v.median()*100:>7.3f}%  t={t:>+5.2f}  "
                  f"| all coins {base.mean()*100:>7.3f}%")

    # Follow the move (continuation) vs fade it (reversion), same rows.
    shock = d["ret_1"].abs() >= 0.10
    report(shock, "any 15-min move >= 10%")
    # Sign split: continuation means following the sign of the shock.
    up = d["ret_1"] >= 0.10
    for h in HORIZONS_H:
        col = f"fwd_{h}h"
        v = d.loc[up, col].dropna()
        if len(v) >= 10:
            print(f"    up-shock, follow +{h}h: {v.mean()*100:>7.3f}%")

    # OI surge without a big price move (coiling).
    thr = d["d_oi_1h"].quantile(0.99)
    report((d["d_oi_1h"] >= thr) & (d["ret_1"].abs() < 0.02),
           f"OI surge top 1% with quiet price (coil)")
    report(d["d_oi_1h"] >= thr, "OI surge top 1% (any)")

    # Funding dislocation.
    ft = d["fund_ann"].abs().quantile(0.99)
    report(d["fund_ann"].abs() >= ft, "|funding| top 1% annualized")


def rsr_case(df: pd.DataFrame) -> None:
    print()
    print("=" * 72)
    print("C) THE RSR EVENT ITSELF")
    print("=" * 72)
    r = df[df["coin"] == "RSR"].copy()
    if r.empty:
        print("  no RSR rows")
        return
    move = r.loc[r["ret_1"].abs().idxmax()]
    print(f"  largest RSR bar : {str(move['ts'])[:19]}  ret_1={move['ret_1']*100:+.1f}%")
    print(f"  d_oi_1h         : {move['d_oi_1h']*100 if pd.notna(move['d_oi_1h']) else float('nan'):+.1f}%")
    print(f"  premium         : {move['premium']:.3f}%")
    print(f"  funding ann     : {move['fund_ann']:.0f}%")
    print(f"  OI before/after : {r['oi_notional'].iloc[max(0, r.index.get_loc(move.name)-4)]:,.0f}"
          f" -> {move['oi_notional']:,.0f}")
    print("\n  Where did the OI surge rank, that hour, across all coins?")
    same = df[df["ts"] == move["ts"]]
    if len(same) > 5:
        rank = (same["d_oi_1h"] > move["d_oi_1h"]).sum()
        print(f"    RSR d_oi_1h rank: {rank+1} of {len(same)} coins that bar")


def main() -> None:
    if not DB.exists():
        print(f"no database at {DB}")
        sys.exit(1)
    df = add_features(load())
    describe(df)
    predictable(df)
    tradeable(df)
    rsr_case(df)


if __name__ == "__main__":
    main()
