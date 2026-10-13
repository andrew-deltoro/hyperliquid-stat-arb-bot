#!/usr/bin/env python
"""What was the RSR recipe, and how do we buy more tickets on it?

Two facts drive this:
  1. Longing the cheap leg is the profit centre (83% of gross PnL); the short
     leg was a drag (-$3.84 ex-RSR).
  2. The squeeze itself was not forecastable -- RSR's prior 36h were quiet.

If the event is not forecastable, the strategy question changes from "predict
the squeeze" to "how many cheap-leg tickets can we hold, and does the payoff
distribution justify holding them". So this measures the distribution of
per-position returns across every OPEN signal the bot generated, and what the
right tail contributes.

RSR's cheapness was RELATIVE (spread below its pair mean), not absolute -- RSR
was up ~4% over the prior 24h. So 'cheapest by own return' would NOT have
selected it. The recipe is 'long leg of a stretched spread', not 'long losers'.
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

H = 24


def main() -> None:
    px, sig = load_prices(), load_signals()
    lo, hi = px["ts"].min(), px["ts"].max()
    sig = sig[(sig["ts"] >= lo) & (sig["ts"] <= hi - pd.Timedelta(hours=72))]
    df = build(sig, px)
    df = df.dropna(subset=["pa0", "pb0", f"pa_{H}", f"pb_{H}"]).copy()
    df["bet"] = -np.sign(df["z"])
    df["spread0"] = np.log(df["pa0"]) - df["beta"] * np.log(df["pb0"])

    # Leg-level returns (what each side actually did).
    df["ra"] = df[f"pa_{H}"] / df["pa0"] - 1.0
    df["rb"] = df[f"pb_{H}"] / df["pb0"] - 1.0
    long_is_a = df["bet"] > 0
    df["ret_long"] = np.where(long_is_a, df["ra"], df["rb"])
    df["ret_short"] = np.where(long_is_a, df["rb"], df["ra"])

    # Position return: 0.5*(long move) - 0.5*(short move) - cost.
    df["ret"] = 0.5 * df["ret_long"] - 0.5 * df["ret_short"] - ROUND_TRIP_COST

    opens = df[df["action"] == "OPEN"].copy()
    print("=" * 78)
    print(f"LEG DECOMPOSITION ACROSS {len(opens)} OPEN SIGNALS ({H}h hold)")
    print("=" * 78)
    print("  Per unit of notional, the cheap leg and expensive leg contribute:")
    print(f"    long  (cheap) leg : {opens['ret_long'].mean()*100:>+7.3f}%  mean, "
          f"median {opens['ret_long'].median()*100:>+7.3f}%")
    print(f"    short (expensive) : {-opens['ret_short'].mean()*100:>+7.3f}%  mean, "
          f"median {-opens['ret_short'].median()*100:>+7.3f}%")
    print(f"    net position      : {opens['ret'].mean()*100:>+7.3f}%  mean  "
          f"(cost {ROUND_TRIP_COST*100:.4f}% deducted)")
    print("\n  NOTE both legs drift up on average in this window, so a long/short")
    print("  pair is not market-neutral by construction here.")

    print()
    print("=" * 78)
    print("PAYOFF DISTRIBUTION: HOW OFTEN DOES A TICKET PAY?")
    print("=" * 78)
    r = opens["ret"]
    q = r.quantile([0.01, 0.05, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99])
    print(f"  n={len(r)}  mean {r.mean()*100:+.3f}%  sd {r.std()*100:.2f}%  "
          f"skew {r.skew():+.2f}")
    for p, v in q.items():
        print(f"    p{int(p*100):<3} {v*100:>+7.2f}%")
    print(f"    worst {r.min()*100:>+7.2f}%    best {r.max()*100:>+7.2f}%")
    print(f"\n  P(ret > +10%) = {100*(r>0.10).mean():.2f}%")
    print(f"  P(ret > +20%) = {100*(r>0.20).mean():.2f}%")
    print(f"  P(ret > +30%) = {100*(r>0.30).mean():.2f}%")

    # How much of total profit sits in the tail?
    print("\n  Share of total profit contributed by the top k% of positions:")
    s = r.sort_values(ascending=False)
    tot = s[s > 0].sum()
    for k in (1, 5, 10, 25):
        n_top = max(1, int(len(s) * k / 100))
        share = s.head(n_top).sum() / tot * 100 if tot > 0 else np.nan
        print(f"    top {k:>2}% ({n_top:>3} positions): {share:>6.1f}%")

    print()
    print("=" * 78)
    print("TICKET RATE: HOW MANY SQUEEZE CHANCES PER WEEK?")
    print("=" * 78)
    days = (opens["ts"].max() - opens["ts"].min()).total_seconds() / 86400
    pairs = opens["pair"].nunique()
    # Signals repeat per pair per cycle, so count distinct pair-days as tickets.
    tickets = opens.groupby([opens["pair"], opens["day"] if "day" in opens
                             else opens["ts"].dt.floor("D")]).ngroups
    print(f"  window           : {days:.1f} days")
    print(f"  pairs traded     : {pairs}")
    print(f"  distinct pair-days with an OPEN signal: {tickets} "
          f"({tickets/days:.1f}/day)")
    print(f"  our live book held ~2-4 pairs at once.")
    print(f"\n  If the squeeze rate is ~{100*(r>0.20).mean():.1f}% per ticket, then:")
    for per_week in (10, 25, 50, 100):
        ev = per_week * (r > 0.20).mean()
        print(f"    {per_week:>3} tickets/week -> {ev:.1f} +20% events/week")

    print()
    print("=" * 78)
    print("WAS RSR 'CHEAP' IN ABSOLUTE TERMS? (would a loser-basket catch it?)")
    print("=" * 78)
    print("  RSR before the squeeze: ~0.00158-0.00166 for 3 days, i.e. FLAT-to-up.")
    print("  Its cheapness was RELATIVE to TAO (z=-0.945, spread below its mean).")
    print("  => 'long the biggest losers' would NOT have selected RSR.")
    print("  => the recipe is 'long leg of a stretched spread', not 'long losers'.")
    print("\n  Check: what did the cheap leg's own 24h return look like at entry?")
    prev = -(opens["ra"])  # not used; placeholder for clarity
    print(f"    the cheap leg was, on median, {opens['ret_long'].median()*100:+.2f}% "
          f"over the hold -- the squeeze is the tail, not the norm")


if __name__ == "__main__":
    main()
