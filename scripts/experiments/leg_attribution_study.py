#!/usr/bin/env python3
"""
Leg attribution across the whole closed book.

A mean-reversion pair trade is supposed to make money on BOTH legs at once:
the rich leg falls and the cheap leg rises, so the spread converges. When
only one leg explains the P&L, we did not harvest a spread -- we were long
(or short) a single coin and the pair only got in the way.

For every closed trade we split gross P&L into leg A / leg B and classify:
  - REVERSION  both legs positive (or both non-trivial and net > 0)
  - DRIFT      one leg dominates: |best leg| / (|a|+|b|) >= dominant_frac

Run: .venv/bin/python scripts/experiments/leg_attribution_study.py
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

PDT = timezone(timedelta(hours=-7))
DOMINANT_FRAC = 0.85
TRADES = [
    Path("/home/hlbot/hlbot-runtime/live/reports/trades.csv"),
    Path("reports/trades.csv"),
]


def pdt(iso: str) -> str:
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(PDT).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return "?"


def find_trades() -> Path | None:
    for p in TRADES:
        if p.exists():
            return p
    return None


def main() -> None:
    f = find_trades()
    if f is None:
        print("no trades.csv found")
        return
    rows = list(csv.DictReader(open(f)))
    print(f"journal: {f}   trades: {len(rows)}\n")

    # Legacy rows (pre-attribution) have no leg columns: recompute from prices.
    hdr = rows[0].keys() if rows else []
    has_leg_cols = "pnl_a_usd" in hdr

    print(f"{'pair':<20}{'dir':<16}{'reason':<16}{'legA$':>9}{'legB$':>9}"
          f"{'gross':>9}{'net':>9}  class")
    print("-" * 100)

    tot_abs_a = tot_abs_b = tot_net = 0.0
    n_drift = n_rev = n_lost = 0
    drift_pnl = rev_pnl = 0.0

    for r in rows:
        pair = r.get("pair", "?")
        direction = r.get("direction", "?")
        a, b = (pair.split("/", 1) + ["?"])[:2] if "/" in pair else (pair, "?")
        la, lb = float(r.get("leg_a_usd") or 0), float(r.get("leg_b_usd") or 0)
        sa = 1.0 if direction == "LONG_A_SHORT_B" else -1.0
        try:
            pa = la * sa * (float(r["exit_price_a"]) / float(r["entry_price_a"]) - 1.0)
            pb = lb * (-sa) * (float(r["exit_price_b"]) / float(r["entry_price_b"]) - 1.0)
        except (KeyError, ValueError, ZeroDivisionError):
            continue
        if has_leg_cols and (r.get("pnl_a_usd") or "").strip():
            pa, pb = float(r["pnl_a_usd"]), float(r["pnl_b_usd"])
        gross = pa + pb
        net = float(r.get("pnl_usd") or 0)
        denom = abs(pa) + abs(pb) + 1e-9
        best = max(abs(pa), abs(pb))
        dom = best / denom
        if gross < 0:
            cls = "LOSS"
            n_lost += 1
        elif dom >= DOMINANT_FRAC:
            cls = f"DRIFT {a if abs(pa)>=abs(pb) else b}({dom:.0%})"
            n_drift += 1
            drift_pnl += gross
        else:
            cls = "REVERSION"
            n_rev += 1
            rev_pnl += gross
        tot_abs_a += abs(pa)
        tot_abs_b += abs(pb)
        tot_net += net
        print(f"{pair:<20}{direction:<16}{r.get('exit_reason','?'):<16}"
              f"{pa:>9.2f}{pb:>9.2f}{gross:>9.2f}{net:>9.2f}  {cls}")

    print("-" * 100)
    print(f"sum |legA| = ${tot_abs_a:>8.2f}   sum |legB| = ${tot_abs_b:>8.2f}")
    print(f"total net P&L = ${tot_net:>8.2f}")
    print(f"\nclassification: reversion={n_rev}  single-leg-drift={n_drift}  losses={n_lost}")
    print(f"  P&L from reversion trades    : ${rev_pnl:>8.2f}")
    print(f"  P&L from single-leg drift    : ${drift_pnl:>8.2f}")
    tot_cls = rev_pnl + drift_pnl
    if tot_cls:
        print(f"  => {drift_pnl/tot_cls*100:.0f}% of gross winning P&L came from ONE LEG, "
              f"not spread convergence")


if __name__ == "__main__":
    main()
