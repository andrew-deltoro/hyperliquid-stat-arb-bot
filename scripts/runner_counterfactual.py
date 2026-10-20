#!/usr/bin/env python3
"""Prototype: a "runner" exit that keeps holding after the cross level is hit.

Motivation (from reports/CROSSING_TENDENCY_REVIEW.md)
-----------------------------------------------------
The cross-exit payload is a handful of large CONTINUATIONS: the spread crosses
the centre and keeps going. The cross exit banks at -cross_frac * entry_spread
and gives back everything beyond that. A runner would instead, once the cross
level is reached, trail the extreme by `runner_frac` of the distance travelled
beyond the cross level -- i.e.

    lock = extreme - runner_frac * (extreme - cross_level)      # long_a
    lock = extreme + runner_frac * (cross_level - extreme)      # short_a

  runner_frac = 0   -> exit on the first tick back (≈ today's cross exit)
  runner_frac = 1   -> hold until it retraces all the way to the cross level
  runner_frac = inf -> never trail; only time_stop / divergence stop ends it

This replays the SAME 29 real closed trades as cross_only_counterfactual.py,
on the SAME 4h decision grid, using the SAME production cost model, and reports
per-trade and aggregate net P&L for a sweep of runner_frac.

Run:  .venv/bin/python scripts/runner_counterfactual.py
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from cross_only_counterfactual import (  # noqa: E402
    CFG, CROSS_FRAC, POSITIONS, iso, next_cycles, price_at, price_table,
)
from src.hyperliquid_bot.phase3.journal import compute_pnl  # noqa: E402

RUNNERS = [0.0, 0.25, 0.5, 0.75, 1.0, float("inf")]


def replay_runner(pos, tables, runner_frac):
    """Cross-then-trail exit. Returns (reason, exit_a, exit_b, exit_ts)."""
    long_a = pos["direction"] == "LONG_A_SHORT_B"
    entry_spread = float(pos["entry_spread"])
    cross_level = -CROSS_FRAC * entry_spread
    stop_spread = float(pos["stop_spread"])
    alpha = float(pos["entry_alpha"])
    mean_raw = float(pos["entry_mean_raw"])
    beta = float(pos["beta_entry"])

    entry = iso(pos["entry_time_iso"])
    tstop = iso(pos.get("time_stop_iso")) or entry
    now = datetime.now(timezone.utc)
    until = min(tstop, now)

    ta = tables.get(pos["coin_a"], [])
    tb = tables.get(pos["coin_b"], [])
    if not ta or not tb:
        return ("no_price_data", None, None, None)

    crossed = False
    extreme = None            # best cc seen since the cross
    for ts in next_cycles(entry, until):
        tsms = int(ts.timestamp() * 1000)
        pa = price_at(ta, tsms)
        pb = price_at(tb, tsms)
        if pa is None or pb is None:
            continue
        try:
            raw = math.log(pa) - alpha - beta * math.log(pb)
        except ValueError:
            continue
        cc = raw - mean_raw

        # divergence stop is always live (same precedence as live code)
        if long_a and cc <= stop_spread:
            return ("divergence_stop", pa, pb, ts)
        if (not long_a) and cc >= stop_spread:
            return ("divergence_stop", pa, pb, ts)

        # has the cross level been reached yet?
        if not crossed:
            if (long_a and cc >= cross_level) or ((not long_a) and cc <= cross_level):
                crossed = True
                extreme = cc
                if runner_frac == 0.0:
                    return ("cross", pa, pb, ts)
        else:
            # update the extreme in the favourable direction
            extreme = max(extreme, cc) if long_a else min(extreme, cc)
            if runner_frac != float("inf"):
                if long_a:
                    lock = extreme - runner_frac * (extreme - cross_level)
                    if cc <= lock and extreme > cross_level:
                        return ("trail", pa, pb, ts)
                else:
                    lock = extreme + runner_frac * (cross_level - extreme)
                    if cc >= lock and extreme < cross_level:
                        return ("trail", pa, pb, ts)

        if ts >= tstop:
            return ("time_stop", pa, pb, ts)

    if until >= tstop and tstop < now:
        tsms = int(tstop.timestamp() * 1000)
        return ("time_stop", price_at(ta, tsms), price_at(tb, tsms), tstop)
    return ("still_open", None, None, None)


def main():
    state = json.loads(POSITIONS.read_text())
    trades = [p for p in state["positions"]
              if p.get("status") == "closed" and (p.get("pnl_usd") or 0) != 0]
    trades.sort(key=lambda p: p.get("entry_time_iso") or "")
    print(f"Real closed trades: {len(trades)}")

    start = min(iso(p["entry_time_iso"]) for p in trades) - timedelta(days=1)
    end = datetime.now(timezone.utc) + timedelta(hours=1)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    coins = sorted({p["coin_a"] for p in trades} | {p["coin_b"] for p in trades})
    tables = {}
    for c in coins:
        tables[c] = price_table(c, start_ms, end_ms)
    print(f"Fetched candles for {len(coins)} coins\n")

    rows = []
    for p in trades:
        rec = {
            "pair": p["pair"],
            "entry": (p.get("entry_time_iso") or "")[:16],
            "actual": round(p.get("pnl_usd") or 0.0, 2),
        }
        for rf in RUNNERS:
            reason, pa, pb, _ = replay_runner(p, tables, rf)
            if pa is None or pb is None:
                rec[f"r{rf}"] = None
                rec[f"r{rf}_why"] = reason
            else:
                pnl, _ = compute_pnl(p, pa, pb)
                rec[f"r{rf}"] = round(pnl, 2)
                rec[f"r{rf}_why"] = reason
        rows.append(rec)

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 240)
    pd.set_option("display.max_rows", 100)
    show = ["pair", "entry", "actual"] + [f"r{rf}" for rf in RUNNERS]
    print(df[show].to_string(index=False))

    print("\n" + "=" * 78)
    print("AGGREGATE NET P&L (USD), replayable trades only")
    print("=" * 78)
    print(f"  {'scheme':<24}{'n':>4}{'total':>11}{'mean':>10}{'median':>10}"
          f"{'winners':>9}{'max':>10}{'min':>10}")
    for rf in RUNNERS:
        col = df[f"r{rf}"].dropna()
        label = "trail(inf)=hold" if rf == float("inf") else \
                ("cross (rf=0)" if rf == 0.0 else f"trail rf={rf}")
        wins = int((col > 0).sum())
        print(f"  {label:<24}{len(col):>4}{col.sum():>11.2f}{col.mean():>10.2f}"
              f"{col.median():>10.2f}{wins:>6}/{len(col):<3}{col.max():>10.2f}"
              f"{col.min():>10.2f}")

    # RSR sensitivity: how much of any uplift is one event?
    print("\n" + "=" * 78)
    print("ROBUSTNESS: same totals with the single RSR event removed")
    print("=" * 78)
    no_rsr = df[~df["pair"].str.contains("RSR", case=False, na=False)]
    print(f"  trades without RSR: {len(no_rsr)}")
    print(f"  {'scheme':<24}{'total':>11}{'mean':>10}")
    for rf in RUNNERS:
        col = no_rsr[f"r{rf}"].dropna()
        label = "trail(inf)=hold" if rf == float("inf") else \
                ("cross (rf=0)" if rf == 0.0 else f"trail rf={rf}")
        print(f"  {label:<24}{col.sum():>11.2f}{col.mean():>10.2f}")

    out = REPO / "reports" / "runner_counterfactual.csv"
    df.to_csv(out, index=False)

    # Apples-to-apples: only trades where EVERY scheme produced a number, so no
    # scheme is flattered/damaged by censoring a big winner.
    cols = [f"r{rf}" for rf in RUNNERS]
    common = df.dropna(subset=cols)
    print("\n" + "=" * 78)
    print(f"APPLES-TO-APPLES: trades replayable under ALL schemes (n={len(common)})")
    print("=" * 78)
    dropped = set(df["pair"]) - set(common["pair"])
    if dropped:
        print(f"  (excluded censored: {', '.join(sorted(dropped))})")
    print(f"  {'scheme':<24}{'total':>11}{'mean':>10}{'vs cross':>12}")
    base = common["r0.0"].sum()
    for rf in RUNNERS:
        col = common[f"r{rf}"]
        label = "trail(inf)=hold" if rf == float("inf") else \
                ("cross (rf=0)" if rf == 0.0 else f"trail rf={rf}")
        print(f"  {label:<24}{col.sum():>11.2f}{col.mean():>10.2f}"
              f"{col.sum()-base:>+12.2f}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
