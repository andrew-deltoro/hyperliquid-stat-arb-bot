#!/usr/bin/env python
"""Can we forecast SUSCEPTIBILITY to an RSR-class move, even if not the trigger?

The RSR squeeze was not forecastable from prior price/funding/OI. But the
environment was not neutral either: RSR carried ~$188k of OI that swelled 6.7x
to $1.26M in hours. A coin whose open interest is large relative to its actual
traded volume is structurally fragile -- thin liquidity to absorb a forced
repricing, and crowded positioning to unwind.

That is a *susceptibility* screen, not a timing signal: it says which coins can
deliver an outsized move, not when. If it works, the recipe is to make sure the
pairs we hold contain such coins, so that we sample the tail more often.

Test: rank each snapshot by OI / daily volume. Do high-rank coins subsequently
produce more large 24h moves than low-rank ones?
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path("/home/hlbot/hlbot-runtime/live/state/funding_history.db")


def load() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "select ts_utc, coin, mark_px, oi_notional, day_volume_usd, "
        "premium_pct, annualized_pct from funding_snapshots "
        "where mark_px > 0 and oi_notional > 0 and day_volume_usd > 0",
        con,
    )
    con.close()
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True, format="mixed")
    return df.sort_values(["coin", "ts"]).reset_index(drop=True)


def main() -> None:
    df = load()
    g = df.groupby("coin", sort=False)
    # 24h forward absolute move: the tail we want to be exposed to.
    df["fwd_abs_24"] = (g["mark_px"].shift(-96) / df["mark_px"] - 1.0).abs()
    df["fwd_24"] = g["mark_px"].shift(-96) / df["mark_px"] - 1.0

    # Susceptibility: OI relative to what actually trades.
    df["oi_to_vol"] = df["oi_notional"] / df["day_volume_usd"]
    # Cross-sectional rank per bar (relative within that moment, so market-wide
    # regime shifts do not leak in).
    df["oi_rank"] = df.groupby("ts")["oi_to_vol"].rank(pct=True)

    d = df.dropna(subset=["fwd_abs_24", "oi_rank"]).copy()
    print("=" * 78)
    print("SUSCEPTIBILITY SCREEN: DOES HIGH OI/VOLUME PRECEDE BIG MOVES?")
    print("=" * 78)
    print(f"  rows {len(d):,}  coins {d['coin'].nunique()}  "
          f"span {d['ts'].min()} -> {d['ts'].max()}")

    # Where did RSR sit before the squeeze?
    pre = df[(df["coin"] == "RSR") & (df["ts"] < "2026-10-12 05:00")]
    if len(pre):
        r = pre.iloc[-1]
        print(f"\n  RSR just before the squeeze:")
        print(f"    oi_to_vol {r['oi_to_vol']:.4f}   cross-sectional rank "
              f"{r['oi_rank']*100:.1f}th pct")
        print(f"    OI ${r['oi_notional']:,.0f}  volume ${r['day_volume_usd']:,.0f}")

    print("\n  Forward |24h move| by OI/volume rank decile:")
    d["decile"] = pd.cut(d["oi_rank"], bins=np.arange(0, 1.01, 0.1),
                         labels=[f"{i*10}-{i*10+10}" for i in range(10)])
    print(f"  {'decile':<9} {'n':>7} {'mean|24h|':>10} {'p95|24h|':>10} "
          f"{'P(>10%)':>9} {'P(>20%)':>9}")
    for dec, sub in d.groupby("decile", observed=True):
        v = sub["fwd_abs_24"]
        print(f"  {str(dec):<9} {len(sub):>7} {v.mean()*100:>9.2f}% "
              f"{v.quantile(0.95)*100:>9.2f}% {100*(v>0.10).mean():>8.2f}% "
              f"{100*(v>0.20).mean():>8.2f}%")

    print("\n  Top decile vs bottom decile:")
    hi = d[d["oi_rank"] >= 0.9]["fwd_abs_24"]
    lo = d[d["oi_rank"] < 0.1]["fwd_abs_24"]
    print(f"    top 10%  : mean {hi.mean()*100:.2f}%  P(>10%)={100*(hi>0.10).mean():.2f}%  n={len(hi)}")
    print(f"    bottom 10%: mean {lo.mean()*100:.2f}%  P(>10%)={100*(lo>0.10).mean():.2f}%  n={len(lo)}")
    if len(hi) and len(lo):
        print(f"    ratio of means: {hi.mean()/lo.mean():.2f}x")

    # Does the screen survive removing the coin that motivated it?
    d2 = d[d["coin"] != "RSR"]
    hi2 = d2[d2["oi_rank"] >= 0.9]["fwd_abs_24"]
    lo2 = d2[d2["oi_rank"] < 0.1]["fwd_abs_24"]
    print(f"\n  Excluding RSR (avoids fitting the screen to its own event):")
    print(f"    top 10%  : mean {hi2.mean()*100:.2f}%  P(>10%)={100*(hi2>0.10).mean():.2f}%")
    print(f"    bottom 10%: mean {lo2.mean()*100:.2f}%  P(>10%)={100*(lo2>0.10).mean():.2f}%")
    if len(hi2) and len(lo2):
        print(f"    ratio of means: {hi2.mean()/lo2.mean():.2f}x")

    # Is the effect just 'large coins move more'? Control for OI size.
    print("\n  Control: is it size, or specifically OI relative to volume?")
    d["oi_size_rank"] = d.groupby("ts")["oi_notional"].rank(pct=True)
    for label, col in (("OI/volume rank", "oi_rank"),
                       ("raw OI size rank", "oi_size_rank")):
        h = d[d[col] >= 0.9]["fwd_abs_24"]
        l = d[d[col] < 0.1]["fwd_abs_24"]
        print(f"    {label:<18} top/bottom mean ratio {h.mean()/l.mean():.2f}x")

    # Event counts: how many >10% moves were in the top susceptibility decile?
    print("\n  Of all |24h moves| >10%, what share came from the top decile?")
    big = d[d["fwd_abs_24"] >= 0.10]
    if len(big):
        print(f"    {len(big)} events; {100*(big['oi_rank']>=0.9).mean():.1f}% "
              f"were in top 10% of OI/volume (vs 10% expected by chance)")
    else:
        print("    no >10% 24h moves in sample")


if __name__ == "__main__":
    main()
