#!/usr/bin/env python
"""Significance test for the entry rule, respecting overlap.

entry_rule_expectancy.py reports t=+3.79 (OPEN, 72h) and t=+4.54 for the
|z|>=3 bucket. Those are not trustworthy: the signals are not independent.

Overlap comes from three places:
  1. Forward windows overlap (72h return at 4h cadence shares 68h of data).
  2. Signals repeat within the same pair across consecutive cycles.
  3. Pairs move together, so even different pairs are not independent.

The 979 signals cover only ~9 calendar days on 13 pairs. The effective sample
is closer to 9 days than 979 observations. This applies a day-block bootstrap:
resample whole days with replacement, keeping all signals in a day together.
That preserves within-day correlation and the overlap structure.

If the edge does not survive a 9-day resample, the honest conclusion is that
we cannot distinguish it from zero -- not that it is real.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from entry_rule_expectancy import (  # noqa: E402
    ROUND_TRIP_COST, build, load_prices, load_signals,
)


def add_returns(df: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    df = df.dropna(subset=["pa0", "pb0"]).copy()
    df["bet"] = -np.sign(df["z"])
    df["spread0"] = np.log(df["pa0"]) - df["beta"] * np.log(df["pb0"])
    df["day"] = df["ts"].dt.floor("D")
    for h in horizons:
        sp = np.log(df[f"pa_{h}"]) - df["beta"] * np.log(df[f"pb_{h}"])
        df[f"ret_{h}"] = 0.5 * df["bet"] * (sp - df["spread0"]) - ROUND_TRIP_COST
    return df


def block_bootstrap(sub: pd.DataFrame, col: str, n_iter: int = 5000,
                    seed: int = 7) -> dict:
    """Resample whole days with replacement; return CI for the mean."""
    days = sub["day"].unique()
    if len(days) < 3:
        return {"n_days": len(days), "mean": np.nan}
    rng = np.random.default_rng(seed)
    groups = {d: g[col].dropna().to_numpy() for d, g in sub.groupby("day")}
    groups = {d: v for d, v in groups.items() if len(v) > 0}
    days = np.array(list(groups))
    if len(days) < 3:
        return {"n_days": len(days), "mean": np.nan}
    means = np.empty(n_iter)
    for i in range(n_iter):
        pick = rng.choice(days, size=len(days), replace=True)
        vals = np.concatenate([groups[d] for d in pick])
        means[i] = vals.mean()
    obs = sub[col].dropna().mean()
    return {
        "n": int(sub[col].notna().sum()),
        "n_days": len(days),
        "mean": obs,
        "lo": float(np.percentile(means, 2.5)),
        "hi": float(np.percentile(means, 97.5)),
        "p_gt0": float((means > 0).mean()),
    }


def main() -> None:
    horizons = [2, 6, 12, 24, 48, 72]
    px, sig = load_prices(), load_signals()

    # Sign convention check against the journal's recorded direction.
    print("=" * 78)
    print("0) SIGN CONVENTION CHECK vs signals.csv")
    print("=" * 78)
    d = sig[sig["direction"].isin(["LONG_A_SHORT_B", "SHORT_A_LONG_B"])].copy()
    d["pred"] = np.where(-np.sign(d["z"]) > 0, "LONG_A_SHORT_B", "SHORT_A_LONG_B")
    agree = (d["pred"] == d["direction"]).mean()
    print(f"  -sign(z) reproduces the logged direction for "
          f"{100*agree:.1f}% of {len(d)} signal rows that carry a direction "
          f"(SKIP rows have direction NONE)")
    rsr = d[d["pair"] == "RSR/TAO"]
    if len(rsr):
        r = rsr.iloc[0]
        print(f"  RSR entry: z={r['z']:+.3f} -> predicted {r['pred']}, "
              f"journal {r['direction']}")

    lo, hi = px["ts"].min(), px["ts"].max()
    sig = sig[(sig["ts"] >= lo) & (sig["ts"] <= hi - pd.Timedelta(hours=72))]
    df = add_returns(build(sig, px), horizons)
    print(f"\n  RSR/TAO rows in test sample: "
          f"{int((df['pair']=='RSR/TAO').sum())} (0 => these are ex-RSR numbers)")

    opens = df[df["action"] == "OPEN"]
    print(f"  OPEN signals testable: {len(opens)} on {opens['pair'].nunique()} pairs "
          f"over {opens['day'].nunique()} days")

    print()
    print("=" * 78)
    print("1) NAIVE t-STAT vs DAY-BLOCK BOOTSTRAP (OPEN signals)")
    print("=" * 78)
    print("  The naive t assumes every signal is independent. The bootstrap")
    print("  resamples whole days, which is the real unit of independence here.")
    print()
    print(f"  {'h':>4} {'n':>5} {'mean%':>8} {'naive t':>8} "
          f"{'boot 95% CI':>20} {'P(>0)':>7} {'days':>5}")
    for h in horizons:
        col = f"ret_{h}"
        s = opens[col].dropna()
        if len(s) < 10:
            continue
        t = s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if s.std(ddof=1) > 0 else np.nan
        b = block_bootstrap(opens, col)
        ci = f"[{b['lo']*100:+.3f}, {b['hi']*100:+.3f}]"
        print(f"  {h:>4} {len(s):>5} {s.mean()*100:>8.3f} {t:>8.2f} "
              f"{ci:>20} {b['p_gt0']:>7.3f} {b['n_days']:>5}")

    print()
    print("=" * 78)
    print("2) THE |z|>=3 'BUCKET' -- REAL OR ONE CLUSTER? (24h)")
    print("=" * 78)
    h = 24
    d24 = df.dropna(subset=[f"ret_{h}"]).copy()
    d24["absz"] = d24["z"].abs()
    big = d24[d24["absz"] >= 3.0]
    print(f"  |z|>=3 rows: {len(big)} across {big['pair'].nunique()} pairs, "
          f"{big['day'].nunique()} days")
    if len(big):
        print(f"  pairs: {dict(big['pair'].value_counts())}")
        print(f"  days : {dict(big['day'].dt.strftime('%m-%d').value_counts())}")
        b = block_bootstrap(big, f"ret_{h}")
        print(f"\n  mean {big[f'ret_{h}'].mean()*100:+.3f}%  "
              f"boot 95% CI [{b['lo']*100:+.3f}, {b['hi']*100:+.3f}]  "
              f"P(>0)={b['p_gt0']:.3f}")
        top = big.nlargest(3, f"ret_{h}")[["ts", "pair", "z", f"ret_{h}"]]
        print("  largest contributors:")
        for _, r in top.iterrows():
            print(f"    {str(r['ts'])[:16]}  {r['pair']:<14} z={r['z']:+.2f}  "
                  f"{r[f'ret_{h}']*100:+.2f}%")
        drop = big.drop(top.index)
        print(f"  excluding those 3: mean {drop[f'ret_{h}'].mean()*100:+.3f}% "
              f"(n={len(drop)})")

    print()
    print("=" * 78)
    print("3) NON-OVERLAPPING CHECK: one signal per pair per 72h")
    print("=" * 78)
    o = opens.dropna(subset=["ret_72"]).sort_values("ts")
    o = o[o.groupby("pair")["ts"].diff().dt.total_seconds().fillna(1e9) >= 72 * 3600]
    print(f"  independent-ish: {len(o)} signals on {o['pair'].nunique()} pairs")
    if len(o) >= 5:
        s = o["ret_72"]
        t = s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if s.std(ddof=1) > 0 else np.nan
        print(f"  72h mean {s.mean()*100:+.3f}%  naive t={t:+.2f}  "
              f"n={len(s)}")
        b = block_bootstrap(o, "ret_72")
        print(f"  boot 95% CI [{b['lo']*100:+.3f}, {b['hi']*100:+.3f}]  "
              f"P(>0)={b['p_gt0']:.3f}")
    else:
        print("  too few non-overlapping observations to test")


if __name__ == "__main__":
    main()
