#!/usr/bin/env python3
"""
Exit-geometry replay on the trades we actually took.

The configured geometry is target = cross_exit_frac * entry_spread (0.75)
against stop = stop_mult * entry_spread (3.0), i.e. reward:risk = 1:4. At
0.25R reward with a 1R stop, breakeven needs a win rate of 0.8 -- we have
0.67. This script asks: would a different geometry have done better on the
same 18 entries?

Method: for each historical trade we reconstruct the centered-spread path
from public candles (entry beta and entry level anchored to the recorded
entry_spread), then replay exit rules on that path. Entry timing is held
fixed, so this isolates the exit rule.

  centered S_t = log(a_t) - beta*log(b_t) - C
  C chosen so S at entry == recorded entry_spread

Rules replayed per trade:
  cross(k):  exit when S reaches -k * S_entry   (opposite side)
  stop(m):   exit when S reaches +m * |S_entry| against us
  time(H):   exit after H hours
and the triple is taken first-touch. Cost = journal's round trip.

Run: .venv/bin/python scripts/experiments/exit_geometry_replay.py
"""
from __future__ import annotations

import csv
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

API = "https://api.hyperliquid.xyz/info"
FEE, SLIP = 0.00045, 3e-4
COST = 2.0 * (FEE + SLIP)
TRADES = [
    Path("/home/hlbot/hlbot-runtime/live/reports/trades.csv"),
    Path("reports/trades.csv"),
]
MAX_DAYS = 12
CROSS = [0.5, 0.75, 1.0, 1.5, 2.0]
STOP = [1.5, 2.0, 3.0]
HOURS = [48, 72, 120]


def candles(coin, s, e, tries=4):
    for k in range(tries):
        try:
            r = requests.post(API, json={"type": "candleSnapshot",
                "req": {"coin": coin, "interval": "1h", "startTime": s, "endTime": e}},
                timeout=40).json()
            if isinstance(r, list) and r:
                return {int(b["t"]): float(b["c"]) for b in r}
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.8 * (k + 1))
    return {}


def replay(S, entry, k, m, H, long_a):
    """First-touch exit on centered spread path S (index 0 = entry bar)."""
    s0 = entry
    tgt = -k * s0
    stop = s0 + m * abs(s0) * (1 if s0 > 0 else -1)
    # adverse direction is away from zero on the entry side
    adverse_sign = 1.0 if s0 > 0 else -1.0
    stop_level = s0 + adverse_sign * m * abs(s0)
    n = min(len(S), H + 1)
    idx = n - 1
    reason = "time"
    for i in range(1, n):
        si = S[i]
        # reversion target (cross, opposite side)
        if (s0 > 0 and si <= tgt) or (s0 < 0 and si >= tgt):
            idx, reason = i, "cross"
            break
        # adverse stop
        if (s0 > 0 and si >= stop_level) or (s0 < 0 and si <= stop_level):
            idx, reason = i, "stop"
            break
    return idx, reason


