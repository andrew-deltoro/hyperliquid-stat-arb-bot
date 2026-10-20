#!/usr/bin/env python
"""Which exit rules actually earn their keep? Value them by COUNTERFACTUAL.

Why the naive attribution is wrong
---------------------------------
The audit reports "PnL contribution by exit reason" and finds beta_shift fires
48.5% of exits but contributes only +$7 of +$116. That metric is misleading for
PROTECTIVE rules, because it credits a rule only with where the trade ENDED, not
with where it would have gone without it.

Concrete example from the real book (GMT/MEME):
    live:      exits via beta_shift at +100.9 bp
    without:   falls to divergence_stop at -519.5 bp      -> the rule is +620 bp
Naive attribution gives beta_shift +100.9 and calls it small. Counterfactual
gives it +620 and calls it essential.

The right way to value an exit rule is to DISABLE it and re-step the same real
paths through the real evaluator. That is what this script does.

Usage:
    .venv/bin/python scripts/experiments/exit_rule_valuation.py
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


def main() -> None:
    _install_safety()
    cfg = load_config(REPO / "config" / "phase3-live.yaml")
    entries, _ = build_entries(load_json_positions(), load_csv("signals.csv"),
                              load_csv("trades.csv"))
    attach_paths(entries, load_csv("signals.csv"))
    dates = [e.date for e in entries]

    base_v = Variant("live")
    base = np.array([replay(e, cfg, base_v)["ret"] for e in entries], float)

    # IMPORTANT: disable_* flags must be set, not just the name.
    ablations = [
        ("beta_shift", Variant("beta_shift", disable_beta_shift=True)),
        ("divergence_stop", Variant("divergence_stop", disable_stop=True)),
        ("time_stop", Variant("time_stop", disable_time_stop=True)),
    ]

    print("=" * 104)
    print("EXIT RULE VALUATION BY COUNTERFACTUAL  (real paths, real evaluator)")
    print("=" * 104)
    print(f"  entries: {len(entries)}   distinct entry dates: {len(set(dates))}")
    print(f"  live reference mean: {np.nanmean(base)*1e4:+.1f} bp\n")
    print(f"  {'rule disabled':<20}{'rule fires now':>16}{'mean bp':>10}{'delta bp':>11}"
          f"{'95% CI':>20}{'P(d<=0)':>9}")

    for name, v in ablations:
        rets = np.array([replay(e, cfg, v)["ret"] for e in entries], float)
        n_fires = sum(1 for e in entries
                      if replay(e, cfg, base_v)["reason"] == name)
        d = rets - base
        bs = block_bootstrap(d, dates)
        ci = (f"[{bs['lo']*1e4:+.1f}, {bs['hi']*1e4:+.1f}]"
              if np.isfinite(bs["lo"]) else "n/a")
        print(f"  {name:<20}{n_fires:>16}{np.nanmean(rets)*1e4:>10.1f}"
              f"{np.nanmean(d)*1e4:>+11.1f}{ci:>20}{bs['p_le_zero']:>9.2f}")

    # ---- per-trade breakdown of the beta_shift ablation, which is the contested one
    print("\n" + "=" * 104)
    print("BETA_SHIFT, TRADE BY TRADE (why the naive attribution understates it)")
    print("=" * 104)
    off_v = Variant("off", disable_beta_shift=True)
    print(f"  {'pair':<22}{'live exit':<16}{'without it':<18}{'live bp':>9}"
          f"{'no-bs bp':>10}{'effect bp':>12}")
    saved = given = 0.0
    for e in entries:
        a = replay(e, cfg, base_v)
        if a["reason"] != "beta_shift":
            continue
        b = replay(e, cfg, off_v)
        d = (b["ret"] - a["ret"]) * 1e4
        if d < 0:
            saved += -d
        else:
            given += d
        print(f"  {e.pair:<22}{a['reason']:<16}{b['reason']:<18}{a['ret']*1e4:>9.1f}"
              f"{b['ret']*1e4:>10.1f}{-d:>+12.1f}")
    print(f"\n  profit the rule SAVED by exiting early  : {saved:>+8.1f} bp")
    print(f"  profit given up by exiting early       : {given:>+8.1f} bp")
    print(f"  net value of beta_shift                : {saved - given:>+8.1f} bp")

    print("\n" + "=" * 104)
    print("READING")
    print("=" * 104)
    print("  A rule is worth keeping if disabling it REDUCES PnL (negative delta,")
    print("  P(d<=0) high). The naive 'PnL by exit reason' table cannot see this,")
    print("  because a protective exit shows a small POSITIVE contribution for the")
    print("  trade it rescues, while the loss it prevented is invisible.")
    print("\n  CAVEAT: 23 entries, 9 distinct entry dates, and one trade carries ~43%")
    print("  of PnL. Day-block bootstrap CI over 9 days is wide. Treat magnitudes as")
    print("  indicative; the SIGN and the per-trade mechanism are the robust parts.")


if __name__ == "__main__":
    main()
