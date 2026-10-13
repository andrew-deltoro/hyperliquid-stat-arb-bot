#!/usr/bin/env python
"""Does the bot's entry rule (|z| >= z_entry_min, bet on reversion) pay?

Direction is -sign(z): z>0 => SHORT_A_LONG_B, z<0 => LONG_A_SHORT_B. Either way
the bot bets the spread moves back toward zero. This tests that bet on every
signal it actually generated, using the legs' prices from funding_history.db.

P&L identity (journal._leg_pnls):
    leg_b = leg_a * beta, notional = 2*leg_a
    LONG_A_SHORT_B : pnl = leg_a*(ra - beta*rb)
    SHORT_A_LONG_B : pnl = leg_a*(-ra + beta*rb)
so pnl/notional ~ 0.5 * direction * d(ln A - beta ln B) = 0.5 * signed spread move.

Rounding note: paths here use 15-min mark prices, so this is an upper bound on
the entry edge — it assumes we entered exactly at the logged signal price.
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path("/home/hlbot/hlbot-runtime/live/state/funding_history.db")
SIGNALS = Path("/home/hlbot/hlbot-runtime/live/reports/signals.csv")
ROUND_TRIP_COST = 0.000656
HORIZONS_H = [2, 6, 12, 24, 48, 72]


def load_prices() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "select ts_utc, coin, mark_px from funding_snapshots where mark_px > 0",
        con,
    )
    con.close()
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True, format="mixed")
    return df[["ts", "coin", "mark_px"]].sort_values(["coin", "ts"])


def load_signals() -> pd.DataFrame:
    with open(SIGNALS) as f:
        rows = list(csv.DictReader(f))
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["timestamp_iso"], utc=True, format="mixed")
    for c in ("z", "beta"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["z", "beta"])
    df["a"] = df["pair"].str.split("/").str[0].str.strip()
    df["b"] = df["pair"].str.split("/").str[1].str.strip()
    return df


def asof(px: pd.DataFrame, coin: str, times: pd.Series) -> pd.Series:
    """Nearest mark price at or before each timestamp, for one coin."""
    s = px[px["coin"] == coin].set_index("ts")["mark_px"].sort_index()
    if s.empty:
        return pd.Series(np.nan, index=times.index)
    idx = s.index.searchsorted(times, side="right") - 1
    out = np.where(idx >= 0, s.to_numpy()[np.clip(idx, 0, len(s) - 1)], np.nan)
    return pd.Series(out, index=times.index)


def build(sig: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    px = px.copy()
    px["ts"] = px["ts"].dt.round("15min")
    sig = sig.copy()
    sig["ts"] = sig["ts"].dt.round("15min")

    spells = []
    for h in HORIZONS_H:
        fwd_t = sig[["ts"]].copy()
        fwd_t["ts"] = fwd_t["ts"] + pd.Timedelta(hours=h)
        s = sig[["ts", "a", "b", "z", "beta", "action"]].copy()
        s["pa0"] = asof(px, "PROBE", sig["ts"])  # placeholder, replaced per coin
        spells.append((h, s))

    # Per-coin lookup is expensive to do with a generic helper, so pivot once.
    wide = px.pivot_table(index="ts", columns="coin", values="mark_px",
                          aggfunc="last").sort_index()

    out = sig.copy()
    for col, coin in (("pa0", "a"), ("pb0", "b")):
        idx = wide.index.searchsorted(out["ts"], side="right") - 1
        keys = out[coin].to_numpy()
        vals = np.full(len(out), np.nan)
        for i, (ix, c) in enumerate(zip(idx, keys)):
            if ix >= 0 and c in wide.columns:
                v = wide.iloc[ix][c]
                if pd.notna(v):
                    vals[i] = float(v)
        out[col] = vals

    for h in HORIZONS_H:
        fts = out["ts"] + pd.Timedelta(hours=h)
        idx = wide.index.searchsorted(fts, side="right") - 1
        pa = np.full(len(out), np.nan)
        pb = np.full(len(out), np.nan)
        for i, (ix, ca, cb) in enumerate(zip(idx, out["a"].to_numpy(),
                                             out["b"].to_numpy())):
            if ix < 0 or ix >= len(wide):
                continue
            row = wide.iloc[ix]
            if ca in wide.columns and pd.notna(row[ca]):
                pa[i] = float(row[ca])
            if cb in wide.columns and pd.notna(row[cb]):
                pb[i] = float(row[cb])
        out[f"pa_{h}"] = pa
        out[f"pb_{h}"] = pb
    return out


def evaluate(df: pd.DataFrame) -> None:
    ok = df.dropna(subset=["pa0", "pb0"]).copy()
    print(f"signals with usable prices: {len(ok)} of {len(df)}")
    print(f"  span: {ok['ts'].min()} -> {ok['ts'].max()}")
    print(f"  pairs: {ok['pair'].nunique()}   action mix: "
          f"{dict(ok['action'].value_counts())}")
    print(f"  round-trip cost assumed: {ROUND_TRIP_COST*100:.4f}% of notional")

    # Bet on reversion: pnl sign is -sign(z).
    ok["bet"] = -np.sign(ok["z"])
    ok["spread0"] = np.log(ok["pa0"]) - ok["beta"] * np.log(ok["pb0"])

    print()
    print("=" * 78)
    print("GROSS SIGNED SPREAD MOVE (positive = the reversion bet paid)")
    print("=" * 78)
    hdr = f"  {'horizon':<9}" + "".join(f"{h:>9}h" for h in HORIZONS_H)
    print(hdr)
    for label, mask in (("ALL signals", ok["action"].notna()),
                        ("OPEN only", ok["action"] == "OPEN")):
        sub = ok[mask]
        vals = []
        for h in HORIZONS_H:
            d = sub.dropna(subset=[f"pa_{h}", f"pb_{h}"])
            if d.empty:
                vals.append(np.nan)
                continue
            sp = np.log(d[f"pa_{h}"]) - d["beta"] * np.log(d[f"pb_{h}"])
            move = d["bet"] * (sp - d["spread0"])
            vals.append(move.mean() * 100)
        print(f"  {label:<9}" + "".join(f"{v:>9.3f}" for v in vals) + "  (% of log-spread)")

    print()
    print("=" * 78)
    print("EXPECTANCY PER TRADE (pnl% of notional), NET of measured cost")
    print("=" * 78)
    print("  pnl% ~ 0.5 x signed spread move - cost")
    for label, mask in (("ALL signals", ok["action"].notna()),
                        ("OPEN only", ok["action"] == "OPEN")):
        sub = ok[mask]
        print(f"\n  {label} (n={len(sub)})")
        print(f"    {'horizon':<8}" + "".join(f"{h:>8}h" for h in HORIZONS_H))
        gross = []
        net = []
        tt = []
        for h in HORIZONS_H:
            d = sub.dropna(subset=[f"pa_{h}", f"pb_{h}"])
            sp = np.log(d[f"pa_{h}"]) - d["beta"] * np.log(d[f"pb_{h}"])
            ret = 0.5 * d["bet"] * (sp - d["spread0"])
            gross.append(ret.mean() * 100)
            net.append((ret.mean() - ROUND_TRIP_COST) * 100)
            tt.append(ret.mean() / (ret.std(ddof=1) / np.sqrt(len(ret)))
                      if len(ret) > 2 and ret.std(ddof=1) > 0 else np.nan)
        print(f"    {'gross':<8}" + "".join(f"{v:>8.3f}" for v in gross))
        print(f"    {'net':<8}" + "".join(f"{v:>8.3f}" for v in net))
        print(f"    {'t-stat':<8}" + "".join(f"{v:>8.2f}" for v in tt))


def by_z_bucket(df: pd.DataFrame) -> None:
    print()
    print("=" * 78)
    print("DOES A BIGGER |z| ENTRY DO BETTER OR WORSE? (the tail question)")
    print("=" * 78)
    ok = df.dropna(subset=["pa0", "pb0"]).copy()
    ok["bet"] = -np.sign(ok["z"])
    ok["spread0"] = np.log(ok["pa0"]) - ok["beta"] * np.log(ok["pb0"])
    ok["absz"] = ok["z"].abs()
    buckets = [(0.9, 1.2), (1.2, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 99.0)]

    h = 24
    d = ok.dropna(subset=[f"pa_{h}", f"pb_{h}"])
    sp = np.log(d[f"pa_{h}"]) - d["beta"] * np.log(d[f"pb_{h}"])
    d = d.assign(ret=0.5 * d["bet"] * (sp - d["spread0"]))
    print(f"  holding {h}h, net of cost")
    print(f"  {'|z| bucket':<14} {'n':>6} {'gross%':>9} {'net%':>9} {'t':>7} "
          f"{'win%':>7}")
    for lo, hi in buckets:
        s = d[(d["absz"] >= lo) & (d["absz"] < hi)]
        if len(s) < 5:
            print(f"  [{lo},{hi})".ljust(14) + f"{len(s):>6}  (too few)")
            continue
        t = s["ret"].mean() / (s["ret"].std(ddof=1) / np.sqrt(len(s))) \
            if s["ret"].std(ddof=1) > 0 else np.nan
        print(f"  [{lo},{hi})".ljust(14)
              + f"{len(s):>6} {s['ret'].mean()*100:>9.3f} "
                f"{(s['ret'].mean()-ROUND_TRIP_COST)*100:>9.3f} {t:>7.2f} "
                f"{100*(s['ret']>0).mean():>7.1f}")

    print("\n  Same, but excluding the RSR/TAO pair entirely:")
    d2 = d[d["pair"] != "RSR/TAO"]
    for lo, hi in buckets:
        s = d2[(d2["absz"] >= lo) & (d2["absz"] < hi)]
        if len(s) < 5:
            continue
        t = s["ret"].mean() / (s["ret"].std(ddof=1) / np.sqrt(len(s))) \
            if s["ret"].std(ddof=1) > 0 else np.nan
        print(f"  [{lo},{hi})".ljust(14)
              + f"{len(s):>6} {s['ret'].mean()*100:>9.3f} "
                f"{(s['ret'].mean()-ROUND_TRIP_COST)*100:>9.3f} {t:>7.2f} "
                f"{100*(s['ret']>0).mean():>7.1f}")


def main() -> None:
    if not DB.exists() or not SIGNALS.exists():
        print("missing data")
        sys.exit(1)
    px = load_prices()
    sig = load_signals()
    print("=" * 78)
    print("TESTING THE BOT'S OWN ENTRY RULE: bet on spread reversion")
    print("=" * 78)
    print(f"signals logged : {len(sig)}")
    print(f"price coverage : {px['coin'].nunique()} coins, "
          f"{px['ts'].min()} -> {px['ts'].max()}")
    # Only signals inside the price window are testable.
    lo, hi = px["ts"].min(), px["ts"].max()
    sig = sig[(sig["ts"] >= lo) & (sig["ts"] <= hi - pd.Timedelta(hours=72))]
    print(f"testable       : {len(sig)} (window-limited)\n")
    built = build(sig, px)
    evaluate(built)
    by_z_bucket(built)


if __name__ == "__main__":
    main()
