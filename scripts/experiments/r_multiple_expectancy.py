#!/usr/bin/env python3
"""
R-multiple expectancy and RSR dependence for the closed book.

Answers the questions that matter for position sizing and for deciding
whether the book has an edge at all:

  1. R multiples: risk = |entry_spread - stop_spread|, so every trade is
     expressed in units of its own risk. Removes the spread-scale confound
     (1bp of spread in BTC/ETH != 1bp in RSR/TAO).
  2. Concentration: how much of the total depends on RSR/TAO?
  3. Payoff ratio and hit rate, which together imply whether the exit
     geometry (target at ~0.25R, stop at 1R) can ever work.

Run: .venv/bin/python scripts/experiments/r_multiple_expectancy.py
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

TRADES = [
    Path("/home/hlbot/hlbot-runtime/live/reports/trades.csv"),
    Path("reports/trades.csv"),
]


def main() -> None:
    f = next((p for p in TRADES if p.exists()), None)
    if f is None:
        print("no trades.csv")
        return
    rows = list(csv.DictReader(open(f)))

    print(f"{'pair':<20}{'reason':<16}{'risk':>8}{'reward':>9}{'R':>8}{'net$':>9}")
    print("-" * 72)
    R, net, rr, rew, pair = [], [], [], [], []
    for r in rows:
        es = float(r["entry_spread"])
        xs = float(r["exit_spread"])
        tg = float(r["target_spread"])
        st = float(r["stop_spread"])
        n = float(r["pnl_usd"] or 0)
        risk = abs(es - st)
        reward = abs(es - tg)
        fav = -np.sign(es) * (xs - es) if es != 0 else 0.0
        R.append(fav / risk if risk > 0 else 0.0)
        node = R[-1]
        net.append(n)
        rr.append(risk)
        rew.append(reward)
        pair.append(r["pair"])
        print(f"{r['pair']:<20}{r['exit_reason']:<16}{risk:>8.4f}"
              f"{reward:>9.4f}{node:>8.2f}{n:>9.2f}")

    R = np.array(R)
    net = np.array(net)
    rr = np.array(rr)
    rew = np.array(rew)
    print("-" * 72)
    is_rsr = np.array(["RSR" in p for p in pair])

    print(f"\nhit rate          : {(net > 0).mean():.0%}  ({(net > 0).sum()}/{len(net)})")
    print(f"mean R (all)      : {R.mean():+.2f}   median R: {np.median(R):+.2f}")
    print(f"mean R ex-RSR     : {R[~is_rsr].mean():+.2f}")

    print(f"\ntotal net         : ${net.sum():+.2f}")
    print(f"total net ex-RSR  : ${net[~is_rsr].sum():+.2f}")
    print(f"  => RSR contribution: {net[is_rsr].sum()/net.sum()*100:.0f}% of all P&L"
          if net.sum() else "")

    print(f"\nmean reward/risk geometry: "
          f"reward={rew.mean():.4f} risk={rr.mean():.4f} "
          f"=> target is only {rew.mean()/rr.mean()*100:.0f}% of the stop distance")
    w = net > 0
    if w.any() and (~w).any():
        print(f"payoff ratio      : {R[w].mean()/abs(R[~w].mean()):.2f} "
              f"(winners {R[w].mean():+.2f}R vs losers {R[~w].mean():+.2f}R)")
        be = 1 / (1 + R[w].mean() / abs(R[~w].mean()))
        print(f"breakeven win rate: {be:.0%}  vs actual {(net > 0).mean():.0%}")
        print("\nA target at ~0.25R with a 1R stop needs a very high hit rate. "
              "This geometry only works if the stop is far tighter or the "
              "target far wider than configured.")


if __name__ == "__main__":
    main()
