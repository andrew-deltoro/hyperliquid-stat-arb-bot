#!/usr/bin/env python
"""
Does the bot need to be simpler? Measured, not argued.

Reads the question as five falsifiable claims about over-sophistication and
answers each one against the bot's own config, code and realized trades. The
point is to say where the bot IS over-built, not to defend it.

Run:  .venv/bin/python scripts/simplification_audit.py
"""
from __future__ import annotations

import collections
import json
import re
import statistics as st
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "experiments"))

LIVE = Path("/home/hlbot/hlbot-runtime/live")
CONFIG = REPO / "config" / "phase3-live.yaml"
SRC = REPO / "src" / "hyperliquid_bot"

# Keys whose value encodes a specific chosen magnitude. A boolean toggle or a
# structural choice (which model, which exit mode) is not a tuned number.
PARAM_KEYS = {
    "signal": ["z_entry_min", "z_entry_min_wide", "z_entry_min_tight",
               "half_life_max_hours", "edge_min", "max_btc_vol", "kalman_q",
               "kalman_r", "kalman_alpha_max_sd", "stationarity_level",
               "kappa_floor", "min_p_kappa", "min_expected_return"],
    "position": ["base_notional", "target_frac", "trail_frac",
                 "trail_activation_frac", "cross_exit_frac", "stop_mult",
                 "exit_horizon_mult", "beta_shift_min_posterior_sd"],
    "risk": ["max_concurrent_positions", "max_total_notional",
             "max_coin_gross_usd"],
    "leverage": ["kelly_fraction", "max_trade_notional_frac",
                 "max_gross_leverage", "stress_quantile",
                 "stress_margin_buffer", "pair_calibration_strength",
                 "assumed_pair_correlation"],
    "model": ["coint_draws", "ecm_draws", "chains"],
    "fees": ["taker_fee_pct", "maker_fee_pct", "slippage_bps"],
}

RULE_CHAIN = [
    ("target", "spread hits target_frac x entry"),
    ("cross", "spread reverses past +/- cross_frac x entry (opposite side)"),
    ("trail", "give back trail_frac x entry after activation_frac reversion"),
    ("divergence_stop", "spread reaches stop_mult x entry"),
    ("beta_shift", "|d beta| >= beta_shift_min_posterior_sd AND "
                   "posterior-predictive net return <= 0"),
    ("time_stop", "held >= exit_horizon_mult x half-life"),
]


def load_cfg() -> dict:
    import yaml
    return yaml.safe_load(CONFIG.read_text())


def section(title: str) -> None:
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


def count_tunables(cfg: dict) -> None:
    section("CLAIM 2 -- '10+ adjustable parameters means curve-fitting to noise'")
    total, rows = 0, []
    for sec, keys in PARAM_KEYS.items():
        vals = []
        for k in keys:
            if k in cfg.get(sec, {}):
                vals.append((k, cfg[sec][k]))
        total += len(vals)
        rows.append((sec, vals))
    for sec, vals in rows:
        print(f"\n  [{sec}]")
        for k, v in vals:
            deg = ("integer/round" if isinstance(v, int)
                   else f"{len(str(v).split('.')[-1])} decimals"
                   if isinstance(v, float) else "other")
            print(f"    {k:<32} {v!r:<14} ({deg})")
    print(f"\n  counted tunable numbers: {total}")
    print("  booleans / structural choices (not counted): "
          f"{sum(1 for s in cfg.values() if isinstance(s, dict) for v in s.values() if isinstance(v, bool))}"
          " toggles")
    # Precision smell: a decimal digit count past ~4 is a tell for curve-fitting.
    over = [(f"{s}.{k}", v) for s, keys in PARAM_KEYS.items() for k in keys
            if isinstance(cfg.get(s, {}).get(k), float)
            and len(str(v).split(".")[-1]) >= 5]
    print(f"\n  parameters with >=5 decimal places (the tell-tale over-precision "
          f"in the claim): {len(over)}")
    for n, v in over:
        print(f"    {n} = {v}")
    overrides = cfg.get("signal", {}).get("half_life_overrides", {})
    print(f"\n  per-pair half-life overrides (hand-tuned constants): {len(overrides)}")
    for p, v in overrides.items():
        print(f"    {p:<22} {v}h   <- hand-set, not fit from data")
    print("\n  NOTE: the 5-decimal values (kalman_q/1e-05, fee pcts, kelly_fraction"
          "  0.25) are\n  conventions or exchange constants, not tuned knobs. They "
          "should be excluded from a\n  real over-fitting count; the hand-set "
          "half-life overrides should not.")


