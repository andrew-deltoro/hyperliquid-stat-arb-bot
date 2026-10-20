#!/usr/bin/env python
"""Build a point-in-time ENTRY dataset: what did each decision actually lead to?

Why
---
Every open question from the 2026-10-14 investigation died on the same rock: the
realized book is 23-33 entries over 9 distinct dates, with one trade carrying
~82% of PnL. That is too thin to settle whether the time stop or the
stationarity gate is mis-calibrated.

The fix is not more analysis, it is MORE LABELLED DECISIONS. The bot already
records every evaluation in reports/signals.csv (timestamp, pair, z, EG_t,
E[r], action, skip_reason). What it does NOT record is what happened next for the
evaluations it declined. This script supplies that.

Design
------
For every (pair, timestamp) evaluation in signals.csv:
  * features as they were visible at decision time (no look-ahead)
  * the decision the bot made (OPEN / SKIP + reason)
  * the FORWARD outcome, computed from price data, in the bot's own convention:
        0.5 * sign * (spread_exit - spread_entry)
    at several horizons, plus a 'to centre' reversion target

The counterfactual outcomes use a SIMPLE, STATED exit rule, not the live exit
policy. That is deliberate: it makes the label reproducible and free of the
very exit-rule questions we are trying to answer.

Honesty constraints
-------------------
* Forward outcomes are LOOK-AHEAD by construction -- they are labels, not
  features. Never use an outcome column as an input to a live decision.
* The exit proxy is not the live exit. Treat magnitudes as indicative.
* Price fetch is cached to disk so re-runs are free and the API is hit once.

Usage:
    .venv/bin/python scripts/build_entry_dataset.py
    .venv/bin/python scripts/build_entry_dataset.py --force-refresh
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
sys.stdout.reconfigure(line_buffering=True)

import numpy as np  # noqa: E402

from simple_pair_screen import LIVE_PAIRS  # noqa: E402

LIVE = Path("/home/hlbot/hlbot-runtime/live")
SIGNALS = LIVE / "reports" / "signals.csv"
TRADES = LIVE / "reports" / "trades.csv"
CACHE = REPO / "reports" / ".entry_ds_cache"
OUT = REPO / "reports" / "entry_dataset.csv"

HORIZONS = {"24h": 24, "72h": 72, "168h": 168}
BAR_HOURS = 1.0


def load_prices(pair: str, force: bool) -> tuple[np.ndarray, np.ndarray] | None:
    """(ts_ms, spread) for a pair, cached. Spread = log(A) - log(B)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / (pair.replace("/", "_").replace(":", "-") + ".csv")
    if f.exists() and not force:
        arr = np.genfromtxt(f, delimiter=",", names=True)
        if arr.size == 0:
            return None
        return np.atleast_1d(arr["ts"]), np.atleast_1d(arr["spread"])
    a, b = pair.split("/")
    if (a, b) not in LIVE_PAIRS:
        return None
    try:
        from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
        ts, ca, cb = aligned_close_prices(a, b, interval="1h",
                                          lookback_hours=24 * 300)
    except Exception as e:
        print(f"    fetch failed {pair}: {str(e)[:50]}")
        return None
    if ts is None or len(ts) < 100:
        return None
    sp = np.log(np.asarray(ca, float)) - np.log(np.asarray(cb, float))
    with open(f, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "spread"])
        for t, s in zip(ts, sp):
            w.writerow([int(t), repr(float(s))])
    return np.asarray(ts, float), sp


