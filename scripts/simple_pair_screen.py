#!/usr/bin/env python
"""A DELIBERATELY SIMPLE pair screen -- the opposite of walk_forward.py.

Why this exists
---------------
The heavyweight walk-forward harness re-fits a Bayesian cointegration + OU model
per pair and yields a median of 6 trades per run, leaving 67 of 69 saved runs
below its own "sample too small for a verdict" threshold of 20. It also charges
no fees. And it does not implement the live exit rule. So it is rigorous in a
way that produces no usable answer.

This screen makes the opposite trade: a fixed, non-fitted rule, so it is cheap
enough to run across EVERY pair, giving a large pooled sample instead of 6
trades per pair. Costs are charged. The rule is stated in one sentence:

    Spread = log A - log B.  Enter when |z| >= Z over a rolling LOOKBACK.
    Exit on reversion to |z| <= EXIT_Z, or after MAX_HOLD bars.

That is the whole strategy. No fitting, no priors, no MAP, no kappa floor.

What it is for
--------------
    * triage: which pairs deserve a heavy harness at all
    * a control: if the sophisticated model cannot beat this on the same pairs,
      the sophistication is not earning its keep

What it is NOT
--------------
    * a substitute for the live-rule replay
    * point-in-time honest about pair selection (we test pairs known today)
    * a final verdict on any single pair

Usage:
    .venv/bin/python scripts/simple_pair_screen.py
    .venv/bin/python scripts/simple_pair_screen.py --interval 1h --lookback 168
    .venv/bin/python scripts/simple_pair_screen.py --pairs SOL/AVAX,SNX/TIA
"""
from __future__ import annotations

import argparse
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(line_buffering=True)

import numpy as np  # noqa: E402

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices  # noqa: E402

LIVE_PAIRS = [
    ("SOL", "AVAX"), ("AAVE", "COMP"), ("SNX", "DYDX"), ("xyz:GOLD", "xyz:SILVER"),
    ("xyz:NVDA", "xyz:PLTR"), ("AIXBT", "DOGE"), ("xyz:MU", "xyz:TSM"),
    ("xyz:AMD", "xyz:TSM"), ("MEME", "TIA"), ("SNX", "TIA"), ("ORDI", "SOL"),
    ("SNX", "SUI"), ("GMT", "MEME"), ("ETC", "NEO"), ("LTC", "DOGE"),
]

# Cost model: 2 legs x 2 sides x (taker fee + slippage).
TAKER_FEE = 0.00045
SLIPPAGE_BP = 3
BARS_PER_YEAR = {"1h": 24 * 365, "4h": 6 * 365, "1d": 365, "15m": 24 * 4 * 365}


def rolling_z(spread: np.ndarray, lookback: int) -> np.ndarray:
    """z of spread[i] against the mean/sd of the preceding `lookback` bars.

    Trailing only -- no future information leaks into the statistic.
    """
    n = len(spread)
    z = np.full(n, np.nan)
    for i in range(lookback, n):
        w = spread[i - lookback:i]
        sd = w.std()
        if sd > 0:
            z[i] = (spread[i] - w.mean()) / sd
    return z


def simulate(spread: np.ndarray, z: np.ndarray, cfg) -> list[dict]:
    """One position at a time, first signal wins. Long/short the spread.

    Two exit families, because they answer different questions:

    exit_mode="revert"  -- exit when the spread has come BACK to the centre
                           (|z| <= z_exit). This is the conservative, textbook
                           mean-reversion exit. Understates the live bot.

    exit_mode="cross"   -- mirror the LIVE rule: hold PAST convergence until the
                           spread has crossed to the opposite side by
                           cross_frac x |entry_spread|. This is what captured
                           the RSR continuation. `cross_frac` is inert at a
                           coarse cadence (see positions.py), so any value in
                           [0.5, 1.5] behaves identically.
    """
    n = len(spread)
    trades = []
    i = cfg.lookback
    while i < n - 1:
        zi = z[i]
        if np.isnan(zi) or abs(zi) < cfg.z_entry:
            i += 1
            continue
        side = -1 if zi > 0 else 1        # zi>0 -> short the spread
        entry_spread = spread[i]

        exit_i, reason = None, "time"
        for j in range(i + 1, min(i + cfg.max_hold + 1, n)):
            zj = z[j]
            if np.isnan(zj):
                continue
            if cfg.exit_mode == "cross":
                # Centered move relative to entry, on the entry's own centre.
                # For side=-1 (short spread) we profit as spread falls; the live
                # rule closes once it has travelled past the centre and out the
                # far side by cross_frac x |entry move|. In z terms the entry
                # move is zi, so the opposite-side level is -cross_frac*zi.
                if side == -1 and zj <= -cfg.cross_frac * abs(zi):
                    exit_i, reason = j, "cross"
                    break
                if side == 1 and zj >= cfg.cross_frac * abs(zi):
                    exit_i, reason = j, "cross"
                    break
            else:
                if abs(zj) <= cfg.z_exit:
                    exit_i, reason = j, "revert"
                    break
            # hard stop: z moved further against us (both modes)
            if abs(zj) >= cfg.z_stop:
                exit_i, reason = j, "stop"
                break
        if exit_i is None:
            exit_i = min(i + cfg.max_hold, n - 1)

        ret = side * (spread[exit_i] - entry_spread)
        trades.append({
            "entry_i": i, "exit_i": exit_i, "side": side,
            "bars": exit_i - i, "gross": ret, "reason": reason, "z_entry": zi,
        })
        i = exit_i + 1                    # no overlapping positions
    return trades


