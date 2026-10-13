#!/usr/bin/env python3
"""
Does the BOT's actual entry rule (level-z far from mean) forecast a spread
reversion, or is the edge in the *change-based* event studied elsewhere?

- level-z entry: z = (S_t - mean(S_{t-LOOKBACK:t})) / sd  (what signals.py does)
- compares forward 24h reversion net of costs, non-overlapping per pair
- also reports RSR/TAO and the single-leg momentum subset explicitly

Run: .venv/bin/python scripts/experiments/entry_rule_check.py
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
import yaml

REPO = Path(__file__).resolve().parents[2]
CFG = yaml.safe_load((REPO / "config" / "phase3-live.yaml").read_text())
API = CFG.get("api_url", "https://api.hyperliquid.xyz/info")
PAIRS = [tuple(p) for p in CFG["pairs"]]
LOOKBACK = 720
COST_FRAC = 2.0 * (0.00045 + 3e-4)
CYCLE_HOURS = [0, 4, 8, 12, 16, 20]
WINDOW_DAYS = 60
HORIZON = 24


def candles(coin, s, e):
    try:
        r = requests.post(API, json={"type": "candleSnapshot",
            "req": {"coin": coin, "interval": "1h", "startTime": s, "endTime": e}},
            timeout=30).json()
        return {int(b["t"]): float(b["c"]) for b in r} if isinstance(r, list) else {}
    except Exception:  # noqa: BLE001
        return {}


def cycle_times(a, b):
    t = a.replace(minute=0, second=0, microsecond=0)
    while t <= b:
        if t.hour in CYCLE_HOURS:
            yield t
        t += timedelta(hours=1)


def run(pair, px):
    a, b = pair
    ta, tb = px.get(a, {}), px.get(b, {})
    ts = sorted(set(ta) & set(tb))
    if len(ts) < LOOKBACK + HORIZON * 2:
        return [], []
    idx = {t: i for i, t in enumerate(ts)}
    la = np.log(np.array([ta[t] for t in ts]))
    lb = np.log(np.array([tb[t] for t in ts]))
    times = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in ts]
    lvl, mom = [], []
    for t in cycle_times(times[0], times[-1]):
        i = idx.get(int(t.timestamp() * 1000))
        if i is None or i < LOOKBACK or i + HORIZON >= len(ts):
            continue
        y, x = la[i - LOOKBACK:i], lb[i - LOOKBACK:i]
        xc = x - x.mean()
        beta = float((xc * (y - y.mean())).sum() / (xc * xc).sum())
        if not np.isfinite(beta) or beta <= 0:
            continue
        S = la - beta * lb
        win = S[i - LOOKBACK:i]
        zl = (S[i] - win.mean()) / (win.std() + 1e-12)          # level z
        dS = S[i] - S[i - HORIZON]
        sd_ch = np.std(np.diff(S[i - LOOKBACK:i])) * math.sqrt(HORIZON)
        zm = dS / (sd_ch + 1e-12)                                # change/momentum z
        fwd = S[i + HORIZON] - S[i]
        cost = COST_FRAC * (1.0 + beta) / 2.0
        rev = -np.sign(S[i] - win.mean()) * fwd * 0.5 - cost     # bet on level reversion
        if abs(zl) >= 1.0:
            lvl.append((t, rev, zl))
        if abs(zm) >= 2.0:
            mom.append((t, -np.sign(dS) * fwd * 0.5 - cost, zm))
    return lvl, mom


def nonoverlap(cands):
    out, last = [], None
    for t, r, _ in cands:
        if last is None or (t - last) >= timedelta(hours=HORIZON):
            out.append(r)
            last = t
    return out


def summ(r, label):
    r = np.array(r)
    if len(r) < 2:
        print(f"{label:>30}: n={len(r)}")
        return
    m = float(r.mean())
    t = m / (r.std(ddof=1) / math.sqrt(len(r)))
    print(f"{label:>30}: n={len(r):>3}  mean={m:+.5f}  median={float(np.median(r)):+.5f}  t={t:+.2f}")


def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=WINDOW_DAYS)
    coins = sorted({c for p in PAIRS for c in p})
    px = {c: candles(c, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
          for c in coins}
    time.sleep(0.2)
    lvl_all, mom_all, rsr_lvl, rsr_mom = [], [], [], []
    for pair in PAIRS:
        lv, mo = run(pair, px)
        lvl_all += nonoverlap(lv) and [x[1] for x in lv] or []
        mom_all += [x[1] for x in mo]
        if pair == ("RSR", "TAO"):
            rsr_lvl = [x[1] for x in lv]
            rsr_mom = [x[1] for x in mo]
    # note: lvl/mom above use ALL events (overlapping); non-overlap applied below
    print("=" * 84)
    print("Forward 24h net return/event (per unit gross-leg notional)")
    print("=" * 84)
    summ(lvl_all, "level-z>=1 (bot's entry rule) [overlap]")
    summ(mom_all, "change-z>=2 (momentum event) [overlap]")
    summ(rsr_lvl, "RSR/TAO level-z>=1 [overlap]")
    summ(rsr_mom, "RSR/TAO change-z>=2 [overlap]")
    print("\nNote: the live bot enters on level-z but its exits (cross/target) "
          "and ~40h hold are tuned for a different horizon than the 24h edge above.")


if __name__ == "__main__":
    main()
