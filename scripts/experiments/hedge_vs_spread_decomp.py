#!/usr/bin/env python3
"""
Proper P&L decomposition: hedge leak vs spread capture.

The "dominant leg" metric is ambiguous -- a legitimate idiosyncratic spread
dislocation ALSO shows one leg dominating. So decompose exactly:

For each trade, let:
  r_A, r_B = simple returns of the two legs over the hold
  s        = +1 if LONG_A_SHORT_B else -1

  P&L = s * (legA*r_A - legB*r_B)

Write r_A = r_B + d  (d = the RELATIVE move, i.e. the spread move in simple
returns). Then:

  P&L = s*r_B*(legA - legB)   +   s*legA*d
        (HEDGE LEAK)                (SPREAD CAPTURE)
        common move times           what a mean-reversion
        the un-equal legs           pair is supposed to earn

HEDGE LEAK is exposure we did not intend: it is nonzero only because the leg
notionals are un-equal (they are set so 1/beta). If the true common-factor
beta differs from the fitted beta, the leak is systematic, not luck.

Run: .venv/bin/python scripts/experiments/hedge_vs_spread_decomp.py
"""
from __future__ import annotations

import csv
from pathlib import Path

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

    print(f"{'pair':<20}{'legA$':>8}{'legB$':>8}{'s':>3}{'rA%':>8}{'rB%':>8}"
          f"{'d%':>8}{'LEAK$':>9}{'SPREAD$':>9}{'net$':>9}")
    print("-" * 108)

    tl = ts = tn = 0.0
    rows_out = []
    for r in rows:
        a, b = r["pair"].split("/", 1)
        direction = r["direction"]
        s = 1.0 if direction == "LONG_A_SHORT_B" else -1.0
        la, lb = float(r["leg_a_usd"]), float(r["leg_b_usd"])
        try:
            ra = float(r["exit_price_a"]) / float(r["entry_price_a"]) - 1.0
            rb = float(r["exit_price_b"]) / float(r["entry_price_b"]) - 1.0
        except (KeyError, ValueError, ZeroDivisionError):
            continue
        d = ra - rb
        leak = s * rb * (la - lb)
        spread = s * la * d
        gross = leak + spread
        net = float(r["pnl_usd"] or 0)
        tl += leak
        ts += spread
        tn += net
        rows_out.append((r["pair"], leak, spread, gross, net))
        print(f"{r['pair']:<20}{la:>8.2f}{lb:>8.2f}{'L' if s>0 else 'S':>3}"
              f"{ra*100:>8.2f}{rb*100:>8.2f}{d*100:>8.2f}"
              f"{leak:>9.2f}{spread:>9.2f}{net:>9.2f}")

    print("-" * 108)
    print(f"{'TOTAL':<20}{'':>37}{tl:>9.2f}{ts:>9.2f}{tn:>9.2f}")
    print(f"\nhedge leak total    : ${tl:>8.2f}")
    print(f"spread capture total: ${ts:>8.2f}")
    print(f"net P&L (journal)   : ${tn:>8.2f}")
    g = abs(tl) + abs(ts)
    if g:
        print(f"\n=> {abs(tl)/g*100:.0f}% of gross P&L magnitude is HEDGE LEAK "
              f"(un-intended common-factor exposure)")
        print(f"=> {abs(ts)/g*100:.0f}% is SPREAD CAPTURE (the intended edge)")

    # Sign agreement: does the leak explain the losers?
    print("\nLosers: is the leak the culprit?")
    for pair, leak, spread, gross, net in rows_out:
        if net < 0:
            who = "LEAK" if abs(leak) > abs(spread) else "spread"
            print(f"  {pair:<20} net={net:>7.2f}  leak={leak:>7.2f} "
                  f"spread={spread:>7.2f}  -> dominated by {who}")


if __name__ == "__main__":
    main()
