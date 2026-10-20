#!/usr/bin/env python
"""Is the cross-exit edge real, or a few episodes wearing a trend's clothes?

The cross exit's continuation trades average +6.574% gross. The question that
decides whether it is robust is CONCENTRATION:

    * How many DISTINCT episodes are those trades?
    * What share of the total comes from the single best trade / pair / week?

With n=90 trades a mean can be dominated by one outlier. This script reports the
honest decomposition and runs leave-one-out, so the edge is either defensible or
exposed.

Usage:
    .venv/bin/python scripts/cross_episode_concentration.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone
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
LOOKBACK = 168
EPISODE_GAP_BARS = 24 * 7      # cross trades in the same pair within 7d = one episode


def main():
    cfg = Cfg(LOOKBACK, 2.0, 0.5, 4.0, 72, exit_mode="cross", cross_frac=0.75)

    cross_tr = []          # only trades that exited via the 'cross' reason
    all_tr = []
    for a, b in LIVE_PAIRS:
        try:
            ts, ca, cb = aligned_close_prices(a, b, interval="1h",
                                              lookback_hours=24 * 260)
        except Exception as e:
            print(f"  skip {a}/{b}: {str(e)[:40]}")
            continue
        if ts is None or len(ts) < LOOKBACK + 80:
            continue
        sp = np.log(np.asarray(ca, float)) - np.log(np.asarray(cb, float))
        tr = simulate(sp, rolling_z(sp, LOOKBACK), cfg)
        for t in tr:
            t = dict(t)
            t["pair"] = f"{a}/{b}"
            try:
                t["entry_dt"] = datetime.fromtimestamp(ts[t["entry_i"]] / 1000,
                                                       tz=timezone.utc)
            except Exception:
                t["entry_dt"] = None
            all_tr.append(t)
            if t["reason"] == "cross":
                cross_tr.append(t)

    if not cross_tr:
        sys.exit("no cross-exit trades")

    print("=" * 96)
    print("CONCENTRATION OF THE CROSS-EXIT EDGE")
    print("=" * 96)
    g = np.asarray([t["gross"] for t in cross_tr]) * 100
    print(f"  cross-exit trades:        {len(g)}")
    print(f"  gross mean:               {g.mean():+.3f}%")
    print(f"  gross median:             {np.median(g):+.3f}%")
    print(f"  trimmed mean (drop 1% tails): {np.mean(np.sort(g)[int(len(g)*0.01):len(g)-int(len(g)*0.01)]):+.3f}%")
    print(f"  best / worst trade:       {g.max():+.2f}% / {g.min():+.2f}%")
    print(f"  share of TOTAL from best single trade:   {g.max()/g.sum()*100:.1f}%")
    top3 = np.sort(g)[-3:].sum() / g.sum() * 100
    print(f"  share of TOTAL from best 3 trades:       {top3:.1f}%")

    # ---- leave-one-out robustness on the cross trades
    print("\n" + "-" * 96)
    print("LEAVE-ONE-OUT: does the mean survive removing the biggest winner?")
    print("-" * 96)
    print(f"  all {len(g)} trades                mean={g.mean():+.3f}%")
    order = np.argsort(g)[::-1]
    for k in (1, 3, 5, 10):
        keep = np.delete(g, order[:k])
        print(f"  drop best {k:<3} trades           mean={keep.mean():+.3f}%   n={len(keep)}")

    # ---- distinct PAIRS
    print("\n" + "-" * 96)
    print("BY PAIR (how many pairs actually contribute?)")
    print("-" * 96)
    by_pair = defaultdict(list)
    for t in cross_tr:
        by_pair[t["pair"]].append(t["gross"] * 100)
    print(f"  {'pair':<22}{'n':>5}{'gross mean%':>13}{'sum%':>10}{'share of total':>16}")
    for p, v in sorted(by_pair.items(), key=lambda kv: -sum(kv[1])):
        print(f"  {p:<22}{len(v):>5}{np.mean(v):>13.3f}{sum(v):>10.2f}"
              f"{sum(v)/g.sum()*100:>15.1f}%")
    print(f"  distinct pairs contributing: {len(by_pair)}")

    # ---- distinct EPISODES (same pair, within EPISODE_GAP_BARS)
    print("\n" + "-" * 96)
    print(f"BY EPISODE (same pair, entries within {EPISODE_GAP_BARS//24}d)")
    print("-" * 96)
    episodes = []
    for p, trs in defaultdict(list).items():
        pass
    grouped = defaultdict(list)
    for t in cross_tr:
        grouped[t["pair"]].append(t)
    for p, trs in grouped.items():
        trs = sorted(trs, key=lambda t: t["entry_i"])
        cur = [trs[0]]
        for t in trs[1:]:
            if t["entry_i"] - cur[-1]["entry_i"] <= EPISODE_GAP_BARS:
                cur.append(t)
            else:
                episodes.append((p, cur))
                cur = [t]
        episodes.append((p, cur))

    ep_pnl = [(p, sum(x["gross"] for x in e), len(e),
               min(x["entry_dt"] for x in e if x["entry_dt"]) if any(x["entry_dt"] for x in e) else None)
              for p, e in episodes]
    ep_pnl.sort(key=lambda x: -x[1])
    total = sum(x[1] for x in ep_pnl)
    print(f"  distinct episodes: {len(ep_pnl)}")
    if ep_pnl and ep_pnl[0][1] > 0:
        print(f"  share of total from best single episode: "
              f"{ep_pnl[0][1]/total*100:.1f}%")
    print(f"  top-3 episode share of total: "
          f"{sum(x[1] for x in ep_pnl[:3])/total*100:.1f}%")
    print(f"\n  {'pair':<22}{'n':>4}{'episode sum%':>15}{'first entry':>22}")
    for p, s, n, dt in ep_pnl[:10]:
        ds = dt.strftime("%Y-%m-%d") if dt else "?"
        print(f"  {p:<22}{n:>4}{s:>15.2f}{ds:>22}")

    # temporal spread
    print("\n" + "-" * 96)
    print("TEMPORAL SPREAD OF CROSS ENTRIES (are they one window?)")
    print("-" * 96)
    dts = [t["entry_dt"] for t in cross_tr if t["entry_dt"]]
    if dts:
        weeks = sorted({d.strftime("%Y-W%V") for d in dts})
        print(f"  entries span {min(dts).strftime('%Y-%m-%d')} .. "
              f"{max(dts).strftime('%Y-%m-%d')}")
        print(f"  distinct calendar weeks with a cross entry: {len(weeks)}")
        byweek = defaultdict(float)
        for t in cross_tr:
            if t["entry_dt"]:
                byweek[t["entry_dt"].strftime("%Y-W%V")] += t["gross"] * 100
        topwk = sorted(byweek.items(), key=lambda kv: -kv[1])[:5]
        print("  top weeks by cross PnL:")
        for w, v in topwk:
            print(f"    {w}  {v:+8.2f}%")
        print(f"  best week share of total: {topwk[0][1]/g.sum()*100:.1f}%")

    print("\n" + "=" * 96)
    print("VERDICT")
    print("=" * 96)
    best_share = g.max() / g.sum() * 100
    ep_share = ep_pnl[0][1] / total * 100 if ep_pnl else 100
    best_week_share = topwk[0][1] / g.sum() * 100 if dts else 100
    print(f"  best-trade share {best_share:.0f}% | best-episode share {ep_share:.0f}% | "
          f"best-week share {best_week_share:.0f}%")
    if best_share > 40 or ep_share > 50:
        print("  => CONCENTRATED. The mean is carried by a small number of episodes.")
        print("     Treat the +6.574% as 'the cross exit can capture large moves',")
        print("     NOT as a reliable per-trade expectation.")
    else:
        print("  => DIFFUSE ENOUGH. No single episode or week dominates the mean, so the")
        print("     cross-exit edge is not an artefact of one lucky continuation.")
    print("\n  Limits: 1h proxy (live is 4h); ~260 days is one regime; episodes defined")
    print(f"  mechanically as same-pair entries within {EPISODE_GAP_BARS//24} days.")


if __name__ == "__main__":
    main()
