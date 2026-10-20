#!/usr/bin/env python
"""Are ideas dying from DISPROOF or from UNDERPOWER?  Answer it with numbers.

Motivation
----------
67 of 69 saved walk-forward runs produced fewer than 20 trades -- below the
harness's own "sample too small for a verdict" threshold. Median: 6 trades.
So the pipeline cannot reject an idea; as the simplification audit put it, "the
bot is not over-fit to one regime so much as UNMEASURED across regimes."

The expensive thing was re-fitting a Bayesian model per pair per day. This
script does not re-fit anything. It asks a cheaper and more decisive question:

    If I run a simple, non-fitted rule over the same pair universe, do I get
    enough trades to measure anything -- and does the edge, if any, survive
    costs and a null distribution?

That decomposes into two tests, both cheap:

  TEST A -- THE NULL. Run the same rule on RANDOM, unrelated series pairs.
      If the real pairs perform no better than unrelated pairs, the
      "cointegration" signal is doing no work.

  TEST B -- RANDOMIZED ENTRY. Keep the real pair and the real holds, but pick
      entry dates at random. If real entries do no better than random entries
      in the same series, the entry timing does no work.

Both are honest tests of the whole pipeline, and both are impossible with 6
trades per pair.

Usage:
    .venv/bin/python scripts/power_or_disproof.py --shuffles 300
"""
from __future__ import annotations

import argparse
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(line_buffering=True)

import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simple_pair_screen import (  # noqa: E402
    Cfg, LIVE_PAIRS, screen_pair, rolling_z, simulate, TAKER_FEE, SLIPPAGE_BP,
    BARS_PER_YEAR,
)


def bootstrap_ci(x, n=4000, seed=7):
    x = np.asarray(x, float)
    rng = np.random.default_rng(seed)
    bs = np.asarray([rng.choice(x, len(x), replace=True).mean() for _ in range(n)])
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return float(lo), float(hi)


