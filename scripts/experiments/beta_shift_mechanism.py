#!/usr/bin/env python
"""Is beta_shift predictive, or bookkeeping? (mechanism + threshold sweep)

The exit fires on ~47% of live trades at a median 40h, and the aggregate
valuation is ~+10 bp -- i.e. it saves almost exactly as much as it gives up.
That aggregate is uninformative, so this asks the mechanism question:

  For each trade beta_shift closed, what did the spread do AFTERWARDS?
    - continued to the target   -> the gate cut a winner
    - diverged to the stop      -> the gate avoided a loss
    - neither                   -> it was bookkeeping

Doing this correctly needs the counterfactual path BEYOND the live exit, which
the tape replay provides (we let the trade run under beta_shift OFF and read the
reason/return).

It also separates the rule's two halves, because they fail differently:
    trigger: |d beta| / beta_sd >= threshold
    gate:    posterior expected return <= 0
A rule that only helps via the *gate* is a different (and better) beast than one
that helps via the *trigger*.

Run:  .venv/bin/python scripts/experiments/beta_shift_mechanism.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "experiments"))
sys.stdout.reconfigure(line_buffering=True)

import numpy as np  # noqa: E402

from tape_exit_replay import (  # noqa: E402
    load_json_positions, load_csv, build_entries, attach_paths, replay,
    block_bootstrap, _install_safety, Variant,
)
from src.hyperliquid_bot.phase3.config import load_config  # noqa: E402

THRESHOLDS = (1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0)


def main() -> None:
    _install_safety()
    cfg = load_config(REPO / "config" / "phase3-live.yaml")
    entries, _ = build_entries(load_json_positions(), load_csv("signals.csv"),
                               load_csv("trades.csv"))
    attach_paths(entries, load_csv("signals.csv"))
    dates = [e.date for e in entries]
    print(f"entries: {len(entries)}   entry dates: {len(set(dates))}\n")

    live = Variant("live")
    off = Variant("off", disable_beta_shift=True)

    # ---------------------------------------------------------------- mechanism
    print("=" * 104)
    print("MECHANISM: for each beta_shift exit, what would the trade have done without it?")
    print("=" * 104)
    print(f"  {'pair':<22}{'entry':<12}{'live bp':>9}{'no-bs bp':>10}"
          f"{'would exit':>14}{'effect':>9}")
    cut_win = cut_loss = flat = 0
    eff = []
    for e in entries:
        a = replay(e, cfg, live)
        b = replay(e, cfg, off)
        if a["reason"] != "beta_shift":
            continue
        d = (b["ret"] - a["ret"]) * 1e4
        eff.append(d)
        if d > 5:
            cut_win += 1
        elif d < -5:
            cut_loss += 1
        else:
            flat += 1
        print(f"  {e.pair:<22}{e.date:<12}{a['ret']*1e4:>9.1f}"
              f"{b['ret']*1e4:>10.1f}{b['reason']:>14}{d:>+9.1f}")
    if eff:
        eff = np.asarray(eff)
        print(f"\n  beta_shift exits: {len(eff)}")
        print(f"    cut a winner (would have made more) : {cut_win}")
        print(f"    avoided a loss (would have made less): {cut_loss}")
        print(f"    bookkeeping (|effect| <= 5 bp)       : {flat}")
        print(f"    mean effect of DISABLING: {eff.mean():+.1f} bp   median {np.median(eff):+.1f}")

    # ---------------------------------------------------------------- precision
    print("\n" + "=" * 104)
    print("PRECISION: does the rule fire when the spread is actually deteriorating?")
    print("=" * 104)
    # At the exit bar, where was the spread relative to entry? A useful risk gate
    # should fire on trades that are BEHIND (spread moved against us), not ahead.
    behind = ahead = 0
    for e in entries:
        a = replay(e, cfg, live)
        if a["reason"] != "beta_shift":
            continue
        prog = ((e.entry_spread - a["exit_spread"]) / e.entry_spread
                if e.entry_spread else 0.0)
        if prog < 0:
            behind += 1
        else:
            ahead += 1
    print(f"  fired while spread was BEHIND entry (losing) : {behind}")
    print(f"  fired while spread was AHEAD  (winning)      : {ahead}")
    if behind + ahead:
        print(f"  precision = {behind/(behind+ahead)*100:.0f}%  "
              "(100% would mean it only fires on genuinely deteriorating trades)")

    # ---------------------------------------------------------------- sweep
    print("\n" + "=" * 104)
    print("THRESHOLD SWEEP on the real paths (larger thresholds fire less)")
    print("=" * 104)
    base = np.array([replay(e, cfg, live)["ret"] for e in entries], float)
    print(f"  {'sd>=':>6}{'bs exits':>10}{'mean bp':>10}{'delta':>9}"
          f"{'95% CI':>20}{'P(d<=0)':>9}{'ex-best':>9}")
    rows = []
    for th in THRESHOLDS:
        v = Variant(f"sd{th}", cfg_patch={"beta_shift_min_posterior_sd": th})
        r = np.array([replay(e, cfg, v)["ret"] for e in entries], float)
        reasons = [replay(e, cfg, v)["reason"] for e in entries]
        n = sum(1 for z in reasons if z == "beta_shift")
        d = r - base
        bs = block_bootstrap(d, dates)
        ci = (f"[{bs['lo']*1e4:+.1f}, {bs['hi']*1e4:+.1f}]"
              if np.isfinite(bs["lo"]) else "n/a")
        ok = np.isfinite(r)
        srt = np.sort(r[ok])[::-1]
        exbest = srt[1:].mean() * 1e4 if len(srt) > 1 else np.nan
        print(f"  {th:>6.1f}{n:>10}{np.nanmean(r)*1e4:>10.1f}"
              f"{d.mean()*1e4:>+9.1f}{ci:>20}{bs['p_le_zero']:>9.2f}"
              f"{exbest:>9.1f}")
        rows.append((th, n, d.mean() * 1e4, bs["p_le_zero"]))

    print("\n" + "=" * 104)
    print("READING")
    print("=" * 104)
    print("  A threshold is only worth changing if the delta is positive AND stable")
    print("  as it moves (not a single-value spike), and the precision line above")
    print("  shows the rule firing on genuinely deteriorating trades.")
    print("  CAVEAT: ~12 entry dates, one trade carries ~43% of PnL. Sign and")
    print("  mechanism are the robust parts; magnitudes are indicative.")


if __name__ == "__main__":
    main()