def main() -> None:
    f = next((p for p in TRADES if p.exists()), None)
    if f is None:
        print("no trades.csv")
        return
    rows = [r for r in csv.DictReader(open(f)) if r.get("entry_price_a")]
    print(f"replaying {len(rows)} trades from {f}\n")

    # results[k][m][H] = list of per-unit-notional net returns
    res = {(k, m, H): [] for k in CROSS for m in STOP for H in HOURS}
    actual = []
    reasons = {(k, m, H): {} for k in CROSS for m in STOP for H in HOURS}
    used = 0
    paths = []          # (entry_spread, S array, sign, beta) per replayed trade
    is_rsr = []

    for r in rows:
        a, b = r["pair"].split("/", 1)
        try:
            beta = float(r["beta_entry"])
            ea, eb = float(r["entry_price_a"]), float(r["entry_price_b"])
            s_ent = float(r["entry_spread"])
            t0 = datetime.fromisoformat(r["entry_time_iso"])
            sgn = 1.0 if r["direction"] == "LONG_A_SHORT_B" else -1.0
        except (KeyError, ValueError, TypeError):
            continue
        if not math.isfinite(beta) or beta <= 0:
            continue
        s_ms = int(t0.timestamp() * 1000)
        e_ms = int((t0 + timedelta(days=MAX_DAYS)).timestamp() * 1000)
        ca = candles(a, s_ms, e_ms)
        cb = candles(b, s_ms, e_ms)
        ts = sorted(set(ca) & set(cb))
        if len(ts) < 10:
            print(f"  ! {r['pair']}: only {len(ts)} bars, skipped")
            continue
        # anchor the constant so S at the entry bar equals recorded entry_spread
        C = math.log(ea) - beta * math.log(eb) - s_ent
        S = np.array([math.log(ca[t]) - beta * math.log(cb[t]) - C for t in ts])
        used += 1
        paths.append((s_ent, S, sgn, beta))
        is_rsr.append("RSR" in r["pair"])

        # weighting: legs are N/(1+beta) and beta*N/(1+beta), so
        # gross per unit notional = s * dS / (1 + beta)   [not s*dS/2]
        scale = 1.0 / (1.0 + beta)

        for k in CROSS:
            for m in STOP:
                for H in HOURS:
                    idx, reason = replay(S, s_ent, k, m, H, sgn)
                    dS = S[idx] - s_ent
                    net = sgn * dS * scale - COST
                    res[(k, m, H)].append(net)
                    reasons[(k, m, H)][reason] = reasons[(k, m, H)].get(reason, 0) + 1

        try:
            actual.append(float(r["pnl_usd"]) / float(r["notional_usd"]))
        except (KeyError, ValueError, ZeroDivisionError):
            pass

    _is_rsr_arr = np.array(is_rsr) if is_rsr else np.array([], dtype=bool)
    print(f"trades replayed: {used}, actual recorded: {len(actual)}\n")
    if actual:
        av = np.array(actual)
        print(f"ACTUAL realized  : n={len(av)}  mean={av.mean():+.4%}  "
              f"median={np.median(av):+.4%}  win={(av>0).mean():.0%}")
        if len(_is_rsr_arr) == len(av):
            print(f"ACTUAL ex-RSR    : n={int(np.sum(~_is_rsr_arr))}  "
                  f"mean={av[~_is_rsr_arr].mean():+.4%}")
    print()

    # per-trade replay detail for the headline config
    print("Per-trade replay (cross=0.75, stop=3.0, H=72):")
    print(f"{'pair':<20}{'entry_s':>9}{'dS':>9}{'net':>9}{'reason':>8}")
    print("-" * 64)
    for i, r in enumerate(rows):
        if i >= len(paths):
            break
        a, b = r["pair"].split("/", 1)
        s_ent, S, sgn, beta = paths[i]
        idx, reason = replay(S, s_ent, 0.75, 3.0, 72, sgn)
        net = sgn * (S[idx] - s_ent) / (1.0 + beta) - COST
        print(f"{r['pair']:<20}{s_ent:>9.4f}{S[idx]-s_ent:>9.4f}{net:>+9.4%}{reason:>8}")
    print()

    print(f"{'cross':>6}{'stop':>6}{'H':>5}{'n':>5}{'mean':>10}{'mean_exRSR':>12}"
          f"{'t':>7}{'win':>6}{'tot':>9}  {'cross/stop/time':>18}")
    best = None
    is_rsr_arr = np.array(is_rsr) if is_rsr else np.array([], dtype=bool)
    n_each = len(is_rsr_arr)

    def tstat(v):
        v = np.asarray(v, float)
        if len(v) < 2 or v.std(ddof=1) == 0:
            return float("nan")
        return float(v.mean() / (v.std(ddof=1) / math.sqrt(len(v))))

    for H in HOURS:
        for m in STOP:
            for k in CROSS:
                v = np.array(res[(k, m, H)])
                if len(v) < 3:
                    continue
                totR = v.sum()
                if best is None or totR > best[0]:
                    best = (totR, k, m, H, v.mean())
                rc = reasons[(k, m, H)]
                tag = f"{rc.get('cross',0)}/{rc.get('stop',0)}/{rc.get('time',0)}"
                ex = v[~is_rsr_arr] if len(is_rsr_arr) == len(v) else v
                exm = f"{ex.mean():>+12.4%}" if len(ex) else " " * 12
                print(f"{k:>6.2f}{m:>6.1f}{H:>5}{len(v):>5}{v.mean():>+10.4%}"
                      f"{exm}{tstat(v):>7.2f}{(v>0).mean():>6.0%}{totR:>9.3f}  {tag:>18}")
        print()

    if best:
        print(f"best total return: cross={best[1]} stop={best[2]} H={best[3]} "
              f"mean={best[4]:+.4%} tot={best[0]:+.3f}")
    print(f"\nCAUTION: n={n_each} trades, two of them the SAME RSR/TAO event. "
          f"Ex-RSR means are the honest read; none of these t-stats are "
          f"significant at this sample size.")
    print("\nNote: this replays only the exits on entries we actually took; it "
          "does not fix the entry rule, which is separately unvalidated.")


if __name__ == "__main__":
    main()
