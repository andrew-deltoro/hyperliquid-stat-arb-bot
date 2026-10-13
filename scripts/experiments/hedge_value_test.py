#!/usr/bin/env python
"""Should we drop the short leg? And can a hedged pair ever reach the tail?

The proposal -- stop opening the expensive short because it loses -- is
tempting but has a trap. The naked long carries market beta, so it looks bad in
a down window and good in an up window for reasons that have nothing to do with
the hedge. Left untreated, that comparison measures market direction.

So this separates three things:
  1. Does the cheap leg have IDIOSYNCRATIC alpha (rA minus market)? If not,
     going naked converts a market-neutral spread trade into an unhedged
     directional bet with no edge claim.
  2. Does the SPREAD revert (rA - beta*rB)? That is the only thing the entry
     signal actually predicts.
  3. Is the hedge RATIO wrong? The cointegration beta is not the
     variance-minimizing hedge. If the used beta exceeds h* = cov/var, we are
     over-hedged, and that is fixable without dropping the hedge.

Also answers directly: can a hedged pair reach +20%?
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

HORIZONS = [4, 12, 24, 48, 72, 168]


def main() -> None:
    px, sig = load_prices(), load_signals()
    lo, hi = px["ts"].min(), px["ts"].max()
    sig = sig[(sig["ts"] >= lo) & (sig["ts"] <= hi - pd.Timedelta(hours=168))]
    base = build(sig, px).dropna(subset=["pa0", "pb0"]).copy()
    base["bet"] = -np.sign(base["z"])

    # Market proxy and forward prices: cross-sectional mean forward return per
    # horizon, computed here because build() only covers its own horizon list.
    wide = px.pivot_table(index="ts", columns="coin", values="mark_px",
                          aggfunc="last").sort_index()
    idx = wide.index.searchsorted(base["ts"], side="right") - 1
    for h in HORIZONS:
        fwd = wide.shift(-4 * h) / wide - 1.0
        mkt_h = fwd.mean(axis=1)
        base[f"mkt_{h}"] = np.where(
            idx >= 0, mkt_h.to_numpy()[np.clip(idx, 0, len(mkt_h) - 1)], np.nan)
        # Per-coin forward price at ts + h, looked up from the same grid.
        fts = base["ts"] + pd.Timedelta(hours=h)
        j = wide.index.searchsorted(fts, side="right") - 1
        pa = np.full(len(base), np.nan)
        pb = np.full(len(base), np.nan)
        for i, (jj, ca, cb) in enumerate(zip(j, base["a"].to_numpy(),
                                             base["b"].to_numpy())):
            if jj < 0 or jj >= len(wide):
                continue
            row = wide.iloc[jj]
            if ca in wide.columns and pd.notna(row[ca]):
                pa[i] = float(row[ca])
            if cb in wide.columns and pd.notna(row[cb]):
                pb[i] = float(row[cb])
        base[f"pa_{h}"] = pa
        base[f"pb_{h}"] = pb
        base[f"ra_{h}"] = base[f"pa_{h}"] / base["pa0"] - 1.0
        base[f"rb_{h}"] = base[f"pb_{h}"] / base["pb0"] - 1.0

    o = base[base["action"] == "OPEN"].copy()
    if o.empty:
        print("no OPEN signals have a full 168h forward window at this point")
    print("=" * 78)
    print(f"OPEN SIGNALS: {len(o)}  pairs: {o['pair'].nunique()}  "
          f"window {o['ts'].min()} -> {o['ts'].max()}")
    print("=" * 78)

    # ---- 1. Is the cheap leg's profit idiosyncratic, or just market beta? ----
    print("\n1) IS THE CHEAP LEG'S RETURN ALPHA, OR MARKET BETA?")
    print("   If the 'profit' is just market direction, naked long is a")
    print("   directional bet with no edge, not a purer version of the trade.")
    print(f"\n   {'h':>5} {'n':>5} {'rA mean':>9} {'market':>9} {'idio rA':>9} "
          f"{'t(idio)':>9} {'%days mkt>0':>12}")
    for h in HORIZONS:
        d = o.dropna(subset=[f"ra_{h}", f"mkt_{h}"])
        if len(d) < 20:
            continue
        idio = d[f"ra_{h}"] - d[f"mkt_{h}"]
        t = idio.mean() / (idio.std(ddof=1) / np.sqrt(len(idio)))
        pos_days = (d.groupby(d["ts"].dt.floor("D"))[f"mkt_{h}"].first() > 0).mean()
        print(f"   {h:>5} {len(d):>5} {d[f'ra_{h}'].mean()*100:>+8.3f}% "
              f"{d[f'mkt_{h}'].mean()*100:>+8.3f}% {idio.mean()*100:>+8.3f}% "
              f"{t:>+9.2f} {100*pos_days:>11.0f}%")

    # ---- 2. Does the spread actually revert? ----
    print("\n2) DOES THE SPREAD REVERT? (this is what the entry signal claims)")
    print(f"\n   {'h':>5} {'n':>5} {'spread ret':>11} {'t':>8} {'win%':>7}")
    for h in HORIZONS:
        d = o.dropna(subset=[f"ra_{h}", f"rb_{h}"])
        if len(d) < 20:
            continue
        sp = d["bet"] * (d[f"ra_{h}"] - d["beta"] * d[f"rb_{h}"])
        t = sp.mean() / (sp.std(ddof=1) / np.sqrt(len(sp))) if sp.std(ddof=1) > 0 else np.nan
        print(f"   {h:>5} {len(d):>5} {sp.mean()*100:>+10.3f}% {t:>+8.2f} "
              f"{100*(sp>0).mean():>6.1f}%")

    # ---- 3. Compare hedged vs naked, market-neutral ----
    print("\n3) HEDGED vs NAKED, BOTH WITH MARKET REMOVED")
    print("   hedged = 0.5*(rA - beta*rB);  naked_idio = 0.5*(rA - market)")
    print("   'naked (raw)' includes beta, so it is not a strategy comparison.")
    print(f"\n   {'h':>5} {'hedged':>9} {'naked_idio':>11} {'naked_raw':>10} "
          f"{'hedge sd':>9} {'naked sd':>9} {'hedge SR':>9} {'naked SR':>9}")
    for h in HORIZONS:
        d = o.dropna(subset=[f"ra_{h}", f"rb_{h}", f"mkt_{h}"])
        if len(d) < 20:
            continue
        hed = 0.5 * d["bet"] * (d[f"ra_{h}"] - d["beta"] * d[f"rb_{h}"]) \
            - ROUND_TRIP_COST
        nk_i = 0.5 * (d[f"ra_{h}"] - d[f"mkt_{h}"]) - ROUND_TRIP_COST * 0.5
        nk_r = 0.5 * d[f"ra_{h}"] - ROUND_TRIP_COST * 0.5
        sr_h = hed.mean() / hed.std(ddof=1) if hed.std(ddof=1) > 0 else np.nan
        sr_n = nk_i.mean() / nk_i.std(ddof=1) if nk_i.std(ddof=1) > 0 else np.nan
        print(f"   {h:>5} {hed.mean()*100:>+8.3f}% {nk_i.mean()*100:>+10.3f}% "
              f"{nk_r.mean()*100:>+9.3f}% {hed.std()*100:>8.2f}% "
              f"{nk_i.std()*100:>8.2f}% {sr_h:>+9.3f} {sr_n:>+9.3f}")

    # ---- 4. Is the hedge RATIO wrong? ----
    print("\n4) IS THE HEDGE RATIO WRONG? (a fixable alternative to dropping it)")
    print("   The used beta is a cointegration coefficient. The")
    print("   variance-minimizing hedge is h* = cov(rA,rB)/var(rB). If used > h*,")
    print("   we are over-hedged and the fix is the ratio, not removal.")
    print(f"\n   {'h':>5} {'used beta':>10} {'h* (varmin)':>12} {'h*/used':>9} "
          f"{'corr(rA,rB)':>12}")
    for h in HORIZONS:
        d = o.dropna(subset=[f"ra_{h}", f"rb_{h}"])
        if len(d) < 20:
            continue
        cov = np.cov(d[f"ra_{h}"], d[f"rb_{h}"])
        hstar = cov[0, 1] / cov[1, 1] if cov[1, 1] > 0 else np.nan
        used = d["beta"].mean()
        corr = d[f"ra_{h}"].corr(d[f"rb_{h}"])
        print(f"   {h:>5} {used:>10.4f} {hstar:>12.4f} "
              f"{hstar/used if used else np.nan:>9.3f} {corr:>12.3f}")

    # ---- 5. Can a hedged pair reach +20%? ----
    print("\n5) CAN A HEDGED PAIR REACH THE +20% TAIL?")
    print("   RSR reached +18.93% hedged over 20h, so this is about horizon,")
    print("   not about the hedge blocking it.")
    print(f"\n   {'h':>5} {'n':>5} {'mean':>9} {'sd':>7} {'best':>8} {'worst':>8} "
          f"{'P>5%':>7} {'P>10%':>7} {'P>20%':>7} {'P<-10%':>8}")
    for h in HORIZONS:
        d = o.dropna(subset=[f"ra_{h}", f"rb_{h}"])
        if len(d) < 20:
            continue
        hed = 0.5 * d["bet"] * (d[f"ra_{h}"] - d["beta"] * d[f"rb_{h}"]) \
            - ROUND_TRIP_COST
        print(f"   {h:>5} {len(d):>5} {hed.mean()*100:>+8.3f}% "
              f"{hed.std()*100:>6.2f}% {hed.max()*100:>+7.2f}% "
              f"{hed.min()*100:>+7.2f}% {100*(hed>0.05).mean():>6.1f}% "
              f"{100*(hed>0.10).mean():>6.1f}% {100*(hed>0.20).mean():>6.1f}% "
              f"{100*(hed<-0.10).mean():>7.1f}%")

    # ---- 6. The naked alternative's tail ----
    print("\n6) NAKED (idiosyncratic) TAIL, for contrast:")
    print(f"\n   {'h':>5} {'mean':>9} {'sd':>7} {'best':>8} {'worst':>8} "
          f"{'P>10%':>7} {'P>20%':>7} {'P<-10%':>8}")
    for h in HORIZONS:
        d = o.dropna(subset=[f"ra_{h}", f"mkt_{h}"])
        if len(d) < 20:
            continue
        nk = 0.5 * (d[f"ra_{h}"] - d[f"mkt_{h}"]) - ROUND_TRIP_COST * 0.5
        print(f"   {h:>5} {len(d):>5} {nk.mean()*100:>+8.3f}% "
              f"{nk.std()*100:>6.2f}% {nk.max()*100:>+7.2f}% "
              f"{nk.min()*100:>+7.2f}% {100*(nk>0.10).mean():>6.1f}% "
              f"{100*(nk>0.20).mean():>6.1f}% {100*(nk<-0.10).mean():>7.1f}%")


if __name__ == "__main__":
    main()
