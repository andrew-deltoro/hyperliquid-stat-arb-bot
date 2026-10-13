#!/usr/bin/env python
"""The three untested ideas, plus an honest ranking of everything measured.

Tested properly this time: market-neutral framing (removes beta), a day-block
bootstrap for significance (overlapping windows across few days inflate naive
t-stats), and measured round-trip cost deducted.

Ideas:
  A) OI x funding interaction. Crowded positioning (extreme funding) combined
     with OI growth should be squeeze fuel.
  B) Relative strength vs BTC-beta. Residual return after removing BTC beta:
     continuation or reversal?
  C) Forced-flow proxy. OI falling while price moves hard = positions being
     liquidated. Does the forced move continue or overshoot and revert?

Volume caveat: day_volume_usd is a rolling 24h sum, so its 15-min change is a
noisy per-bar volume proxy (it still contains the drop-off from 24h earlier).
OI, price and funding have no such problem.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path("/home/hlbot/hlbot-runtime/live/state/funding_history.db")
ROUND_TRIP_COST = 0.000656
HORIZONS = [1, 4, 12, 24]
BOOT = 4000


def load() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "select ts_utc, coin, mark_px, oi_notional, day_volume_usd, "
        "premium_pct, annualized_pct from funding_snapshots "
        "where mark_px > 0 and oi_notional > 0",
        con,
    )
    con.close()
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True, format="mixed")
    df = df.sort_values(["coin", "ts"]).reset_index(drop=True)
    return df


def features(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("coin", sort=False)
    df["ret_1"] = g["mark_px"].pct_change()
    df["ret_1h"] = g["mark_px"].pct_change(4)
    df["d_oi_1h"] = g["oi_notional"].pct_change(4)
    df["vol_bar"] = g["day_volume_usd"].diff()
    df["vol_z"] = g["vol_bar"].transform(
        lambda s: (s - s.mean()) / (s.std() + 1e-9))
    # BTC beta per bar via cross-sectional regression on BTC returns.
    btc = df[df["coin"] == "BTC"].set_index("ts")["ret_1"].rename("btc")
    df = df.join(btc, on="ts")
    df["day"] = df["ts"].dt.floor("D")

    # Forward returns, demeaned per bar (market-neutral), cost deducted.
    for h in HORIZONS:
        fwd = g["mark_px"].shift(-4 * h) / df["mark_px"] - 1.0
        df[f"fwd_{h}"] = fwd
        df[f"xs_{h}"] = fwd - df.groupby("ts")[f"fwd_{h}"].transform("mean") \
            - ROUND_TRIP_COST
        df[f"abs_{h}"] = fwd.abs()
    return df


def boot_ci(d: pd.DataFrame, col: str, seed: int = 3) -> tuple[float, float, float]:
    """Day-block bootstrap CI for the mean, plus P(mean>0)."""
    grp = {k: g[col].dropna().to_numpy() for k, g in d.groupby("day")}
    grp = {k: v for k, v in grp.items() if len(v)}
    days = np.array(list(grp))
    if len(days) < 3:
        return np.nan, np.nan, np.nan
    rng = np.random.default_rng(seed)
    ms = np.empty(BOOT)
    for i in range(BOOT):
        pick = rng.choice(days, size=len(days), replace=True)
        ms[i] = np.concatenate([grp[k] for k in pick]).mean()
    return float(np.percentile(ms, 2.5)), float(np.percentile(ms, 97.5)), \
        float((ms > 0).mean())


def report(d: pd.DataFrame, mask: pd.Series, label: str,
           horizons=HORIZONS) -> None:
    sub = d[mask]
    print(f"\n  {label}")
    print(f"    n={len(sub)}  coins={sub['coin'].nunique()}  "
          f"days={sub['day'].nunique()}")
    if len(sub) < 30:
        print("    too few observations")
        return
    print(f"    {'h':>3} {'mean xs%':>9} {'t':>6} {'95% CI':>18} {'P>0':>6} "
          f"{'n':>5}")
    for h in horizons:
        col = f"xs_{h}"
        v = sub[col].dropna()
        if len(v) < 30:
            continue
        t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if v.std(ddof=1) > 0 else np.nan
        lo, hi, p = boot_ci(sub, col)
        print(f"    {h:>3} {v.mean()*100:>+8.3f}% {t:>+6.2f} "
              f"[{lo*100:>+6.3f},{hi*100:>+6.3f}] {p:>6.3f} {len(v):>5}")


def main() -> None:
    df = features(load())
    d = df.dropna(subset=["d_oi_1h", "ret_1", "xs_24"]).copy()
    print("=" * 80)
    print("THREE UNTESTED IDEAS, MARKET-NEUTRAL, DAY-BLOCK BOOTSTRAPPED")
    print("=" * 80)
    print(f"rows {len(d):,}  coins {d['coin'].nunique()}  days {d['day'].nunique()}")
    print(f"cost {ROUND_TRIP_COST*100:.4f}% deducted; xs = excess vs that bar's mean")

    fq = d["annualized_pct"].abs()
    f_thr = fq.quantile(0.99)
    oi_thr = d["d_oi_1h"].quantile(0.95)
    drop_thr = d["d_oi_1h"].quantile(0.01)
    vol_thr = d["vol_z"].quantile(0.90)

    print("\n" + "=" * 80)
    print("A) OI x FUNDING INTERACTION (squeeze fuel)")
    print("=" * 80)
    report(d, fq >= f_thr, "|funding| top 1% (baseline for this family)")
    report(d, (fq >= f_thr) & (d["d_oi_1h"] > 0),
           "|funding| top 1% AND OI rising -> crowded + building")
    report(d, (fq >= f_thr) & (d["d_oi_1h"] < 0),
           "|funding| top 1% AND OI falling -> crowded + unwinding")
    report(d, (fq >= f_thr) & (d["d_oi_1h"] > oi_thr),
           "|funding| top 1% AND OI surge (top 5%)")

    print("\n" + "=" * 80)
    print("B) RELATIVE STRENGTH vs BTC-BETA (residual, not raw)")
    print("=" * 80)
    # Residual return over the last hour after removing BTC's move.
    beta = d["ret_1h"] * d["btc"]  # placeholder; proper beta below
    # Rolling cross-sectional beta of each coin's 1h return on BTC's 1h return.
    cov = d.groupby("coin", sort=False).apply(
        lambda g: g["ret_1h"].cov(g["btc"]) / (g["btc"].var() + 1e-12),
        include_groups=False).rename("beta_btc")
    d = d.join(cov, on="coin")
    d["resid_1h"] = d["ret_1h"] - d["beta_btc"] * d["btc"]
    print(f"  beta_btc: median {d['beta_btc'].median():.2f}  "
          f"p10 {d['beta_btc'].quantile(.1):.2f}  p90 {d['beta_btc'].quantile(.9):.2f}")
    r_thr = d["resid_1h"].quantile([0.05, 0.95])
    report(d, d["resid_1h"] >= r_thr[0.95], "residual 1h strength top 5%")
    report(d, d["resid_1h"] <= r_thr[0.05], "residual 1h weakness bottom 5%")
    # Sign-aware: does strength continue (follow) or reverse (fade)?
    for h in HORIZONS:
        col = f"xs_{h}"
        strong = d[d["resid_1h"] >= r_thr[0.95]]
        v = strong[col].dropna()
        if len(v) > 30:
            print(f"    follow-strength {h:>2}h: {v.mean()*100:>+7.3f}% "
                  f"(negate to fade)")

    print("\n" + "=" * 80)
    print("C) FORCED-FLOW PROXY (OI dropping + hard move = liquidations)")
    print("=" * 80)
    report(d, (d["d_oi_1h"] <= drop_thr) & (d["ret_1h"].abs() >= 0.03),
           "OI drop bottom 1% + |1h move| >= 3% (liquidation-like)")
    report(d, (d["d_oi_1h"] <= drop_thr) & (d["ret_1h"] <= -0.03),
           "same, price DOWN (forced selling)")
    report(d, (d["d_oi_1h"] <= drop_thr) & (d["ret_1h"] >= 0.03),
           "same, price UP (forced buying / short squeeze)")
    report(d, (d["d_oi_1h"] <= drop_thr) & (d["vol_z"] >= vol_thr),
           "OI drop bottom 1% + volume spike (top 10%)")

    # Sign-aware fade test on the forced-flow cohort.
    ff = d[(d["d_oi_1h"] <= drop_thr) & (d["ret_1h"].abs() >= 0.03)].copy()
    if len(ff) >= 30:
        print(f"\n  Liquidation fade: bet AGAINST the forced move "
              f"(n={len(ff)})")
        for h in HORIZONS:
            v = (ff[f"xs_{h}"] * -np.sign(ff["ret_1h"])).dropna()
            if len(v) < 30:
                continue
            t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if v.std(ddof=1) > 0 else np.nan
            print(f"    fade {h:>2}h: {v.mean()*100:>+7.3f}%  t={t:>+5.2f}  n={len(v)}")

    print("\n" + "=" * 80)
    print("RANKING: EVERYTHING MEASURED TODAY, BY |t|, WITH BOOTSTRAP")
    print("=" * 80)
    print("  Only includes signals with n>=100 so the t is not pure noise.")
    cands = {
        "|funding| top 1%": fq >= f_thr,
        "|funding| top 1% (24h)": fq >= f_thr,
    }
    rows = []
    tests = [
        ("|funding| top 1%", fq >= f_thr),
        ("OI surge top 1%", d["d_oi_1h"] >= d["d_oi_1h"].quantile(0.99)),
        ("|funding| top1% & OI rising", (fq >= f_thr) & (d["d_oi_1h"] > 0)),
        ("forced flow (OI drop+move)", (d["d_oi_1h"] <= drop_thr)
         & (d["ret_1h"].abs() >= 0.03)),
        ("resid strength top 5%", d["resid_1h"] >= r_thr[0.95]),
    ]
    for name, m in tests:
        sub = d[m]
        for h in (4, 24):
            v = sub[f"xs_{h}"].dropna()
            if len(v) < 100:
                continue
            t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if v.std(ddof=1) > 0 else np.nan
            lo, hi, p = boot_ci(sub, f"xs_{h}")
            rows.append((abs(t), name, h, v.mean(), t, lo, hi, len(v)))
    rows.sort(reverse=True)
    print(f"  {'signal':<30} {'h':>3} {'mean%':>8} {'t':>7} {'95% CI':>17} "
          f"{'n':>6}")
    for _, name, h, mean, t, lo, hi, n in rows:
        print(f"  {name:<30} {h:>3} {mean*100:>+7.3f} % {t:>+6.2f} "
              f"[{lo*100:>+6.2f},{hi*100:>+6.2f}] {n:>6}")


if __name__ == "__main__":
    main()
