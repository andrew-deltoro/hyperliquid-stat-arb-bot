#!/usr/bin/env python
"""Are the RSR-class moves harvestable, and what payoff shape do they imply?

The first pass found extreme funding and OI surges "predict" decline, but the
whole universe fell 0.56% per 24h in this window, so absolute forward returns
mostly measure market drift. Everything here is therefore made CROSS-SECTIONAL:
each coin's forward return minus that bar's mean across all coins. That is the
relevant benchmark for a relative-value book, and it removes market beta.

Then the question that matters for the RSR trade: these dislocations are rare
and huge. A strategy that harvests them is a skew bet, not a hit-rate bet. So
we measure the payoff shape of the bot's actual core position -- long the
most-oversold leg -- rather than just its mean.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path("/home/hlbot/hlbot-runtime/live/state/funding_history.db")
ROUND_TRIP_COST = 0.000656
HORIZONS_H = [1, 4, 12, 24]


def load() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "select ts_utc, coin, annualized_pct, mark_px, premium_pct, "
        "oi_notional, day_volume_usd from funding_snapshots where mark_px > 0 "
        "order by ts_utc, coin",
        con,
    )
    con.close()
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True, format="mixed")
    return df.drop(columns=["ts_utc"])


def features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["coin", "ts"]).reset_index(drop=True)
    g = df.groupby("coin", sort=False)
    df["ret_1"] = g["mark_px"].pct_change()
    df["d_oi_1h"] = g["oi_notional"].pct_change(4)

    # Raw forward return, then demeaned within each bar (market neutral).
    for h in HORIZONS_H:
        fwd = g["mark_px"].shift(-4 * h) / df["mark_px"] - 1.0
        df[f"fwd_{h}h"] = fwd
        bar_mean = df.groupby("ts")[f"fwd_{h}h"].transform("mean")
        df[f"xs_{h}h"] = fwd - bar_mean - ROUND_TRIP_COST
    return df


def hdr(t: str) -> None:
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


def line(label: str, sub: pd.DataFrame, all_df: pd.DataFrame,
         col: str, n_min: int = 30) -> None:
    v = sub[col].dropna()
    if len(v) < n_min:
        print(f"  {label:<34} +{col[4:-1]:>2}h  n={len(v)} (too few)")
        return
    t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if v.std(ddof=1) > 0 else 0.0
    base = all_df[col].dropna().mean()
    print(f"  {label:<34} +{col[4:-1]:>2}h  n={len(v):<5} mean {v.mean()*100:>+7.3f}%  "
          f"t={t:>+5.2f}   (universe {base*100:>+6.3f}%)")


def main() -> None:
    if not DB.exists():
        print(f"no database at {DB}")
        sys.exit(1)
    df = features(load())

    hdr("MARKET-NEUTRAL FORWARD RETURNS (excess vs that bar's cross-section)")
    print(f"  rows {len(df):,}   coins {df['coin'].nunique()}   "
          f"span {df['ts'].min()} -> {df['ts'].max()}")
    print(f"  cost deducted: {ROUND_TRIP_COST*100:.4f}% per round trip")
    print("\n  universe absolute drift (why demeaning matters):")
    for h in HORIZONS_H:
        v = df[f"fwd_{h}h"].dropna()
        print(f"    +{h:>2}h raw: {v.mean()*100:>+7.3f}%   "
              f"market-neutral: {df[f'xs_{h}h'].dropna().mean()*100:>+7.3f}%")

    hdr("DO THE RSR-CLASS FEATURES SURVIVE THE MARKET-NEUTRAL TEST?")
    d = df.dropna(subset=["d_oi_1h"]).copy()
    oi_thr = d["d_oi_1h"].quantile(0.99)
    f_thr = d["annualized_pct"].abs().quantile(0.99)

    cands = {
        "big bar >=10%": d["ret_1"].abs() >= 0.10,
        "big UP bar >=10%": d["ret_1"] >= 0.10,
        "big DOWN bar >=10%": d["ret_1"] <= -0.10,
        "OI surge top1%": d["d_oi_1h"] >= oi_thr,
        "OI surge + quiet price": (d["d_oi_1h"] >= oi_thr) & (d["ret_1"].abs() < 0.02),
        "|funding| top1%": d["annualized_pct"].abs() >= f_thr,
        "premium <= -1%": d["premium_pct"] <= -1.0,
        "most oversold (z<-2)": None,
    }

    # The bot's actual core bet: long the most-oversold leg in a pair.
    r1 = d["ret_1"]
    z = (r1 - r1.groupby(d["ts"]).transform("mean")) / \
        r1.groupby(d["ts"]).transform("std")
    d["z_ret"] = z
    cands["most oversold (z<-2)"] = d["z_ret"] <= -2.0
    cands["most overbought (z>+2)"] = d["z_ret"] >= 2.0

    for label, mask in cands.items():
        if mask is None:
            continue
        sub = d[mask]
        print(f"\n  -- {label}  (n={len(sub)})")
        for h in HORIZONS_H:
            line("excess return", sub, d, f"xs_{h}h")

    hdr("PAYOFF SHAPE: IS THIS A SKEW BET OR A HIT-RATE BET?")
    print("  The bot goes long the cheap leg. Its return is therefore the")
    print("  mirrored distribution of that leg's forward move -- capped at -100%,")
    print("  unbounded up. Judging it on the mean of 18 trades hides this.")
    sub = d[d["z_ret"] <= -2.0]
    for h in (24,):
        v = sub[f"fwd_{h}h"].dropna()
        if len(v) < 30:
            continue
        q = v.quantile([0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99])
        print(f"\n  long most-oversold, raw +{h}h return (n={len(v)}):")
        print(f"    mean {v.mean()*100:>+7.2f}%   sd {v.std()*100:>6.2f}%")
        for p, val in q.items():
            print(f"    p{int(p*100):<3} {val*100:>+7.2f}%")
        print(f"    worst {v.min()*100:>+7.2f}%   best {v.max()*100:>+7.2f}%")
        print(f"    skew {v.skew():>+6.2f}   "
              f"P(>+20%) {100*(v>0.20).mean():.2f}%   P(<-20%) {100*(v<-0.20).mean():.2f}%")

    hdr("HARVEST RATE: HOW OFTEN DOES A LEG MOVE BIG ENOUGH?")
    print("  If the bot is already positioned and just needs the squeeze, the")
    print("  binding constraint is how often a universe coin delivers one.")
    n_coins = df["coin"].nunique()
    n_days = (df["ts"].max() - df["ts"].min()).total_seconds() / 86400
    for thr in (0.10, 0.20, 0.40):
        ev = d[d["ret_1"].abs() >= thr]
        per_day = len(ev) / n_days
        print(f"\n  |15-min move| >= {thr*100:>2.0f}%:  {len(ev):>4} events over "
              f"{n_days:.1f}d across {n_coins} coins")
        print(f"    = {per_day:.2f} events/day universe-wide "
              f"({ev['coin'].nunique()} distinct coins)")
    print("\n  Our live book holds ~2-4 pairs at a time out of 234 coins.")
    print("  That is the crux: at 2-4 pairs we sample a small slice of the")
    print("  universe, so we catch these events at the rate chance allows.")


if __name__ == "__main__":
    main()
