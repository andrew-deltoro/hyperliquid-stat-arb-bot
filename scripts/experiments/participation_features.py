#!/usr/bin/env python3
"""
Do participation features separate real breakouts from noise?

We already have, from the candle API: v (base volume), n (trade count).
Derived: avg trade size = v/n, which separates broad retail participation
from a one-actor spike.

For every shock bar (|ret| >= MOVE, vol-z >= VOLZ) we measure forward 24h
continuation and ask whether ANY observable feature at the shock bar
predicts it. Decile/base-rate framing rather than a fitted model, so a
result is readable and hard to overfit.

Features at the shock bar:
  ret1        the shock return itself (signed)
  vol_z       volume z vs trailing 168h
  cnt_z       trade-count z vs trailing 168h
  size_z      avg-trade-size z vs trailing 168h
  rng         high-low range / close (bar quality)
  fwd24       forward 24h return SAME direction (what we want)

Run: .venv/bin/python scripts/experiments/participation_features.py
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
    "TAO", "FET", "RENDER",
})
WINDOW_DAYS = 180
VOLZ_WIN = 168
MOVE = 0.05
VOLZ_MIN = 3.0
FWD = 24


def candles(coin, s, e, tries=4):
    for k in range(tries):
        try:
            r = requests.post(API, json={"type": "candleSnapshot",
                "req": {"coin": coin, "interval": "1h", "startTime": s, "endTime": e}},
                timeout=40).json()
            if isinstance(r, list) and r:
                return r
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.8 * (k + 1))
    return []


def zs(x):
    x = np.asarray(x, float)
    mu, sd = x.mean(), x.std()
    return (x - mu) / sd if sd > 0 else np.zeros_like(x)


def tstat(v):
    v = np.asarray(v, float)
    if len(v) < 3 or v.std(ddof=1) == 0:
        return float("nan")
    return float(v.mean() / (v.std(ddof=1) / math.sqrt(len(v))))


def main():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=WINDOW_DAYS)
    s_ms, e_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    print(f"Fetching {len(COINS)} coins x {WINDOW_DAYS}d...")

    rows = []   # feature dicts
    rsr_event = None
    for coin in COINS:
        bars = sorted(candles(coin, s_ms, e_ms), key=lambda b: b["t"])
        time.sleep(0.2)
        if len(bars) < VOLZ_WIN + FWD + 10:
            continue
        C = np.array([float(b["c"]) for b in bars])
        V = np.array([float(b.get("v", 0)) for b in bars])
        N = np.array([float(b.get("n", 0)) for b in bars])
        H = np.array([float(b.get("h", b["c"])) for b in bars])
        L = np.array([float(b.get("l", b["c"])) for b in bars])
        ret = np.zeros_like(C)
        ret[1:] = C[1:] / C[:-1] - 1.0
        with np.errstate(divide="ignore", invalid="ignore"):
            size = np.where(N > 0, V / np.maximum(N, 1), 0.0)
        T = [b["t"] for b in bars]

        for i in range(VOLZ_WIN, len(C) - FWD - 1):
            if abs(ret[i]) < MOVE:
                continue
            wz = zs(V[i - VOLZ_WIN:i])
            cz = zs(N[i - VOLZ_WIN:i])
            sz = zs(size[i - VOLZ_WIN:i])
            if not np.isfinite(wz).all() or not np.isfinite(cz).all():
                continue
            vol_z = (V[i] - V[i - VOLZ_WIN:i].mean()) / (V[i - VOLZ_WIN:i].std() or 1)
            cnt_z = (N[i] - N[i - VOLZ_WIN:i].mean()) / (N[i - VOLZ_WIN:i].std() or 1)
            sz_w = size[i - VOLZ_WIN:i]
            size_z = ((size[i] - sz_w.mean()) / (sz_w.std() or 1)) if sz_w.std() else 0.0
            if vol_z < VOLZ_MIN:
                continue
            sgn = np.sign(ret[i])
            fwd = sgn * (C[i + FWD] / C[i] - 1.0)
            rng = (H[i] - L[i]) / C[i] if C[i] else 0.0
            rec = dict(coin=coin, t=T[i], ret1=float(ret[i]), vol_z=float(vol_z),
                       cnt_z=float(cnt_z), size_z=float(size_z), rng=float(rng),
                       fwd24=float(fwd))
            rows.append(rec)
            if coin == "RSR" and abs(ret[i]) > 0.30:
                rsr_event = rec

    print(f"shock events: {len(rows)}")
    if not rows:
        return
    fwd = np.array([r["fwd24"] for r in rows])
    base = (fwd > 0).mean()
    print(f"base rate P(fwd24 continues) = {base:.1%}   mean fwd24 = {fwd.mean():+.3%}\n")

    for feat in ("ret1", "vol_z", "cnt_z", "size_z", "rng"):
        x = np.array([r[feat] for r in rows])
        # decile separation
        q = np.quantile(x, [0.2, 0.4, 0.6, 0.8])
        buckets = np.digitize(x, q)
        print(f"--- {feat} ---")
        for bi in range(5):
            sel = fwd[buckets == bi]
            if len(sel) < 5:
                continue
            print(f"   Q{bi+1} n={len(sel):>4}  P(cont)={ (sel>0).mean():.1%}  "
                  f"mean={sel.mean():+.3%}  t={tstat(sel):+.2f}")
        # rank correlation
        rk = np.argsort(np.argsort(x))
        corr = np.corrcoef(rk, np.argsort(np.argsort(fwd)))[0, 1]
        print(f"   spearman(feat, fwd24) = {corr:+.3f}\n")

    # the RSR event itself
    if rsr_event:
        print("RSR event features (the one we actually caught):")
        print(f"  ret1={rsr_event['ret1']:+.1%} vol_z={rsr_event['vol_z']:+.1f} "
              f"cnt_z={rsr_event['cnt_z']:+.1f} size_z={rsr_event['size_z']:+.1f} "
              f"rng={rsr_event['rng']:.3f} fwd24={rsr_event['fwd24']:+.1%}")
        sz = np.array([r["size_z"] for r in rows])
        cz = np.array([r["cnt_z"] for r in rows])
        print(f"  percentile among shocks: size_z {100*(sz<rsr_event['size_z']).mean():.0f}th, "
              f"cnt_z {100*(cz<rsr_event['cnt_z']).mean():.0f}th")

    print("\nIf no feature shows a monotone decile pattern, participation "
          "features do not make breakouts selective and we need OI / "
          "liquidations instead.")


if __name__ == "__main__":
    main()