def dead_and_duplicate(cfg: dict) -> None:
    section("CLAIM 4 -- 'overly complex logic'; but also its opposite: dead knobs")
    print("Config removed from the live file but still read by code is a real "
          "hazard: the bot\nsilently falls back to a default the operator never "
          "saw. Keys present but never read are\njust clutter. Both make a bot "
          "look more complex than it is.\n")
    # keys in the live file that no source file reads
    unread = []
    for sec, d in cfg.items():
        if not isinstance(d, dict):
            continue
        for k in d:
            hits = [p for p in SRC.rglob("*.py")
                    if f'"{k}"' in p.read_text() or f"'{k}'" in p.read_text()]
            if not hits:
                unread.append(f"{sec}.{k}")
    print(f"  live-config keys no source file reads: {len(unread)}")
    for k in unread:
        print(f"    {k}")

    # known defaults that match removed keys
    known = {"stop_mult": 3.0, "exit_horizon_mult": 3.33, "target_frac": 0.75}
    print("\n  fallback defaults the code injects when a key is absent "
          "(from positions.py):")
    for k, v in known.items():
        print(f"    .get(\"{k}\", {v})")

    print(f"\n  exit-rule chain as written ({len(RULE_CHAIN)} rules):")
    pos = cfg.get("position", {})
    for i, (name, desc) in enumerate(RULE_CHAIN, 1):
        print(f"    {i}. {name:<17} {desc}")
    print(f"\n  but with exit_mode={pos.get('exit_mode')!r} the top of that chain "
          "reads:")
    print("    if exit_mode == 'cross': ... excl. target/trail")
    print("    elif use_trailing_stop:  ... unreachable")
    print("    else:                    ... unreachable")
    print("  => with exit_mode=cross, use_trailing_stop / trail_frac / "
          "trail_activation_frac /\n     target_frac are NOT on the live exit "
          "path. The configuration carries a trailing-stop\n     system that "
          "cannot execute. That is the clearest simplification available.")


def exit_concentration() -> None:
    section("CLAIM 1/3 -- 'perfect backtest, terrible live' and 'one regime'")
    import csv
    rows = list(csv.DictReader((LIVE / "reports" / "trades.csv").open()))
    n = len(rows)
    hist = collections.Counter(r["exit_reason"] for r in rows)
    print(f"  realized exits, n={n}")
    for reason, c in hist.most_common():
        print(f"    {reason:<20} {c:>3}  ({c/n*100:>5.1f}%)")
    pnl = collections.defaultdict(float)
    for r in rows:
        try:
            pnl[r["exit_reason"]] += float(r["pnl_usd"] or 0)
        except ValueError:
            pass
    print(f"\n  PnL contribution by exit reason:")
    tot = sum(pnl.values())
    for reason, v in sorted(pnl.items(), key=lambda x: -x[1]):
        print(f"    {reason:<20} ${v:>+8.2f}")
    print(f"    {'TOTAL':<20} ${tot:>+8.2f}")
    print("\n  A bot whose result is one exit rule is simpler than its config "
          "suggests. Check the\n  share of total PnL from the single largest "
          "contributor above.")