def iso_to_ms(iso: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000.0


def forward_outcome(ts, sp, t0_ms, direction, z_entry, spread_entry):
    """Forward outcomes under a stated simple exit rule.

    The rule: hold for the horizon; also report time-to-centre (compression of
    the half-displacement). Return convention matches the replay harness:
        0.5 * sign * (spread_exit - spread_entry)

    DIRECTION: the bot records a real direction only for entries it took
    (LONG_A_SHORT_B / SHORT_A_LONG_B) and writes "NONE" for every SKIP. So for
    skipped rows the natural mean-reversion direction must be DERIVED from the
    sign of z -- short the spread when z>0, long it when z<0. Defaulting to a
    fixed sign would bias every skipped row and invalidate any OPEN-vs-SKIP
    comparison.
    """
    d = (direction or "").strip().upper()
    if d == "LONG_A_SHORT_B":
        sign = 1.0
    elif d == "SHORT_A_LONG_B":
        sign = -1.0
    else:
        sign = -1.0 if z_entry > 0 else 1.0
    i = int(np.searchsorted(ts, t0_ms, side="left"))
    if i <= 0 or i >= len(ts) - 2:
        return {}
    out = {"idx": i}
    for name, hrs in HORIZONS.items():
        j = min(i + int(hrs / BAR_HOURS), len(ts) - 1)
        out[f"ret_{name}"] = 0.5 * sign * (sp[i] if False else sp[j] - sp[i])
    # time to centre: first bar where the half-displacement compresses to <=25%
    target = 0.25 * abs(spread_entry)
    reached = None
    for j in range(i + 1, len(ts)):
        if abs(sp[j]) <= target:
            reached = (j - i) * BAR_HOURS
            break
    out["hours_to_centre"] = reached if reached is not None else ""
    out["ret_to_centre"] = (
        0.5 * sign * (sp[i + int(reached / BAR_HOURS)] - sp[i])
        if reached else ""
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-refresh", action="store_true")
    args = ap.parse_args()

    if not SIGNALS.exists():
        sys.exit(f"missing {SIGNALS}")
    sigs = list(csv.DictReader(open(SIGNALS)))

    # realized outcomes, keyed by (pair, rounded entry timestamp)
    realized = {}
    if TRADES.exists():
        for r in csv.DictReader(open(TRADES)):
            try:
                k = (r["pair"], round(iso_to_ms(r["entry_time_iso"]) / 3_600_000))
                realized[k] = r
            except Exception:
                continue
    print(f"signals: {len(sigs)}   realized trades: {len(realized)}")

    pairs = sorted({r["pair"] for r in sigs if r.get("pair")})
    print(f"pairs: {len(pairs)}  -> fetching/caching 1h series")
    px = {}
    for p in pairs:
        r = load_prices(p, args.force_refresh)
        if r is not None:
            px[p] = r
    print(f"  series available: {len(px)}/{len(pairs)}\n")

    cols = ["timestamp_iso", "pair", "action", "skip_reason", "direction",
            "z", "eg_tstat", "exp_return", "p_revert", "edge", "kappa",
            "half_life_hours", "stress_loss_frac", "spread_sd", "beta",
            "current_spread", "notional",
            "fv_ret_24h", "fv_ret_72h", "fv_ret_168h", "fv_hours_to_centre",
            "fv_ret_to_centre",
            "realized_pnl_usd", "realized_pnl_pct", "realized_exit_reason",
            "realized_holding_hours"]
    rows = []
    matched = 0
    for r in sigs:
        p = r.get("pair")
        if p not in px:
            continue
        ts, sp = px[p]
        try:
            t0 = iso_to_ms(r["timestamp_iso"])
            spread_entry = float(r["current_spread"])
            z_entry = float(r["z"])
        except Exception:
            continue
        fw = forward_outcome(ts, sp, t0, r.get("direction", ""), z_entry, spread_entry)
        if not fw:
            continue
        # realized join (only for entries the bot actually took)
        k = (p, round(t0 / 3_600_000))
        rz = realized.get(k, {})
        if rz:
            matched += 1
        out = {c: r.get(c, "") for c in cols}
        out["fv_ret_24h"] = fw.get("ret_24h", "")
        out["fv_ret_72h"] = fw.get("ret_72h", "")
        out["fv_ret_168h"] = fw.get("ret_168h", "")
        out["fv_hours_to_centre"] = fw.get("hours_to_centre", "")
        out["fv_ret_to_centre"] = fw.get("ret_to_centre", "")
        out["realized_pnl_usd"] = rz.get("pnl_usd", "")
        out["realized_pnl_pct"] = rz.get("pnl_pct", "")
        out["realized_exit_reason"] = rz.get("exit_reason", "")
        out["realized_holding_hours"] = rz.get("elapsed_hours", "")
        rows.append(out)

    if not rows:
        sys.exit("no rows built")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {OUT}  ({len(rows)} rows)")
    print(f"  rows joined to a realized trade: {matched}")

    # ---------------- summary that answers the actual questions
    def f(v):
        try:
            return float(v)
        except Exception:
            return None

    print("\n" + "=" * 96)
    print("WHAT THE DATASET NOW MAKES MEASURABLE")
    print("=" * 96)
    opens = [r for r in rows if r["action"] == "OPEN"]
    skips = [r for r in rows if r["action"] == "SKIP"]
    print(f"  evaluations: {len(rows)}   OPEN {len(opens)}   SKIP {len(skips)}")

    for label, grp in (("OPEN", opens), ("SKIP", skips)):
        v = [f(r["fv_ret_72h"]) for r in grp]
        v = [x for x in v if x is not None]
        if not v:
            continue
        v = np.asarray(v)
        print(f"\n  {label}: forward 72h return under the simple exit proxy")
        print(f"    n={len(v)}  mean={v.mean()*100:+.3f}%  median={np.median(v)*100:+.3f}%"
              f"  win%={100*(v > 0).mean():.0f}")

    # the two contested questions
    print("\n  --- Q1: does the stationarity gate discard good forward outcomes? ---")
    stat_only = [r for r in skips if (r["skip_reason"] or "") == "not_stationary"]
    other_skip = [r for r in skips if (r["skip_reason"] or "") != "not_stationary"]
    for label, grp in (("rejected ONLY by not_stationary", stat_only),
                       ("rejected for other reasons", other_skip),
                       ("all OPEN", opens)):
        v = [f(r["fv_ret_72h"]) for r in grp]
        v = [x for x in v if x is not None]
        if not v:
            continue
        v = np.asarray(v)
        print(f"    {label:<34} n={len(v):>5}  mean={v.mean()*100:+.3f}%"
              f"  median={np.median(v)*100:+.3f}%  win%={100*(v > 0).mean():.0f}")

    # EG_t decile vs forward return
    print("\n  --- Q2: is EG_t monotone in forward return? (bin by EG_t) ---")
    pts = [(f(r["eg_tstat"]), f(r["fv_ret_72h"])) for r in rows]
    pts = [(a, b) for a, b in pts if a is not None and b is not None]
    if len(pts) > 50:
        pts.sort()
        n = len(pts)
        print(f"    {'EG_t quintile':<22}{'n':>6}{'mean fwd 72h':>15}")
        for q in range(5):
            seg = pts[q * n // 5:(q + 1) * n // 5]
            lo, hi = seg[0][0], seg[-1][0]
            m = np.mean([b for _, b in seg])
            print(f"    [{lo:>6.2f}, {hi:>6.2f}]{'':<5}{len(seg):>6}{m*100:>14.3f}%")

    print("\n  NOTE: fv_* columns are LABELS (look-ahead). The exit proxy is a fixed")
    print("  simple rule, not the live exit policy. Use for measurement, never as")
    print("  live inputs.")


if __name__ == "__main__":
    main()
