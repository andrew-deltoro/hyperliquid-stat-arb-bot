#!/usr/bin/env python3
"""
Exit-policy replay at scale: entries from the bot's OWN rule, exits swept.

The 18-trade replay is hopeless as a basis for choosing cross_exit_frac or
exit_horizon_mult (n=18, two rows the same RSR event). Here we reconstruct
the bot's actual entry decision on the tape and then replay exits with a
proper sample.

Entry rule (mirrors signals.py, minus the NUTS fit which cannot run at
this scale):
  - rolling estimate of beta over `lookback` hours
  - centered spread S = log a - beta log b
  - level z = (S - mean) / sd, and change z = (S_t - S_{t-H}) / (sd_dS*sqrt(H))
  - ENTER when |level z| >= z_min (the bot's live trigger; change-z is
    recorded too so the two can be compared on identical paths)

Exit rules replayed per entry:
  cross(k):  opposite-side level -k * entry_spread
  stop(m):   adverse level m * |entry_spread|
  time(H):   H hours
  trail(t):  give back t of the favourable excursion
and we hold the entry fixed while sweeping (k, m, H, t).

Every configuration is reported on a single locked 50/50 split so nothing is
chosen on the test half. Non-overlapping entries per pair.

Run: .venv/bin/python scripts/experiments/exit_universe_replay.py
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
COST = 2.0 * (0.00045 + 3e-4)
FUNDING_BPS_PER_HOUR = 0.1     # leverage.funding_bps_per_hour in the live config
CYCLE_HOURS = [0, 4, 8, 12, 16, 20]
WINDOW_DAYS = 180

Z_MIN = 1.0                      # the bot's live level-z trigger
ENTRY_SPACING_H = 24             # non-overlapping entries per pair
HORIZON_Z = 24                   # change-z reference horizon

CROSS = [0.5, 1.0, 1.5]
STOP = [2.0, 3.0]
TIME = [72, 120, 168, 240]
TRAIL = [0.25, 0.5]


def candles(coin, s, e, tries=4):
    for k in range(tries):
        try:
            r = requests.post(API, json={"type": "candleSnapshot",
                "req": {"coin": coin, "interval": "1h", "startTime": s, "endTime": e}},
                timeout=40).json()
            if isinstance(r, list) and r:
                return {int(b["t"]): b for b in r}
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.8 * (k + 1))
    return {}


def exit_index(S, s0, sgn, k, m, H, trail=None):
    """First-touch exit index and reason on centered spread path S."""
    adverse = 1.0 if s0 > 0 else -1.0
    cross_lvl = -k * s0
    stop_lvl = s0 + adverse * m * abs(s0)
    n = min(len(S), H + 1)
    best = s0
    idx, reason = n - 1, "time"
    for i in range(1, n):
        si = S[i]
        if trail is not None:
            # favourable excursion tracked as movement toward/past zero
            if (s0 > 0 and si < best) or (s0 < 0 and si > best):
                best = si
            give = abs(s0) * trail
            if (s0 > 0 and si >= best + give) or (s0 < 0 and si <= best - give):
                return i, "trail"
        if (s0 > 0 and si <= cross_lvl) or (s0 < 0 and si >= cross_lvl):
            return i, "cross"
        if (s0 > 0 and si >= stop_lvl) or (s0 < 0 and si <= stop_lvl):
            return i, "stop"
    return idx, reason


def tstat(v):
    v = np.asarray(v, float)
    if len(v) < 2 or v.std(ddof=1) == 0:
        return float("nan")
    return float(v.mean() / (v.std(ddof=1) / math.sqrt(len(v))))


def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=WINDOW_DAYS)
    s_ms, e_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    coins = sorted({c for p in PAIRS for c in p})
    print(f"Fetching {len(coins)} coins x {WINDOW_DAYS}d...")
    px = {}
    for c in coins:
        px[c] = candles(c, s_ms, e_ms)
        time.sleep(0.25)

    # configs: ("cross", k, m, H) and ("trail", t, m, H)
    configs = [("cross", k, m, H, None) for k in CROSS for m in STOP for H in TIME]
    configs += [("trail", 0.0, m, H, t) for t in TRAIL for m in STOP for H in TIME]
    res = {c: [] for c in configs}
    reasons = {c: {} for c in configs}
    n_entries = 0
    mid_ms = s_ms + (e_ms - s_ms) / 2

    # BTC 24h realized vol at each entry, so we can test whether the live
    # max_btc_vol filter already removes the bad regime.
    btc = candles("BTC", s_ms, e_ms)
    btc_idx = sorted(btc)
    btc_ret = {}
    for j in range(1, len(btc_idx)):
        a_, b_ = btc_idx[j - 1], btc_idx[j]
        btc_ret[b_] = math.log(float(btc[b_]["c"]) / float(btc[a_]["c"]))
    def btc_vol_at(ms):
        prior = [t for t in btc_idx if t <= ms]
        if len(prior) < 25:
            return None
        last = prior[-1]
        k = btc_idx.index(last)
        if k < 24:
            return None
        w = [btc_ret[btc_idx[m]] for m in range(k - 23, k + 1) if btc_idx[m] in btc_ret]
        if len(w) < 20:
            return None
        # match scheduler._get_btc_vol_24h: ANNUALIZED, not daily
        return float(np.std(w) * math.sqrt(24 * 365))

    for a, b in PAIRS:
        ta, tb = px.get(a, {}), px.get(b, {})
        ts = sorted(set(ta) & set(tb))
        if len(ts) < LOOKBACK + max(TIME) + HORIZON_Z + 10:
            continue
        idx_of = {t: i for i, t in enumerate(ts)}
        la = np.log(np.array([ta[t]["c"] for t in ts], dtype=float))
        lb = np.log(np.array([tb[t]["c"] for t in ts], dtype=float))
        times = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in ts]

        last_entry = None
        t = times[0].replace(minute=0, second=0, microsecond=0)
        while t <= times[-1]:
            if t.hour not in CYCLE_HOURS:
                t += timedelta(hours=1)
                continue
            i = idx_of.get(int(t.timestamp() * 1000))
            if i is None or i < LOOKBACK or i + max(TIME) + 1 >= len(ts):
                t += timedelta(hours=1)
                continue
            y, x = la[i - LOOKBACK:i], lb[i - LOOKBACK:i]
            xc = x - x.mean()
            beta = float((xc * (y - y.mean())).sum() / (xc * xc).sum())
            if not np.isfinite(beta) or beta <= 0:
                t += timedelta(hours=1)
                continue
            S_full = la - beta * lb
            win = S_full[i - LOOKBACK:i]
            z = (S_full[i] - win.mean()) / (win.std() + 1e-12)
            if abs(z) < Z_MIN:
                t += timedelta(hours=1)
                continue
            if last_entry is not None and (t - last_entry) < timedelta(hours=ENTRY_SPACING_H):
                t += timedelta(hours=1)
                continue

            s0 = float(S_full[i] - win.mean())     # centered entry spread
            if s0 == 0:
                t += timedelta(hours=1)
                continue
            sgn = 1.0 if s0 < 0 else -1.0          # long the cheap leg
            scale = 1.0 / (1.0 + beta)
            # path forward, re-anchored so S[0] = s0 (constant shift)
            shift = S_full[i] - s0
            S = S_full[i:i + max(TIME) + 1] - shift
            last_entry = t
            n_entries += 1

            for cfg in configs:
                kind, k, m, H, tr = cfg
                if kind == "cross":
                    j, reason = exit_index(S, s0, sgn, k, m, H)
                else:
                    j, reason = exit_index(S, s0, sgn, 99.0, m, H, trail=tr)
                net = sgn * (S[j] - s0) * scale - COST
                # carry: funding/borrow accrues per hour held, like the live
                # bot (leverage.py carry_frac_per_bar). Without this the long
                # horizons are flattered.
                net -= (FUNDING_BPS_PER_HOUR / 1e4) * (j + 1)
                res[cfg].append((t.timestamp() * 1000, net))
                reasons[cfg][reason] = reasons[cfg].get(reason, 0) + 1
            t += timedelta(hours=1)

    print(f"entries generated by the bot's level-z>={Z_MIN} rule: {n_entries}\n")
    if not n_entries:
        return

    print(f"{'kind':>6}{'param':>7}{'stop':>6}{'H':>5}{'n':>6}{'mean':>10}"
          f"{'scatter':>10}{'test':>10}{'test_t':>8}  {'cross/stop/time/trail':>22}")
    rows = []
    for cfg in configs:
        kind, k, m, H, tr = cfg
        ev = res[cfg]
        if len(ev) < 30:
            continue
        v = np.array([x[1] for x in ev])
        sc = np.array([x[1] for x in ev if x[0] < mid_ms])
        te = np.array([x[1] for x in ev if x[0] >= mid_ms])
        if len(sc) < 5 or len(te) < 5:
            continue
        param = tr if kind == "trail" else k
        rc = reasons[cfg]
        tag = (f"{rc.get('cross',0)}/{rc.get('stop',0)}/"
               f"{rc.get('time',0)}/{rc.get('trail',0)}")
        rows.append((te.mean(), tstat(te), kind, param, m, H, len(v),
                     v.mean(), sc.mean(), te.mean(), tag))
        print(f"{kind:>6}{param:>7.2f}{m:>6.1f}{H:>5}{len(v):>6}{v.mean():>+10.4%}"
              f"{sc.mean():>+10.4%}{te.mean():>+10.4%}{tstat(te):>8.2f}  {tag:>22}")
    print("\nRanked by TEST-half mean (this ordering is post-hoc; treat the "
          "scatter column as the honest in-sample check):")
    for r in sorted(rows, reverse=True)[:8]:
        print(f"  {r[2]:>6} param={r[3]:<5.2f} stop={r[4]:<4.1f} H={r[5]:<4}"
              f" n={r[6]:<5} scatter={r[8]:+.4%} test={r[9]:+.4%} t={r[1]:+.2f}")

    best = max(rows, key=lambda r: r[0]) if rows else None
    if best:
        print(f"\nbest test-half: {best[2]} param={best[3]} stop={best[4]} "
              f"H={best[5]} test={best[9]:+.4%} (t={best[1]:+.2f}), "
              f"scatter was {best[8]:+.4%}")

    # ---- stability: per-quarter means for representative configs ----
    # A config that only works in the last quarter is regime, not edge.
    print("\nPer-quarter mean (quarters of ~30d), all configs signature:")
    print("  NB: every cross config shows scatter<0 and test>0, which is a "
          "regime split. A real parameter should not flip sign at the split.")
    show = [
        ("cross", 0.75, 3.0, 72, None),   # current live config
        ("cross", 0.50, 3.0, 120, None),  # 'best' by test half
        ("cross", 1.00, 3.0, 120, None),
        ("trail", 0.0, 3.0, 48, 0.50),
    ]
    qms = []
    for qi in range(6):
        lo = s_ms + (e_ms - s_ms) * qi / 6
        hi = s_ms + (e_ms - s_ms) * (qi + 1) / 6
        qms.append((lo, hi))
    print(f"  {'config':<26}" + "".join(
        f"{(datetime.fromtimestamp((a+b)/2/1000, tz=timezone.utc)):%m-%d:>10}"
        for a, b in qms))
    for cfg in show:
        if cfg not in res or len(res[cfg]) < 30:
            continue
        cells = []
        for lo, hi in qms:
            v = [x[1] for x in res[cfg] if lo <= x[0] < hi]
            cells.append(f"{np.mean(v):+.3%}" if len(v) >= 3 else "   --  ")
        kind, k, m, H, tr = cfg
        name = (f"{kind} k={tr if kind=='trail' else k} m={m} H={H}")
        print(f"  {name:<26}" + "".join(f"{c:>10}" for c in cells))

    # ---- block bootstrap CI on the headline config, to respect the
    # overlapping holds of monthly-biased entries ----
    print("\nBlock bootstrap (30d blocks) on cross k=0.5 m=3.0 H=120:")
    cfg = ("cross", 0.5, 3.0, 120, None)
    if cfg in res and len(res[cfg]) >= 50:
        by_day = {}
        for ms, v in res[cfg]:
            d = int(ms // (86400_000))
            by_day.setdefault(d, []).append(v)
        days = sorted(by_day)
        rng = np.random.default_rng(0)
        block = 30
        means = []
        for _ in range(2000):
            picks = rng.integers(0, max(1, len(days) - block), size=max(1, (len(days) + block - 1) // block))
            vals = []
            for p in picks:
                for d in days[p:p + block]:
                    vals.extend(by_day[d])
            if vals:
                means.append(np.mean(vals))
        means = np.array(means)
        lo, hi = np.percentile(means, [2.5, 97.5])
        print(f"  mean={np.mean([v for _, v in res[cfg]]):+.4%}  "
              f"95% CI=[{lo:+.4%}, {hi:+.4%}]  P(mean<=0)={np.mean(means <= 0):.3f}")

    # ---- does the live BTC-vol filter remove the bad regime? ----
    print("\nDoes the live max_btc_vol=0.55 filter remove the bad first month?")
    for cfg in [("cross", 0.5, 3.0, 168, None)]:
        if cfg not in res or not res[cfg]:
            continue
        for label, keep in (("all entries", lambda v: True),
                            ("btc_vol<=0.55", lambda v: v is not None and v <= 0.55),
                            ("btc_vol>0.55 ", lambda v: v is not None and v > 0.55)):
            vals = [(ms, net, btc_vol_at(ms)) for ms, net in res[cfg]]
            sel = [net for ms, net, bv in vals if keep(bv)]
            if len(sel) < 10:
                print(f"  {label}: n={len(sel)} (too few)")
                continue
            arr = np.array(sel)
            print(f"  {label}: n={len(arr):>5} mean={arr.mean():+.4%} "
                  f"t={tstat(arr):+.2f} win={(arr>0).mean():.0%}")
        # per-quarter under the filter
        print("  per-quarter with btc_vol<=0.55:")
        for qi in range(6):
            lo = s_ms + (e_ms - s_ms) * qi / 6
            hi = s_ms + (e_ms - s_ms) * (qi + 1) / 6
            v = [net for ms, net in res[cfg]
                 if lo <= ms < hi and (btc_vol_at(ms) or 9) <= 0.55]
            tag = datetime.fromtimestamp((lo + hi) / 2 / 1000, tz=timezone.utc).strftime("%m-%d")
            print(f"    {tag}: n={len(v):>4} "
                  + (f"mean={np.mean(v):+.4%}" if v else "  --  "))


if __name__ == "__main__":
    main()