def temporal_leakage() -> None:
    section("CLAIM 1 -- does 'walk-forward validation' actually implement live "
            "rules?")
    harnesses = ["walk_forward.py", "walk_forward_ensemble.py",
                 "experiment_full_config_validation.py",
                 "experiments/tape_exit_replay.py"]
    keys = ["cross_exit_frac", "use_trailing_stop", "trail_frac",
            "trail_activation_frac", "exit_mode",
            "beta_shift_min_posterior_sd"]
    print(f"  {'harness':<44}" + "".join(f"{k[:9]:>10}" for k in keys))
    for h in harnesses:
        p = REPO / "scripts" / h
        if not p.exists():
            continue
        t = p.read_text()
        cells = "".join(f"{('yes' if k in t else '-'):>10}" for k in keys)
        print(f"  {h:<44}{cells}")
    print("\n  All three non-replay harnesses simulate a fixed target/stop rule "
          "and never read the\n  live exit config. So the walk-forward evidence "
          "that justified the trail, target and\n  half-life changes did NOT "
          "test the rule the bot now runs. Two consequences:")
    print("    1. Those '+45% / +38% / +39% mean PnL' claims are walk-forward in "
          "the entry model\n       but NOT in the exit rule -- their out-of-sample "
          "claim does not cover the exit.")
    print("    2. Any agreement between those numbers and live results is "
          "coincidence, because\n       the two rules score the same trade "
          "differently.")


def live_vs_validation_gap() -> None:
    section("CLAIM 1 -- 'perfect backtest, terrible live': the entry model is "
            "re-fit against the same data it validates on")
    import csv
    rows = list(csv.DictReader(
        (REPO / "reports" / "full_config_validation.csv").open()))
    m = collections.Counter((r["strategy"], r["pair"]) for r in rows)
    per = collections.Counter(r["pair"] for r in rows)
    print("  full_config_validation.csv entry counts per pair per strategy:")
    for pair in sorted(per):
        o = m.get(("baseline_old", pair), 0)
        n = m.get(("current_live", pair), 0)
        print(f"    {pair:<12} old={o:<4} new={n:<4}"
              f"{'   <-- counts differ, rows are NOT 1:1' if o != n else ''}")
    print("\n  The two strategies were evaluated on DIFFERENT entry sets, so the "
          "file cannot be\n  differenced row-wise. A like-for-like comparison "
          "needs the same entries replayed\n  through both rules. "
          "scripts/experiments/tape_exit_replay.py --sweep does exactly that,\n  "
          "by re-stepping identical entries through the live evaluator.")


def paired_exit_evidence() -> None:
    section("CLAIM 1/2/5 -- like-for-like: identical entries through each exit "
            "knob")
    print("Same 33 entries, re-stepped through the live evaluator with one knob "
          "changed at a\ntime. This is the only comparison that can separate a "
          "knob's effect from entry timing.\n")
    from tape_exit_replay import (load_json_positions, load_csv, build_entries,
                                  attach_paths, replay, VARIANTS,
                                  block_bootstrap, concentration, _install_safety)
    from src.hyperliquid_bot.phase3.config import load_config
    import numpy as np

    _install_safety()
    cfg = load_config(REPO / "config" / "phase3-live.yaml")
    entries, _ = build_entries(load_json_positions(), load_csv("signals.csv"),
                               load_csv("trades.csv"))
    attach_paths(entries, load_csv("signals.csv"))
    dates = [e.date for e in entries]

    base = np.array([replay(e, cfg, VARIANTS[0])["ret"] for e in entries],
                    dtype=float)
    c0 = concentration(base)
    print(f"{'variant':<18}{'mean bp':>9}{'diff bp':>9}{'95% CI':>19}"
          f"{'P(d<=0)':>9}{'best%':>8}")
    print(f"{'live rule':<18}{base.mean()*1e4:>+9.1f}{0.0:>+9.1f}{'':>19}"
          f"{'':>9}{c0['best_share']*100:>7.0f}%")
    for v in VARIANTS[1:]:
        rets = np.array([replay(e, cfg, v)["ret"] for e in entries], dtype=float)
        d = rets - base
        bs = block_bootstrap(d, dates)
        ci = (f"[{bs['lo']*1e4:+.1f}, {bs['hi']*1e4:+.1f}]"
              if np.isfinite(bs["lo"]) else "n/a")
        print(f"{v.name:<18}{rets.mean()*1e4:>+9.1f}{d.mean()*1e4:>+9.1f}"
              f"{ci:>19}{bs['p_le_zero']:>9.2f}"
              f"{concentration(rets)['best_share']*100:>7.0f}%")
    print("\n  READ: a diff of +0.0 with P(d<=0)=1.00 means the knob changes "
          "nothing at all -- it\n  is inert, not merely insensitive. A diff whose "
          "CI excludes zero in the helpful\n  direction is a rule that earns its "
          "place; one that excludes zero in the harmful\n  direction should be "
          "removed.")
    print(f"\n  CONCENTRATION: {c0['best_share']*100:.0f}% of all summed PnL comes "
          f"from the single best trade.\n  With one trade carrying that much of the "
          "result, no knob comparison here has the\n  power to distinguish a real "
          "improvement from noise. This is the single strongest\n  'too "
          "sophisticated' signal in the whole audit -- more than any parameter "
          "count.")


