#!/usr/bin/env python
"""Was the cross exit a mistake to question? Measure the two exit families.

The concern raised: the 'cross' exit deliberately holds PAST convergence and
closes on the opposite side, because many spreads keep travelling after they
cross. That behaviour captured the RSR short continuation. So a screen whose
exit is 'close AT convergence' is not measuring the live bot -- it is measuring
a weaker exit.

This script runs BOTH exits on the SAME entries and reports the difference, so
the question 'does holding past convergence pay?' is answered with numbers
rather than defended in prose.

    exit_mode=revert : close when |z| <= z_exit          (at convergence)
    exit_mode=cross  : close when the spread has crossed  (past convergence)
                       to the opposite side by cross_frac x |entry move|

Usage:
    .venv/bin/python scripts/exit_family_compare.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(line_buffering=True)

import numpy as np  # noqa: E402

from simple_pair_screen import (  # noqa: E402
    Cfg, LIVE_PAIRS, TAKER_FEE, SLIPPAGE_BP, rolling_z, simulate,
)
from src.hyperliquid_bot.data.ohlcv import aligned_close_prices  # noqa: E402

COST = 2 * 2 * (TAKER_FEE + SLIPPAGE_BP / 10000.0)


def summarise(name, trades):
    if not trades:
        print(f"  {name:<26} no trades")
        return None
    g = np.asarray([t["gross"] for t in trades]) * 100
    net = g - COST * 100
    t_ = net.mean() / (net.std(ddof=1) / np.sqrt(len(net))) if net.std(ddof=1) > 0 else 0.0
    print(f"  {name:<26}{len(net):>6}{g.mean():>+10.3f}{net.mean():>+10.3f}"
          f"{100*(net > 0).mean():>+8.1f}{np.median([t['bars'] for t in trades]):>8.0f}{t_:>8.2f}")
    return net


def main():
    lookup = int(sys.argv[1]) if len(sys.argv) > 1 else 168
    print("=" * 104)
    print("EXIT FAMILY COMPARISON -- does holding PAST convergence pay?")
    print("=" * 104)
    print(f"  lookback={lookup}  z_entry=2.0  z_stop=4.0  max_hold=72  cost={COST*10000:.0f}bp")
    print(f"\n  {'exit family':<26}{'n':>6}{'gross%':>10}{'net%':>10}{'hit%':>8}"
          f"{'bars':>8}{'t':>8}")

    results = {}
    for mode in ("revert", "cross"):
        cfg = Cfg(lookup, 2.0, 0.5, 4.0, 72, exit_mode=mode, cross_frac=0.75)
        all_tr = []
        for a, b in LIVE_PAIRS:
            try:
                ts, ca, cb = aligned_close_prices(a, b, interval="1h",
                                                  lookback_hours=24 * 260)
            except Exception:
                continue
            if ts is None or len(ts) < lookup + 80:
                continue
            sp = np.log(np.asarray(ca, float)) - np.log(np.asarray(cb, float))
            all_tr.extend(simulate(sp, rolling_z(sp, lookup), cfg))
        results[mode] = summarise(f"{mode}", all_tr)
        if mode == "cross" and all_tr:
            reasons = {}
            for t in all_tr:
                reasons.setdefault(t["reason"], []).append(t["gross"] * 100)
            for r, v in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
                print(f"      {r:<12} n={len(v):>4}  gross mean={np.mean(v):+.3f}%")

    if results.get("revert") is not None and results.get("cross") is not None:
        r, c = results["revert"], results["cross"]
        diff = c.mean() - r.mean()
        print("\n" + "=" * 104)
        print("READING")
        print("=" * 104)
        print(f"  net mean  revert={r.mean():+.3f}%   cross={c.mean():+.3f}%   "
              f"difference={diff:+.3f}%/trade")
        if c.mean() > r.mean():
            print("  => Holding PAST convergence beats exiting AT convergence on this data.")
            print("     The cross exit is earning its keep; my earlier net figure, which used")
            print("     the revert exit, was PESSIMISTIC relative to the live rule.")
        else:
            print("  => On this data the cross exit does NOT beat the revert exit. But note the")
            print("     max_hold and z_stop may be cutting the continuation short, and this is a")
            print("     proxy on 1h bars rather than the live 4h cadence.")
        print("\n  Limits: proxy rule on 1h bars (live runs 4h); single regime; the continuation")
        print("  capture is tail-driven, so a mean difference can be one or two episodes.")


if __name__ == "__main__":
    main()
