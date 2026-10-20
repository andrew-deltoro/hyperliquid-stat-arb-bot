#!/usr/bin/env python
"""Pin down the time stop: sweep it on REAL entries AND pooled proxy data.

Why two sources
---------------
The counterfactual showed disabling the time stop gains +43.2 bp/trade. But that
rests on 23 real entries across 9 entry dates, with one trade carrying 43% of
PnL. The day-block CI is [-1.0, +128.6]. That is a signal, not proof.

So sweep the same question on two independent samples:

  A) REAL ENTRIES -- the 23 recorded positions re-stepped through the bot's real
     evaluate_open, varying exit_horizon_mult. Ground truth, tiny n, and it can
     only see the paths that actually happened.

  B) POOLED PROXY -- 15 live pairs, ~208 days of 1h history, fixed cross-exit
     rule, sweeping the hold cap directly. ~800 trades, broad, but a proxy.

If both say "longer holds are better", the finding is robust across sample and
method. If they disagree, the real-entry result was those 9 dates.

Plus C) a MECHANISM diagnostic: for trades the time stop cut, what did the spread
actually do afterwards? That separates "cut a recovering winner" from "cut a
position that was going nowhere".

Usage:
    .venv/bin/python scripts/experiments/time_stop_sweep.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "experiments"))
sys.path.insert(0, str(REPO / "scripts"))
sys.stdout.reconfigure(line_buffering=True)

import numpy as np  # noqa: E402

from tape_exit_replay import (  # noqa: E402
    load_json_positions, load_csv, build_entries, attach_paths, replay,
    block_bootstrap, _install_safety, Variant,
)
from src.hyperliquid_bot.phase3.config import load_config  # noqa: E402
from simple_pair_screen import (  # noqa: E402
    Cfg, LIVE_PAIRS, TAKER_FEE, SLIPPAGE_BP, rolling_z, simulate,
)
from src.hyperliquid_bot.data.ohlcv import aligned_close_prices  # noqa: E402

COST = 2 * 2 * (TAKER_FEE + SLIPPAGE_BP / 10000.0)
LIVE_MULT = 3.33


# --------------------------------------------------------------------- part A
def part_a(cfg, entries):
    print("=" * 104)
    print("A) REAL ENTRIES -- exit_horizon_mult swept through the real evaluator")
    print("=" * 104)
    print(f"  entries: {len(entries)}   distinct entry dates: "
          f"{len(set(e.date for e in entries))}")
    base = np.array([replay(e, cfg, Variant("live"))["ret"] for e in entries], float)
    dates = [e.date for e in entries]
    print(f"  live reference (mult={LIVE_MULT}): mean {np.nanmean(base)*1e4:+.1f} bp\n")
    print(f"  {'mult':>8}{'≈half-lives':>14}{'time_stop n':>13}{'mean bp':>11}"
          f"{'delta bp':>11}{'95% CI':>20}{'P(d<=0)':>9}")

    out = []
    for mult in (0.5, 1.0, 2.0, LIVE_MULT, 5.0, 10.0):
        v = Variant(f"m{mult}", time_stop_mult=mult)
        r = np.array([replay(e, cfg, v)["ret"] for e in entries], float)
        n_ts = sum(1 for e in entries if replay(e, cfg, v)["reason"] == "time_stop")
        d = r - base
        bs = block_bootstrap(d, dates)
        ci = (f"[{bs['lo']*1e4:+.1f}, {bs['hi']*1e4:+.1f}]"
              if np.isfinite(bs["lo"]) else "n/a")
        print(f"  {mult:>8.2f}{mult/LIVE_MULT:>13.2f}x{n_ts:>13}"
              f"{np.nanmean(r)*1e4:>11.1f}{np.nanmean(d)*1e4:>+11.1f}"
              f"{ci:>20}{bs['p_le_zero']:>9.2f}")
        out.append((mult, np.nanmean(r) * 1e4))

    v = Variant("none", disable_time_stop=True)
    r = np.array([replay(e, cfg, v)["ret"] for e in entries], float)
    n_ts = sum(1 for e in entries if replay(e, cfg, v)["reason"] == "time_stop")
    d = r - base
    bs = block_bootstrap(d, dates)
    ci = (f"[{bs['lo']*1e4:+.1f}, {bs['hi']*1e4:+.1f}]"
          if np.isfinite(bs["lo"]) else "n/a")
    print(f"  {'none':>8}{'-':>14}{n_ts:>13}{np.nanmean(r)*1e4:>11.1f}"
          f"{np.nanmean(d)*1e4:>+11.1f}{ci:>20}{bs['p_le_zero']:>9.2f}")
    out.append((float("inf"), np.nanmean(r) * 1e4))
    return base, out


# --------------------------------------------------------------------- part B
def part_b(lookback=168):
    print("\n" + "=" * 104)
    print("B) POOLED PROXY -- hold cap swept over 15 pairs / ~208 days / 1h bars")
    print("=" * 104)
    data = []
    for a, b in LIVE_PAIRS:
        try:
            ts, ca, cb = aligned_close_prices(a, b, interval="1h",
                                              lookback_hours=24 * 260)
        except Exception:
            continue
        if ts is None or len(ts) < lookback + 400:
            continue
        sp = np.log(np.asarray(ca, float)) - np.log(np.asarray(cb, float))
        data.append((f"{a}/{b}", sp, rolling_z(sp, lookback)))
    nbars = int(np.median([len(d[1]) for d in data]))
    print(f"  pairs: {len(data)}   median bars: {nbars}   "
          f"cost: {COST*10000:.0f} bp\n")
    print(f"  {'max_hold':>9}{'≈days@4h':>10}{'n':>7}{'net mean%':>11}{'net total%':>12}"
          f"{'hit%':>7}{'t':>7}{'annSR':>7}")

    results = []
    for mh in (18, 30, 42, 60, 90, 120, 200, int(nbars)):
        cfg = Cfg(lookback, 2.0, 0.5, 4.0, mh, exit_mode="cross", cross_frac=0.75)
        trs = []
        for pair, sp, z in data:
            trs.extend(simulate(sp, z, cfg))
        if not trs:
            continue
        g = np.asarray([t["gross"] for t in trs]) * 100
        net = g - COST * 100
        s = net.std(ddof=1)
        t_ = net.mean() / (s / np.sqrt(len(net))) if s > 0 else 0.0
        med = np.median([t["bars"] for t in trs])
        sr = (net.mean() / s) * np.sqrt((24 * 365) / med) if s > 0 and med > 0 else 0.0
        print(f"  {mh:>9}{mh/6:>10.1f}{len(net):>7}{net.mean():>11.3f}"
              f"{net.sum():>12.1f}{100*(net > 0).mean():>7.1f}{t_:>7.2f}{sr:>7.2f}")
        results.append((mh, net.mean(), net.sum(), t_, med))
    return data, results


# --------------------------------------------------------------------- part C
def part_c(cfg, entries, base):
    print("\n" + "=" * 104)
    print("C) MECHANISM -- what did the time stop actually interrupt?")
    print("=" * 104)
    live = Variant("live")
    none = Variant("none", disable_time_stop=True)
    print(f"  {'pair':<22}{'live bp':>9}{'no time-stop bp':>17}"
          f"{'would exit':>13}{'effect bp':>11}")
    cut_winners = cut_losers = 0
    deltas = []
    for e in entries:
        a = replay(e, cfg, live)
        if a["reason"] != "time_stop":
            continue
        b = replay(e, cfg, none)
        d = (b["ret"] - a["ret"]) * 1e4
        deltas.append(d)
        if d > 0:
            cut_winners += 1
        else:
            cut_losers += 1
        print(f"  {e.pair:<22}{a['ret']*1e4:>9.1f}{b['ret']*1e4:>17.1f}"
              f"{b['reason']:>13}{d:>+11.1f}")
    if deltas:
        print(f"\n  time-stop trades: {len(deltas)}")
        print(f"    cut a recovering winner: {cut_winners}")
        print(f"    cut a going-nowhere/loser: {cut_losers}")
        print(f"    mean effect of removing it: {np.mean(deltas):+.1f} bp")
    return deltas


def main() -> None:
    _install_safety()
    cfg = load_config(REPO / "config" / "phase3-live.yaml")
    entries, _ = build_entries(load_json_positions(), load_csv("signals.csv"),
                              load_csv("trades.csv"))
    attach_paths(entries, load_csv("signals.csv"))

    base, a_out = part_a(cfg, entries)
    data, b_out = part_b()
    part_c(cfg, entries, base)

    # -------------------------------------------------------------- agreement
    print("\n" + "=" * 104)
    print("DO THE TWO SOURCES AGREE?")
    print("=" * 104)
    real_peak = max(a_out, key=lambda x: x[1])
    proxy_peak = max(b_out, key=lambda x: x[2]) if b_out else None
    live_real = [x for x in a_out if x[0] == LIVE_MULT][0][1]
    live_proxy = [x for x in b_out if x[0] == 18]
    print(f"  real entries: best config is mult={real_peak[0]} "
          f"({real_peak[1]:+.1f} bp) vs live {LIVE_MULT} ({live_real:+.1f} bp)"
          f" -> {real_peak[1]-live_real:+.1f} bp")
    if live_proxy:
        print(f"  proxy:        best max_hold={proxy_peak[0]} "
              f"({proxy_peak[1]:+.3f}% net) vs 18 ({live_proxy[0][1]:+.3f}%)"
              f" -> {proxy_peak[1]-live_proxy[0][1]:+.3f}%")
    both_help = (real_peak[1] > live_real) and proxy_peak and \
                (proxy_peak[1] > live_proxy[0][1])
    print()
    if both_help:
        print("  BOTH SOURCES: relaxing the time stop improves the result. The finding")
        print("  survives the change of sample and method.")
    else:
        print("  THE SOURCES DISAGREE, or the proxy does not improve. Treat the")
        print("  real-entry result as specific to those 9 entry dates rather than a")
        print("  general property of the strategy. Do NOT change the live config on")
        print("  the strength of it.")
    print("\n  Limits: the replay can only see paths that actually occurred (a 5x hold")
    print("  may run past the recorded path and resolve as 'open_at_path_end'), and")
    print("  the proxy is a fixed rule rather than the live exit policy.")


if __name__ == "__main__":
    main()
