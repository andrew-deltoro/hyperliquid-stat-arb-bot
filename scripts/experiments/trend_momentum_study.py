#!/usr/bin/env python3
"""
Daily-cadence trend detector: the STARKNET/TIA class, not the 1h shock class.

The user's 7d leaderboard (STARKNET +65.36%, TIA +19.62%, AERO +11.11%) is a
different animal from the 1h shocks studied in momentum_event_study.py. This
tests the obvious question: is weekly cross-sectional momentum tradeable on
our own pair universe?

Design (deliberately simple, so it can be falsified):
  Every day, rank the universe by trailing 7d return.
  Buy the top decile (or top N), hold H days, measure forward return.
  Compare against the universe mean and against the bottom decile.
  Locked 50/50 split; non-overlapping holds.

Also tests the reversal variant (buy bottom decile) since crypto sometimes
reverts at the weekly scale.

Run: .venv/bin/python scripts/experiments/trend_momentum_study.py
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
COINS = sorted({c for p in PAIRS for c in p} | {
    "BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK", "ARB", "OP", "SUI", "APT",
    "INJ", "TIA", "SEI", "WLD", "AAVE", "COMP", "UNI", "MKR", "CRV", "SNX",
    "DYDX", "RSR", "UMA", "PYTH", "LTC", "ETC", "NEO", "MEME", "GMT", "ORDI",
    "TAO", "FET", "RENDER", "AERO", "STX", "STRK", "ATOM", "KCS", "FF", "XDC",
})
WINDOW_DAYS = 365
LOOKBACK_D = 7
HOLDS = [7, 14, 21]
DECILE = 0.10
COST = 2.0 * (0.00045 + 3e-4)


def daily(coin, s, e, tries=4):
    for k in range(tries):
        try:
            r = requests.post(API, json={"type": "candleSnapshot",
                "req": {"coin": coin, "interval": "1d", "startTime": s, "endTime": e}},
                timeout=40).json()
            if isinstance(r, list) and r:
                return {int(b["t"]): float(b["c"]) for b in r}
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.8 * (k + 1))
    return {}


def tstat(v):
    v = np.asarray(v, float)
    if len(v) < 3 or v.std(ddof=1) == 0:
        return float("nan")
    return float(v.mean() / (v.std(ddof=1) / math.sqrt(len(v))))


def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=WINDOW_DAYS)
    s_ms, e_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    print(f"Fetching {len(COINS)} coins x {WINDOW_DAYS}d daily...")
    px = {}
    for c in COINS:
        d = daily(c, s_ms, e_ms)
        time.sleep(0.2)
        if len(d) > LOOKBACK_D + max(HOLDS) + 5:
            px[c] = d
    print(f"usable coins: {len(px)}\n")
    if len(px) < 10:
        return

    # common daily timeline
    all_ts = sorted(set.intersection(*[set(d) for d in px.values()]))
    if len(all_ts) < LOOKBACK_D + max(HOLDS) + 5:
        print("insufficient common timeline")
        return
    idx = {t: i for i, t in enumerate(all_ts)}
    M = {c: np.array([px[c].get(t, np.nan) for t in all_ts], float) for c in px}

    mid = len(all_ts) // 2
    for H in HOLDS:
        recs = []   # (ts_index, top_ret, bot_ret, uni_ret)
        i = LOOKBACK_D
        while i + H < len(all_ts):
            pas = {}
            for c in px:
                p0, p1 = M[c][i - LOOKBACK_D], M[c][i]
                if p0 > 0 and p1 > 0 and np.isfinite(p0) and np.isfinite(p1):
                    pas[c] = p1 / p0 - 1.0
            if len(pas) < 10:
                i += H
                continue
            ranked = sorted(pas.items(), key=lambda kv: -kv[1])
            k = max(1, int(len(ranked) * DECILE))
            top, bot = ranked[:k], ranked[-k:]
            def fwd(sel):
                vals = []
                for c, _ in sel:
                    p0, p1 = M[c][i], M[c][i + H]
                    if np.isfinite(p0) and np.isfinite(p1) and p0 > 0:
                        vals.append(p1 / p0 - 1.0)
                return float(np.mean(vals)) if vals else np.nan
            uni = float(np.mean([M[c][i + H] / M[c][i] - 1.0
                                 for c in pas
                                 if np.isfinite(M[c][i]) and M[c][i] > 0]))
            recs.append((i, fwd(top), fwd(bot), uni))
            i += H

        if len(recs) < 6:
            continue
        top = np.array([r[1] for r in recs]) - COST
        bot = np.array([r[2] for r in recs]) - COST
        uni = np.array([r[3] for r in recs])
        sc = np.array([i for i, *_ in recs]) < mid

        print(f"--- hold {H}d, top/bottom decile of trailing {LOOKBACK_D}d ---")
        print(f"  MOMENTUM buy-top: n={len(top)} scatter={top[sc].mean():+.3%} "
              f"test={top[~sc].mean():+.3%} t={tstat(top[~sc]):+.2f} "
              f"(excess vs uni {np.mean(top-uni):+.3%})")
        print(f"  REVERSAL buy-bot: n={len(bot)} scatter={bot[sc].mean():+.3%} "
              f"test={bot[~sc].mean():+.3%} t={tstat(bot[~sc]):+.2f} "
              f"(excess vs uni {np.mean(bot-uni):+.3%})")
        print(f"  universe mean   : {uni.mean():+.3%}\n")

    # the user's actual screenshot names: what happened AFTER big 7d runs?
    print("=" * 78)
    print("Event study: after a coin is up >=20% over 7d, what does the next 7d do?")
    print("=" * 78)
    ev = []
    for c in px:
        for i in range(LOOKBACK_D, len(all_ts) - 7):
            p0, p1 = M[c][i - LOOKBACK_D], M[c][i]
            p2 = M[c][i + 7]
            if not all(np.isfinite(v) and v > 0 for v in (p0, p1, p2)):
                continue
            r7 = p1 / p0 - 1.0
            if r7 < 0.20:
                continue
            ev.append((i, r7, p2 / p1 - 1.0))
    if ev:
        r7 = np.array([e[1] for e in ev])
        fwd = np.array([e[2] for e in ev]) - COST
        print(f"events (7d >= +20%): n={len(ev)}  [OVERLAPPING - t is inflated]")
        print(f"  next 7d mean = {fwd.mean():+.3%}  t={tstat(fwd):+.2f}  "
              f"P(up)={(fwd>0).mean():.0%}")
        for lo, hi in ((0.20, 0.35), (0.35, 0.60), (0.60, 99)):
            sel = fwd[(r7 >= lo) & (r7 < hi)]
            if len(sel) >= 3:
                print(f"  after {lo:.0%}-{hi:.0%}: n={len(sel)} mean={sel.mean():+.3%} "
                      f"t={tstat(sel):+.2f}")

        # ---- NON-OVERLAPPING: one event per coin per 7d window ----
        print("\nNON-OVERLAPPING (>=7d between events per coin):")
        by_coin_ev: dict[str, list] = {}
        for i, run, fw in ev:
            by_coin_ev.setdefault("", []).append((i, run, fw))
        # regroup by coin using M keys via the stored tuple list below
        print("  (recomputed from per-coin greedy selection)")
        non = []
        for c in px:
            last = -10**9
            for i in range(LOOKBACK_D, len(all_ts) - 7):
                p0, p1, p2 = M[c][i - LOOKBACK_D], M[c][i], M[c][i + 7]
                if not all(np.isfinite(v) and v > 0 for v in (p0, p1, p2)):
                    continue
                r7 = p1 / p0 - 1.0
                if r7 < 0.20 or (i - last) < 7:
                    continue
                # market-relative: subtract the cross-sectional mean move
                uni7 = float(np.nanmean([M[x][i + 7] / M[x][i] - 1.0
                                         for x in px if np.isfinite(M[x][i]) and M[x][i] > 0]))
                non.append((i, r7, p2 / p1 - 1.0 - COST, p2 / p1 - 1.0 - uni7))
                last = i
        if non:
            nf = np.array([x[2] for x in non])
            nx = np.array([x[3] for x in non])
            print(f"  n={len(non)}  absolute next7d={nf.mean():+.3%} t={tstat(nf):+.2f} "
                  f"P(up)={(nf>0).mean():.0%}")
            print(f"  RELATIVE to universe (market-neutral) = {nx.mean():+.3%} "
                  f"t={tstat(nx):+.2f} P(up)={(nx>0).mean():.0%}")
            # block bootstrap over 30d calendar blocks
            rng = np.random.default_rng(0)
            days = {}
            for i, _, a, x in non:
                days.setdefault(all_ts[i] // 86_400_000 // 30, []).append(x)
            keys = sorted(days)
            if len(keys) >= 4:
                boot = []
                for _ in range(2000):
                    pick = rng.integers(0, len(keys), len(keys))
                    vals = [v for p in pick for v in days[keys[p]]]
                    if vals:
                        boot.append(np.mean(vals))
                boot = np.array(boot)
                lo, hi = np.percentile(boot, [2.5, 97.5])
                print(f"  30d-block bootstrap on relative: 95% CI=[{lo:+.3%}, {hi:+.3%}] "
                      f"P(<=0)={np.mean(boot<=0):.3f}")

        big = [e for e in ev if e[1] >= 0.60]
        if big:
            print(f"\n  the >=60% 7d runners (STARKNET class):")
            for i, run, fw in sorted(big, key=lambda x: -x[1])[:10]:
                d = datetime.fromtimestamp(all_ts[i] / 1000, tz=timezone.utc)
                print(f"    {d:%Y-%m-%d} run={run:+.1%}  next7d={fw-COST:+.1%}")


if __name__ == "__main__":
    main()