class Cfg:
    """Plain namespace so simulate() stays pure and importable."""
    def __init__(self, lookback, z_entry, z_exit, z_stop, max_hold,
                 exit_mode="revert", cross_frac=0.75):
        self.lookback, self.z_entry = lookback, z_entry
        self.z_exit, self.z_stop, self.max_hold = z_exit, z_stop, max_hold
        self.exit_mode, self.cross_frac = exit_mode, cross_frac


def screen_pair(a, b, cfg, interval="1h", hours=24 * 300, retries=3):
    """Fetch one pair, run the fixed rule, return (n_bars, trades, error)."""
    import time
    for attempt in range(retries):
        try:
            ts, ca, cb = aligned_close_prices(a, b, interval=interval,
                                              lookback_hours=hours)
            break
        except Exception as e:
            if attempt == retries - 1:
                return 0, [], str(e)[:60]
            time.sleep(2.0 * (attempt + 1))
    else:
        return 0, [], "fetch failed"

    if ts is None or len(ts) < cfg.lookback + cfg.max_hold + 10:
        return (0 if ts is None else len(ts)), [], "insufficient bars"
    la, lb = np.log(np.asarray(ca, float)), np.log(np.asarray(cb, float))
    spread = la - lb
    z = rolling_z(spread, cfg.lookback)
    return len(ts), simulate(spread, z, cfg), ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--lookback", type=int, default=168,
                    help="bars in the rolling z window (168 = 7d at 1h)")
    ap.add_argument("--hours", type=int, default=24 * 300,
                    help="history to request (HL caps ~5000 bars)")
    ap.add_argument("--z-entry", type=float, default=2.0)
    ap.add_argument("--z-exit", type=float, default=0.5)
    ap.add_argument("--z-stop", type=float, default=4.0)
    ap.add_argument("--max-hold", type=int, default=72)
    ap.add_argument("--pairs", default="", help="comma list A/B, overrides live pairs")
    ap.add_argument("--exit-mode", default="revert", choices=["revert", "cross"],
                    help="'cross' mirrors the LIVE rule (hold past convergence)")
    ap.add_argument("--cross-frac", type=float, default=0.75)
    args = ap.parse_args()

    pairs = LIVE_PAIRS
    if args.pairs:
        pairs = [tuple(p.split("/")) for p in args.pairs.split(",")]

    bars_year = BARS_PER_YEAR.get(args.interval, 24 * 365)
    cost = 2 * 2 * (TAKER_FEE + SLIPPAGE_BP / 10000.0)
    cfg = Cfg(args.lookback, args.z_entry, args.z_exit, args.z_stop, args.max_hold,
              exit_mode=args.exit_mode, cross_frac=args.cross_frac)

    print("=" * 108)
    print("SIMPLE PAIR SCREEN  (fixed rule, no fitting, costs charged)")
    print(f"  interval={args.interval}  lookback={args.lookback}  z_entry={args.z_entry}  "
          f"z_exit={args.z_exit}  z_stop={args.z_stop}  max_hold={args.max_hold}")
    print(f"  exit_mode={args.exit_mode}" +
          (f"  cross_frac={args.cross_frac}  (LIVE rule: hold PAST convergence)"
           if args.exit_mode == "cross" else "  (conservative: exit AT convergence)"))
    print(f"  round-trip cost per trade = {cost*10000:.1f} bp (2 legs x 2 sides)")
    print("=" * 108)
    print(f"  {'pair':<22}{'bars':>6}{'n':>5}{'gross%':>9}{'net%':>9}{'hit%':>6}"
          f"{'bars/t':>7}{'t':>7}{'annSR':>7}")

    pooled = []
    for a, b in pairs:
        nb, tr, err = screen_pair(a, b, cfg, args.interval, args.hours)
        pair = f"{a}/{b}"
        if err:
            print(f"  {pair:<22}{nb:>6}  {err}")
            continue
        if not tr:
            print(f"  {pair:<22}{nb:>6}{0:>5}")
            continue

        g = np.asarray([t["gross"] for t in tr]) * 100
        net = g - cost * 100
        m, s = net.mean(), net.std(ddof=1) if len(net) > 1 else 0.0
        tstat = m / (s / np.sqrt(len(net))) if s > 0 else 0.0
        bpt = st.median([t["bars"] for t in tr])
        sr = (m / s) * np.sqrt(bars_year / bpt) if s > 0 and bpt > 0 else 0.0
        print(f"  {pair:<22}{nb:>6}{len(tr):>5}{g.mean():>9.2f}{m:>9.2f}"
              f"{100*(net > 0).mean():>6.0f}{bpt:>7.0f}{tstat:>7.2f}{sr:>7.2f}")
        for t in tr:
            t = dict(t)
            t["pair"] = pair
            pooled.append(t)

    if not pooled:
        print("\nNo trades at all.")
        return

    g = np.asarray([t["gross"] for t in pooled]) * 100
    net = g - cost * 100
    m, s = net.mean(), net.std(ddof=1)
    tstat = m / (s / np.sqrt(len(net))) if s > 0 else 0.0
    n = len(net)

    print("\n" + "=" * 108)
    print("POOLED (all pairs) -- compare against the heavyweight harness's median of 6 trades/pair")
    print("=" * 108)
    print(f"  trades:            {n:,}")
    print(f"  gross mean:        {g.mean():+.3f}%   (walk_forward.py reports numbers like this)")
    print(f"  net mean:          {net.mean():+.3f}%   after {cost*10000:.1f} bp")
    print(f"  median net:        {np.median(net):+.3f}%")
    print(f"  hit rate:          {100*(net > 0).mean():.1f}%")
    print(f"  std:               {net.std(ddof=1):.3f}%")
    print(f"  t-stat on mean:    {tstat:+.2f}")
    # bootstrap CI (simple, per-trade)
    rng = np.random.default_rng(7)
    bs = np.asarray([rng.choice(net, n, replace=True).mean() for _ in range(4000)])
    lo, hi = np.percentile(bs, [2.5, 97.5])
    print(f"  95% CI on net mean:[{lo:+.3f}%, {hi:+.3f}%]")
    med_bars = st.median([t["bars"] for t in pooled])
    print(f"  median hold:       {med_bars:.0f} bars -> ~{n/(len(pairs)*args.hours/24):.2f} trades/pair/day")
    print(f"  annualised Sharpe: {(net.mean()/net.std(ddof=1)) * np.sqrt(bars_year/med_bars):+.2f}")

    by_reason = {}
    for t in pooled:
        by_reason.setdefault(t["reason"], []).append(t["gross"] * 100)
    print("\n  by exit reason:")
    for r, v in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"    {r:<8} n={len(v):>5}  gross mean={np.mean(v):+.3f}%")

    print("\n  VERDICT (read honestly):")
    if n < 30:
        print(f"    Only {n} trades. Not enough for any conclusion -- same trap as the")
        print("    heavyweight harness.")
    elif lo > 0:
        print(f"    Net CI [{lo:+.2f}%, {hi:+.2f}%] excludes zero. Worth a heavier look.")
    elif hi < 0:
        print(f"    Net CI [{lo:+.2f}%, {hi:+.2f}%] entirely below zero. The simple")
        print("    rule does not work here -- and note this is the EASY version.")
    else:
        print(f"    Net CI [{lo:+.2f}%, {hi:+.2f}%] crosses zero: no evidence either way")
        print(f"    from {n} trades. Indicative only.")
    print("    This screen is triage, not proof: survival-biased pair list, one")
    print("    market regime of history, and no live-exit fidelity.")


if __name__ == "__main__":
    main()
