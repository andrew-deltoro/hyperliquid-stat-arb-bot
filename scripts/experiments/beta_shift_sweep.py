#!/usr/bin/env python
"""Is the beta_shift exit earning its 48.5% share of exits?

Context: beta_shift fires on 48.5% of realized exits and contributes ~+$7 of
+$116 (audit). It is a two-part rule (positions.py:691-715):

    trigger: |beta_now - beta_entry| / beta_sd >= beta_shift_min_posterior_sd
    gate:    additionally exit only if the posterior expected return <= 0

So raising the threshold makes it fire less; disabling it tests whether the
combination is worth anything at all.

This uses the GROUND-TRUTH replay harness (tape_exit_replay), which steps the
REAL recorded paths through the bot's REAL evaluate_open. Not a proxy.

Usage:
    .venv/bin/python scripts/experiments/beta_shift_sweep.py
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
    block_bootstrap, concentration, _install_safety, Variant,
)
from src.hyperliquid_bot.phase3.config import load_config  # noqa: E402


def main() -> None:
    _install_safety()
    cfg = load_config(REPO / "config" / "phase3-live.yaml")
    entries, _ = build_entries(load_json_positions(), load_csv("signals.csv"),
                              load_csv("trades.csv"))
    attach_paths(entries, load_csv("signals.csv"))
    dates = [e.date for e in entries]
    print(f"entries: {len(entries)}\n")

    variants = [
        Variant("live (sd>=2.0)"),
        Variant("sd>=1.0 (fires more)", cfg_patch={"beta_shift_min_posterior_sd": 1.0}),
        Variant("sd>=1.5", cfg_patch={"beta_shift_min_posterior_sd": 1.5}),
        Variant("sd>=3.0 (fires less)", cfg_patch={"beta_shift_min_posterior_sd": 3.0}),
        Variant("sd>=4.0", cfg_patch={"beta_shift_min_posterior_sd": 4.0}),
        Variant("sd>=6.0", cfg_patch={"beta_shift_min_posterior_sd": 6.0}),
        Variant("beta_shift OFF", disable_beta_shift=True),
    ]

    print("=" * 108)
    print("BETA_SHIFT SWEEP -- real paths through the real evaluate_open")
    print("=" * 108)
    print(f"{'variant':<24}{'mean bp':>10}{'diff bp':>10}{'95% CI':>20}"
          f"{'P(d<=0)':>9}{'best%':>8}{'ex-best bp':>12}")

    base = None
    rows = []
    for v in variants:
        rets = np.array([replay(e, cfg, v)["ret"] for e in entries], dtype=float)
        reasons = [replay(e, cfg, v)["reason"] for e in entries]
        n_bs = sum(1 for r in reasons if r == "beta_shift")
        if base is None:
            base = rets
            diff_txt, ci_txt, p_txt = "", "", ""
        else:
            d = rets - base
            bs = block_bootstrap(d, dates)
            ci_txt = (f"[{bs['lo']*1e4:+.1f}, {bs['hi']*1e4:+.1f}]"
                      if np.isfinite(bs["lo"]) else "n/a")
            p_txt = f"{bs['p_le_zero']:.2f}"
            diff_txt = f"{d.mean()*1e4:+.1f}"
        c = concentration(rets)
        mean = np.nanmean(rets) * 1e4
        finite = rets[np.isfinite(rets)]
        exbest = ((finite.sum() - finite.max()) / len(finite) * 1e4
                  if len(finite) else float("nan"))
        print(f"{v.name:<24}{mean:>10.1f}{diff_txt:>10}{ci_txt:>20}{p_txt:>9}"
              f"{c['best_share']*100:>7.0f}%{exbest:>12.1f}")
        rows.append((v.name, n_bs, mean, c["best_share"]))

    print("\n" + "=" * 108)
    print("BETA_SHIFT EXIT FREQUENCY UNDER EACH THRESHOLD")
    print("=" * 108)
    print(f"  {'variant':<24}{'beta_shift exits':>18}{'share of entries':>19}")
    for name, n_bs, mean, _ in rows:
        print(f"  {name:<24}{n_bs:>18}{100*n_bs/len(entries):>18.0f}%")

    print("\n" + "=" * 108)
    print("READING")
    print("=" * 108)
    print("  Compare 'live' against 'beta_shift OFF'. If turning the rule off changes")
    print("  nothing (diff ~0, P(d<=0) high), the rule is not earning its 48.5% share")
    print("  of exits -- it is bookkeeping, not risk management.")
    print("\n  CAVEAT: only 9 distinct entry dates and 1 trade carries ~43% of PnL.")
    print("  The day-block bootstrap CI will be wide; treat any result as indicative.")
    print("  The exit REASON counts are the robust part of this output -- they do not")
    print("  depend on one trade's magnitude.")


if __name__ == "__main__":
    main()
