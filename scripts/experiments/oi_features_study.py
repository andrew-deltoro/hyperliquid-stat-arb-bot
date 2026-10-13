#!/usr/bin/env python3
"""
Open-interest features from data we ALREADY have.

state/funding_history.db has held 15-min snapshots of openInterest, premium,
mark/oracle, funding and day volume for all 234 coins since 2026-10-02
(funding_collector.py). That window contains the RSR pump of 2026-10-12
04:00 UTC, so the first question -- did OI lead the move? -- is answerable
today, with no new collection.

Two parts:
  1. Event window: 15-min OI / funding / premium / mark around the RSR pump.
  2. Cross-sectional test: does an extreme dOI z-score predict forward
     return, across all coins, non-overlapping?

Run: .venv/bin/python scripts/experiments/oi_features_study.py
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

PDT = timezone(timedelta(hours=-7))
DB = Path("/home/hlbot/hyperliquid-stat-arb-bot/state/funding_history.db")
EVENT = datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)
FWD_BARS = 4          # 4 x 15min = 1h forward
NONOVL_BARS = 4       # 1h between samples


def load(conn):
    rows = conn.execute(
        "SELECT ts_utc, coin, mark_px, oracle_px, premium_pct, oi_notional, "
        "funding_hourly, day_volume_usd FROM funding_snapshots "
        "ORDER BY coin, ts_utc"
    ).fetchall()
    by_coin: dict[str, list] = {}
    for r in rows:
        ts = datetime.fromisoformat(r[0])
        by_coin.setdefault(r[1], []).append((ts, *r[2:]))
    return by_coin


def part1_event(by_coin):
    print("=" * 100)
    print("PART 1: RSR around the pump (times in PDT)")
    print("=" * 100)
    for coin in ("RSR", "TAO"):
        seq = by_coin.get(coin, [])
        if not seq:
            print(f"{coin}: no rows")
            continue
        sel = [(ts, mk, orc, prem, oi, fr, dv)
               for (ts, mk, orc, prem, oi, fr, dv) in seq
               if abs((ts - EVENT).total_seconds()) <= 6 * 3600]
        if not sel:
            print(f"{coin}: no rows near event")
            continue
        print(f"\n{coin}:")
        print(f"{'time PDT':>16}{'mark':>12}{'prem%':>9}{'OI$':>16}"
              f"{'dOI$':>14}{'dOI%':>8}{'fund%/h':>9}{'dPx%':>8}")
        prev_oi = sel[0][4]
        prev_mk = sel[0][1]
        for ts, mk, orc, prem, oi, fr, dv in sel:
            doi = oi - prev_oi
            doip = (doi / prev_oi * 100) if prev_oi else 0.0
            dpx = ((mk / prev_mk - 1) * 100) if prev_mk else 0.0
            mark = f"{ts.astimezone(PDT):%m-%d %H:%M}"
            print(f"{mark:>16}{mk:>12.6f}{prem:>9.3f}{oi:>16,.0f}"
                  f"{doi:>14,.0f}{doip:>8.2f}{fr*100:>9.4f}{dpx:>8.2f}")
            prev_oi, prev_mk = oi, mk


def part2_cross_section(by_coin):
    print("\n" + "=" * 100)
    print("PART 2: does extreme dOI predict forward return? (all coins, non-overlapping 1h)")
    print("=" * 100)
    obs = []
    for coin, seq in by_coin.items():
        if len(seq) < 200:
            continue
        ts = [s[0] for s in seq]
        mk = np.array([s[1] for s in seq], float)
        oi = np.array([s[4] for s in seq], float)
        i = 8
        while i < len(seq) - FWD_BARS:
            w = oi[i - 8:i]
            if len(w) < 8 or w.std() == 0 or mk[i] <= 0 or mk[i - 1] <= 0:
                i += NONOVL_BARS
                continue
            doi = oi[i] - oi[i - 1]
            z = doi / (np.diff(w).std() + 1e-9)
            dpx = mk[i] / mk[i - 1] - 1.0          # contemporaneous bar move
            fwd = mk[i + FWD_BARS] / mk[i] - 1.0
            obs.append((float(z), float(dpx), float(fwd), coin))
            i += NONOVL_BARS

    if not obs:
        print("no observations")
        return
    z = np.array([o[0] for o in obs])
    dpx = np.array([o[1] for o in obs])
    f = np.array([o[2] for o in obs])
    print(f"observations: {len(obs):,}   base mean fwd1h = {f.mean():+.4%}   "
          f"sd = {f.std():.3%}")

    print(f"\n{'dOI z bucket':>14}{'n':>8}{'mean fwd1h':>12}{'P(up)':>8}{'t':>8}")
    edges = [-99, -3, -2, -1, 1, 2, 3, 99]
    labels = ["<-3", "-3..-2", "-2..-1", "-1..1", "1..2", "2..3", ">3"]
    for lo, hi, lab in zip(edges[:-1], edges[1:], labels):
        sel = f[(z > lo) & (z <= hi)]
        if len(sel) < 20:
            continue
        t = sel.mean() / (sel.std(ddof=1) / math.sqrt(len(sel))) if sel.std() else float("nan")
        print(f"{lab:>14}{len(sel):>8}{sel.mean():>+12.4%}{(sel>0).mean():>8.1%}{t:>8.2f}")

    # ---- CONFOUND CHECK: is dOI just momentum? ----
    print("\nCONFOUND CHECK: forward return by (bar price move) x (dOI bucket)")
    print("If dOI adds nothing once the bar's own price move is controlled, the")
    print("signal above is just short-term momentum, not new information.\n")
    px_edges = [(-99, -0.005, "px dn"), (-0.005, 0.005, "px flat"),
                (0.005, 99, "px up")]
    oi_edges = [(-99, 0, "dOI<0"), (0, 2, "dOI 0..2"), (2, 99, "dOI>2")]
    print(f"{'':>12}" + "".join(f"{ol:>18}" for _, _, ol in oi_edges))
    for plo, phi, plab in px_edges:
        cells = []
        for olo, ohi, olab in oi_edges:
            sel = f[(dpx > plo) & (dpx <= phi) & (z > olo) & (z <= ohi)]
            if len(sel) < 20:
                cells.append("   --   ")
                continue
            cells.append(f"{sel.mean():+.3%}(n={len(sel):<4})")
        print(f"{plab:>12}" + "".join(f"{c:>18}" for c in cells))

    # crude regression: fwd ~ a + b*dpx + c*z  (does c survive?)
    X = np.column_stack([np.ones_like(f), dpx, z])
    beta, *_ = np.linalg.lstsq(X, f, rcond=None)
    resid = f - X @ beta
    s2 = (resid @ resid) / (len(f) - 3)
    cov = s2 * np.linalg.inv(X.T @ X)
    se = np.sqrt(np.diag(cov))
    print(f"\nOLS fwd1h ~ 1 + dpx + dOIz")
    for name, b, s in zip(("const", "dPx(bar)", "dOI z"), beta, se):
        print(f"  {name:>10}: {b:+.5f}  se={s:.5f}  t={b/s:+.2f}")


def main():
    if not DB.exists():
        print(f"missing {DB}")
        return
    conn = sqlite3.connect(DB)
    by_coin = load(conn)
    print(f"coins in db: {len(by_coin)}")
    part1_event(by_coin)
    part2_cross_section(by_coin)


if __name__ == "__main__":
    main()