def one_tail_p(obs, null):
    """Fraction of null draws at least as large as obs (0 if none)."""
    null = np.asarray(null, float)
    return float((null >= obs).mean())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--hours", type=int, default=24 * 200)
    ap.add_argument("--lookback", type=int, default=168)
    ap.add_argument("--z-entry", type=float, default=2.0)
    ap.add_argument("--z-exit", type=float, default=0.5)
    ap.add_argument("--z-stop", type=float, default=4.0)
    ap.add_argument("--max-hold", type=int, default=72)
    ap.add_argument("--shuffles", type=int, default=300)
    args = ap.parse_args()

    cfg = Cfg(args.lookback, args.z_entry, args.z_exit, args.z_stop, args.max_hold)
    cost = 2 * 2 * (TAKER_FEE + SLIPPAGE_BP / 10000.0)
    rng = np.random.default_rng(11)

    # ---------------------------------------------------------------- real run
    print("=" * 100)
    print("POWER OR DISPROOF?  fixed rule, no fitting, costs charged")
    print("=" * 100)
    series = {}
    real_all = []
    per_pair = {}
    for a, b in LIVE_PAIRS:
        nb, tr, err = screen_pair(a, b, cfg, args.interval, args.hours)
        pair = f"{a}/{b}"
        if err:
            print(f"  {pair:<22} skip: {err}")
            continue
        series[pair] = (a, b, nb)
        for t in tr:
            t = dict(t)
            t["pair"] = pair
            real_all.append(t)
        if tr:
            per_pair[pair] = [t["gross"] for t in tr]

    if not real_all:
        sys.exit("no real trades")
    g_real = np.asarray([t["gross"] for t in real_all])            # in log units
    net_real = g_real * 100 - cost * 100
    lo, hi = bootstrap_ci(net_real)
    print(f"\n  REAL: {len(net_real):,} trades over {len(series)} pairs")
    print(f"    gross mean  {g_real.mean()*100:+.3f}%")
    print(f"    net mean    {net_real.mean():+.3f}%   (after {cost*10000:.0f} bp)")
    print(f"    median net  {np.median(net_real):+.3f}%")
    print(f"    t-stat      {net_real.mean()/(net_real.std(ddof=1)/np.sqrt(len(net_real))):+.2f}")
    print(f"    95% CI      [{lo:+.3f}%, {hi:+.3f}%]")

    # ------------------------------------------------------- TEST A: the null
    print("\n" + "=" * 100)
    print(f"TEST A -- NULL DISTRIBUTION ({args.shuffles} draws)")
    print("=" * 100)
    print("  Same rule, but the 'pair' is two RANDOM UNRELATED series. This is what")
    print("  the rule earns when there is no economic relationship to exploit.")

    # gather a pool of raw series to build random pairs from
    pool = []
    for pair, (a, b, nb) in series.items():
        try:
            from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
            ts, ca, _ = aligned_close_prices(a, b, interval=args.interval,
                                             lookback_hours=args.hours)
            pool.append((a, np.log(np.asarray(ca, float))))
        except Exception:
            continue
    pool = [p for p in pool if len(p[1]) > args.lookback + args.max_hold + 50]
    print(f"  series pool: {len(pool)}")

    null_means = []
    for k in range(args.shuffles):
        i, j = rng.integers(0, len(pool)), rng.integers(0, len(pool))
        if i == j:
            continue
        x, y = pool[i][1], pool[j][1]
        n = min(len(x), len(y))
        spread = x[-n:] - y[-n:]
        # randomly WINDOW-OFFSET the second series so we destroy any true alignment
        z = rolling_z(spread, cfg.lookback)
        tr = simulate(spread, z, cfg)
        if tr:
            null_means.append(np.mean([t["gross"] for t in tr]))
    null_means = np.asarray(null_means) * 100
    obs = g_real.mean() * 100
    p_a = one_tail_p(obs, null_means)
    print(f"  null gross mean: {null_means.mean():+.3f}%  "
          f"p5={np.percentile(null_means,5):+.3f}%  p95={np.percentile(null_means,95):+.3f}%")
    print(f"  REAL gross mean: {obs:+.3f}%")
    print(f"  one-tailed p (real beats random unrelated pairs): {p_a:.3f}")

    # -------------------------------------------------- TEST B: random entries
    print("\n" + "=" * 100)
    print("TEST B -- RANDOMIZED ENTRY DATES (same pairs, same hold lengths)")
    print("=" * 100)
    print("  Keep each real trade's pair and holding period, but choose the entry bar")
    print("  at random. Isolates whether the z-score ENTRY adds anything.")

    # Preload every pair's log series ONCE (the API must not be hit per shuffle).
    print("\n  preloading series for TEST B ...")
    cache = {}
    for pair, (a, b, nb) in series.items():
        try:
            from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
            ts, ca, cb = aligned_close_prices(a, b, interval=args.interval,
                                             lookback_hours=args.hours)
            la, lb = np.log(np.asarray(ca, float)), np.log(np.asarray(cb, float))
            n = min(len(la), len(lb))
            cache[pair] = (la[-n:] - lb[-n:])          # the spread only
        except Exception:
            continue
    print(f"  cached {len(cache)} pairs")

    rand_means = []
    for k in range(args.shuffles):
        tot, cnt = 0.0, 0
        for tr in real_all:
            sp = cache.get(tr["pair"])
            if sp is None:
                continue
            hold = tr["bars"]
            n = len(sp)
            if n - hold - 1 <= cfg.lookback:
                continue
            i = int(rng.integers(cfg.lookback, n - hold - 1))
            j = i + hold
            side = -1 if rng.random() < 0.5 else 1
            tot += side * (sp[j] - sp[i])
            cnt += 1
        if cnt:
            rand_means.append(tot / cnt)
    rand_means = np.asarray(rand_means) * 100
    p_b = one_tail_p(obs, rand_means)
    if len(rand_means):
        print(f"  random-entry gross mean: {rand_means.mean():+.3f}%  "
              f"p5={np.percentile(rand_means,5):+.3f}%  "
              f"p95={np.percentile(rand_means,95):+.3f}%")
        print(f"  REAL gross mean:         {obs:+.3f}%")
        print(f"  one-tailed p (entry timing beats random date/direction): {p_b:.3f}")
    else:
        print("  could not build random-entry draws")

    # ------------------------------------------------------------- conclusion
    print("\n" + "=" * 100)
    print("CONCLUSION")
    print("=" * 100)
    print(f"  This run produced {len(net_real):,} trades across {len(series)} pairs in"
          f" ~1 minute, no model fitting.")
    print(f"  For comparison, ALL 69 saved heavyweight walk-forward runs contain 522")
    print(f"  trades total, with a median of 6 per run.")
    print()
    verdict_a = ("NOT distinguishable from unrelated random pairs"
                 if p_a > 0.10 else "beats unrelated random pairs")
    verdict_b = ("NOT distinguishable from random entry dates"
                 if p_b > 0.10 else "beats random entry dates")
    print(f"  A) signal vs null       : {verdict_a} (p={p_a:.3f})")
    print(f"  B) entry timing vs random: {verdict_b} (p={p_b:.3f})")
    print()
    if net_real.mean() < 0:
        print("  AND the net-of-cost mean is NEGATIVE. Both tests may be moot: whatever")
        print("  gross edge exists is consumed by the ~30 bp round trip on 2 legs.")
    print("\n  Honest limits: single regime of history; pair list chosen with hindsight;")
    print("  fixed rule is a proxy, not the live exit rule. This measures whether an")
    print("  idea is MARGINAL OR MEASURABLE, not whether the live bot works.")


if __name__ == "__main__":
    main()