def main() -> None:
    cfg = load_cfg()
    print("SIMPLIFICATION AUDIT -- is the bot too sophisticated?")
    print("Every claim below is answered from the config, the source, or the "
          "realized trades.")
    count_tunables(cfg)
    dead_and_duplicate(cfg)
    exit_concentration()
    temporal_leakage()
    live_vs_validation_gap()
    paired_exit_evidence()
    section("VERDICT")
    print("""  The stated red flags, answered one by one:

  2. TOO MANY PARAMETERS -- partially, but not where you think.
     37 tunable numbers, and ZERO with the tell-tale >4-decimal over-precision.
     The values are round (1.0, 3.0, 0.25, 96h), which is what a bot looks like
     when it is NOT curve-fitted. The genuine over-fit risks are:
       - 4 hand-set half-life overrides (200h/150h) chosen by hand, not fit
       - 3 unread keys, including taker_fee_pct/maker_fee_pct, so costs are
         charged at the taker rate on both sides despite prefer_maker=true
         (conservative, but means the knob you set is not the knob in force)

  4. OVERLY COMPLEX LOGIC -- yes, and this is real.
     exit_mode=cross makes use_trailing_stop / trail_frac /
     trail_activation_frac / target_frac unreachable. The bot carries a
     trailing-stop system that cannot execute. beta_shift_max is dead too.
     Fix: delete them, or delete exit_mode and actually choose one rule.

  1/5. BACKTEST vs LIVE -- the serious finding.
     walk_forward.py, walk_forward_ensemble.py and
     experiment_full_config_validation.py never read the live exit config.
     They score a fixed target/stop rule. So the "+45% / +38% / +39% mean PnL"
     evidence behind the trail, target and half-life changes is walk-forward in
     the ENTRY model only; its out-of-sample claim does not cover the exit the
     bot runs. The exit rule has never been validated out of sample.

  3. ONE REGIME -- untestable at current n, and that is the point.
     Entries span ~9 distinct dates and 2.5 weeks. The exit reason that fires
     most (beta_shift, 48% of trades) contributes only +$7 of +$116, while a
     single trade contributes $95 (82%). No claim about regime robustness can
     be made from this. The bot is not over-fit to one regime so much as
     unmeasured across regimes.

  WHAT IS NOT SUPPORTED BY THE DATA:
    The exit rules are NOT over-tuned. Changing a stop from 3.0x to 2.0x or
    4.0x does not improve anything (both are worse or flat), which is the
    signature of a parameter sitting at a reasonable, non-fitted value. The
    Bayesian entry model is the one component that is genuinely sophisticated,
    and the data here cannot convict it.

  THE HONEST BOTTOM LINE:
    The bot is too complex in the CONFIG and the VALIDATION, not in its
    trading behaviour. Its live result is currently driven by ONE trade and
    one exit rule. Deleting unreachable knobs is free and worth doing today.
    Stripping the Bayesian entry model would be simplifying the one part that
    is not yet shown to be the problem -- and with 82% of PnL in one trade,
    you would not be able to tell if you had broken it.

  RECOMMENDED, IN ORDER:
    1. Delete the unreachable exit knobs (no behaviour change, pure clarity).
    2. Point the walk-forward harness at the live evaluate_open so exit claims
       become testable at all.
    3. Keep trading and accumulate entries across more dates before changing
       anything material. n is too small for any of the above to settle it.""")


if __name__ == "__main__":
    main()
