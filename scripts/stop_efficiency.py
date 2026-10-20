#!/usr/bin/env python
"""Is the stop leg bleeding because the stop is in the wrong place?

Context: with the live-mirroring cross exit, 307 of 814 trades exit via `stop` at
a mean of -2.513% gross. That is 38% of all trades hitting the stop. The
question is whether the stop is PROTECTING or DESTROYING value.

In a mean-reversion strategy this is the central design question, because a stop
converts a temporary adverse excursion -- which the strategy believes will
revert -- into a permanent realised loss. The tests here:

  1. COUNTERFACTUAL: for every stopped trade, what would the same trade have
     earned if it had NOT been stopped (held to the normal cross/time exit)?
     Sum the difference. Positive = the stop is destroying money.

  2. WIDTH SWEEP: stop placed at K x the entry deviation. Live uses K=3.0
     (stop_mult). Sweep K and see whether the total is improving or bleeding.

  3. RECOVERY: of stopped trades, what fraction would have ended profitable?

Stops are framed as multiples of the ENTRY deviation to match the live
`stop_mult` semantics (live: stop at 3.0x entry_spread).

Usage:
    .venv/bin/python scripts/stop_efficiency.py
    .venv/bin/python scripts/stop_efficiency.py --interval 1h --lookback 168
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(line_buffering=True)

import numpy as np  # noqa: E402

from simple_pair_screen import (  # noqa: E402
    Cfg, LIVE_PAIRS, TAKER_FEE, SLIPPAGE_BP, rolling_z,
)
from src.hyperliquid_bot.data.ohlcv import aligned_close_prices  # noqa: E402

COST = 2 * 2 * (TAKER_FEE + SLIPPAGE_BP / 10000.0)      # 30 bp round trip


def find_exit(i, spread, z, cfg, stop_mult, cross_frac):
    """Return (exit_i, reason). stop_mult=None disables the stop."""
    side = -1 if z[i] > 0 else 1
    zi = z[i]
    n = len(spread)
    last = min(i + cfg.max_hold, n - 1)
    for j in range(i + 1, last + 1):
        zj = z[j]
        if np.isnan(zj):
            continue
        # cross exit: spread travelled to the opposite side past the centre
        if side == -1 and zj <= -cross_frac * abs(zi):
            return j, "cross"
        if side == 1 and zj >= cross_frac * abs(zi):
            return j, "cross"
        # stop: adverse excursion beyond stop_mult x the entry deviation
        if stop_mult is not None:
            adverse = (zj > zi) if side == -1 else (zj < zi)
            if adverse and abs(zj) >= stop_mult * abs(zi):
                return j, "stop"
    return last, "time"


def run_pair(spread, z, cfg, stop_mult, cross_frac):
    """One position at a time. Returns trades including a no-stop counterfactual."""
    n = len(spread)
    trades = []
    i = cfg.lookback
    while i < n - 1:
        zi = z[i]
        if np.isnan(zi) or abs(zi) < cfg.z_entry:
            i += 1
            continue
        side = -1 if zi > 0 else 1
        entry = spread[i]

        ex_i, reason = find_exit(i, spread, z, cfg, stop_mult, cross_frac)
        actual = side * (spread[ex_i] - entry)

        # counterfactual: same entry, stop disabled
        cf_i, cf_reason = find_exit(i, spread, z, cfg, None, cross_frac)
        cf = side * (spread[cf_i] - entry)

        trades.append({
            "entry_i": i, "exit_i": ex_i, "reason": reason,
            "side": side, "bars": ex_i - i,
            "gross": actual, "cf_gross": cf, "cf_reason": cf_reason,
        })
        i = ex_i + 1
    return trades


def total_net(trades):
    if not trades:
        return 0.0, 0.0, 0
    g = np.asarray([t["gross"] for t in trades]) * 100
    net = g - COST * 100
    return float(net.mean()), float(net.sum()), len(net)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--hours", type=int, default=24 * 260)
    ap.add_argument("--lookback", type=int, default=168)
    ap.add_argument("--z-entry", type=float, default=2.0)
    ap.add_argument("--max-hold", type=int, default=72)
    ap.add_argument("--cross-frac", type=float, default=0.75)
    args = ap.parse_args()

    cfg = Cfg(args.lookback, args.z_entry, 0.5, 4.0, args.max_hold,
              exit_mode="cross", cross_frac=args.cross_frac)

    # load once, reuse for every sweep point
    data = []
    for a, b in LIVE_PAIRS:
        try:
            ts, ca, cb = aligned_close_prices(a, b, interval=args.interval,
                                              lookback_hours=args.hours)
        except Exception as e:
            print(f"  skip {a}/{b}: {str(e)[:40]}")
            continue
        if ts is None or len(ts) < args.lookback + 80:
            continue
        sp = np.log(np.asarray(ca, float)) - np.log(np.asarray(cb, float))
        data.append((f"{a}/{b}", sp, rolling_z(sp, args.lookback)))
    print(f"loaded {len(data)} pairs\n")

    # ------------------------------------------------ 1. live stop, w/ counterfactual
    live_k = 3.0                       # live stop_mult
    all_tr = []
    for pair, sp, z in data:
        for t in run_pair(sp, z, cfg, live_k, args.cross_frac):
            t = dict(t)
            t["pair"] = pair
            all_tr.append(t)

    stops = [t for t in all_tr if t["reason"] == "stop"]
    print("=" * 100)
    print(f"1. COUNTERFACTUAL AT THE LIVE STOP (K={live_k:.1f} x entry deviation)")
    print("=" * 100)
    m, s, n = total_net(all_tr)
    print(f"  strategy as-is:            n={n:>5}  net mean={m:+.3f}%  net total={s:+.1f}%")
    print(f"  trades that hit the stop:  n={len(stops):>5}  "
          f"({100*len(stops)/len(all_tr):.1f}% of all trades)")
    if stops:
        gs = np.asarray([t["gross"] for t in stops]) * 100
        cfs = np.asarray([t["cf_gross"] for t in stops]) * 100
        print(f"    actual mean on stopped:  {gs.mean():+.3f}%")
        print(f"    if NOT stopped (cf):     {cfs.mean():+.3f}%")
        diff = cfs - gs
        print(f"    mean difference:         {diff.mean():+.3f}%  "
              f"(net of cost, same trade count)")
        print(f"    TOTAL the stop gave up:  {diff.sum():+.1f}%  "
              f"across {len(stops)} stopped trades")
        print(f"    of stopped trades, would have ended PROFITABLE: "
              f"{100*(cfs - COST*100 > 0).mean():.1f}%")
        print(f"    of stopped trades, the stop SAVED money:        "
              f"{100*(cfs < gs).mean():.1f}%")
        rec = defaultdict(int)
        for t in stops:
            rec[t["cf_reason"]] += 1
        print("    counterfactual exit reason for stopped trades:")
        for r, c in sorted(rec.items(), key=lambda kv: -kv[1]):
            print(f"      {r:<8} {c:>5}  ({100*c/len(stops):.1f}%)")

    # ------------------------------------------------------------- 2. width sweep
    print("\n" + "=" * 100)
    print("2. STOP WIDTH SWEEP  (stop at K x entry deviation; live uses K=3.0)")
    print("=" * 100)
    print(f"  {'K':>6}{'n':>7}{'stop n':>8}{'stop%':>7}{'net mean%':>12}{'net total%':>13}"
          f"{'hit%':>7}{'t':>7}")
    rows = []
    for k in (1.5, 2.0, 2.5, 3.0, 4.0, 6.0, None):
        trs = []
        for pair, sp, z in data:
            trs.extend(run_pair(sp, z, cfg, k, args.cross_frac))
        g = np.asarray([t["gross"] for t in trs]) * 100
        net = g - COST * 100
        nst = sum(1 for t in trs if t["reason"] == "stop")
        t_ = net.mean() / (net.std(ddof=1) / np.sqrt(len(net))) if net.std(ddof=1) > 0 else 0
        label = "none" if k is None else f"{k:.1f}"
        print(f"  {label:>6}{len(net):>7}{nst:>8}{100*nst/len(net):>6.0f}%"
              f"{net.mean():>12.3f}{net.sum():>13.1f}{100*(net > 0).mean():>7.1f}{t_:>7.2f}")
        rows.append((label, net.mean(), net.sum(), t_))

    best = max(rows, key=lambda r: r[2])
    print(f"\n  best by net TOTAL: K={best[0]}  (net total {best[2]:+.1f}%, mean {best[1]:+.3f}%)")

    # ------------------------------------------------------------- 3. by pair
    print("\n" + "=" * 100)
    print("3. WHERE THE STOPS ACTUALLY HAPPEN, with counterfactual (K=3.0)")
    print("=" * 100)
    byp = defaultdict(list)
    for t in all_tr:
        byp[t["pair"]].append(t)
    print(f"  {'pair':<22}{'n':>5}{'stops':>7}{'stop%':>7}{'actual%':>10}{'cf%':>10}{'diff%':>10}")
    for p, trs in sorted(byp.items(), key=lambda kv: -len([t for t in kv[1] if t["reason"] == "stop"])):
        st = [t for t in trs if t["reason"] == "stop"]
        if not st:
            print(f"  {p:<22}{len(trs):>5}{0:>7}{0:>6.0f}%")
            continue
        a = np.mean([t["gross"] for t in st]) * 100
        c = np.mean([t["cf_gross"] for t in st]) * 100
        print(f"  {p:<22}{len(trs):>5}{len(st):>7}{100*len(st)/len(trs):>6.0f}%"
              f"{a:>10.2f}{c:>10.2f}{c-a:>10.2f}")

    print("\n" + "=" * 100)
    print("VERDICT")
    print("=" * 100)
    if stops:
        diff = cfs - gs
        if diff.mean() > 0.2:
            print(f"  The stop is DESTROYING value: stopped trades would have earned")
            print(f"  {diff.mean():+.2f}% more each without it ({diff.sum():+.0f}% total).")
            print("  For a mean-reversion strategy that is the expected failure mode: the")
            print("  stop realises losses on excursions the model expects to revert.")
        elif diff.mean() < -0.2:
            print(f"  The stop is PROTECTING value: it saves {abs(diff.mean()):.2f}% per")
            print("  stopped trade. Keep it.")
        else:
            print(f"  The stop is roughly PnL-NEUTRAL ({diff.mean():+.2f}% per stopped trade).")
            print("  Its value is risk control, not return.")
    print("\n  Limits: 1h proxy; `stop` here means an adverse z excursion, which is the")
    print("  same SHAPE as the live divergence_stop but not bit-identical to it.")


if __name__ == "__main__":
    main()
