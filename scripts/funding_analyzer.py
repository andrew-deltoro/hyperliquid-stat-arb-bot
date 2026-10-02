"""
funding_analyzer.py — Hyperliquid funding rate scanner.

Queries HL metaAndAssetCtxs, computes annualized funding for each asset,
filters by liquidity and magnitude, outputs candidates for funding arb.

Usage:
    python funding_analyzer.py
    python funding_analyzer.py --threshold 30 --min-oi 500000
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

HL_URL = "https://api.hyperliquid.xyz/info"
REPO = Path("/home/hlbot/hyperliquid-stat-arb-bot")


def fetch_ctxs() -> tuple[list[str], list[dict]]:
    """Return (asset names, ctx dicts) for all HL perps."""
    r = requests.post(HL_URL, json={"type": "metaAndAssetCtxs"}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or len(data) < 2:
        raise ValueError(f"Unexpected response structure: {type(data)}")
    meta, ctxs = data[0], data[1]
    names = [a["name"] for a in meta["universe"]]
    return names, ctxs


def annualize(hourly_funding: float) -> float:
    """Hourly funding -> annualized percent. HL pays hourly."""
    return hourly_funding * 24 * 365 * 100


def analyze(threshold: float, min_oi_notional: float):
    names, ctxs = fetch_ctxs()
    print(f"Fetched {len(names)} assets from Hyperliquid")
    print()

    rows = []
    for name, ctx in zip(names, ctxs):
        try:
            funding_hourly = float(ctx.get("funding", 0))
            oi = float(ctx.get("openInterest", 0))
            mark_px = float(ctx.get("markPx", 0))
            oracle_px = float(ctx.get("oraclePx", 0))
            day_vol = float(ctx.get("dayNtlVlm", 0))

            # skip if missing data
            if mark_px <= 0:
                continue

            oi_notional = oi * mark_px
            ann_pct = annualize(funding_hourly)

            # premium: how far mark is from oracle (perp premium/discount)
            premium = (mark_px - oracle_px) / oracle_px if oracle_px > 0 else 0

            rows.append({
                "coin": name,
                "funding_hourly": funding_hourly,
                "annualized_pct": ann_pct,
                "mark_px": mark_px,
                "oracle_px": oracle_px,
                "premium_pct": premium * 100,
                "oi_notional": oi_notional,
                "day_volume_usd": day_vol,
            })
        except Exception:
            continue

    df = pd.DataFrame(rows)

    # Filter for meaningful signals
    df = df[
        (df["annualized_pct"].abs() >= threshold) &
        (df["oi_notional"] >= min_oi_notional)
    ].copy()

    df = df.sort_values("annualized_pct", key=lambda s: s.abs(), ascending=False)

    return df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=30.0,
                        help="Min |annualized funding| %% (default 30)")
    parser.add_argument("--min-oi", type=float, default=100_000,
                        help="Min OI notional in USD (default 100k)")
    parser.add_argument("--out", default=str(REPO / "reports" / "funding_scan.csv"))
    args = parser.parse_args()

    print("=" * 78)
    print("HYPERLIQUID FUNDING SCANNER")
    print("=" * 78)
    print(f"Threshold: |annualized| >= {args.threshold}%")
    print(f"Min OI notional: ${args.min_oi:,.0f}")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print()

    df = analyze(args.threshold, args.min_oi)

    if df.empty:
        print("No candidates meet the criteria.")
        return

    print(f"{len(df)} candidates found\n")
    print(f"{'coin':>12}  {'ann_pct':>9}  {'hourly':>12}  {'premium%':>9}  {'OI($)':>14}  {'24h vol($)':>14}")
    print("-" * 92)
    for _, r in df.iterrows():
        print(f"{r['coin']:>12}  {r['annualized_pct']:>+8.2f}%  {r['funding_hourly']:>+12.8f}  "
              f"{r['premium_pct']:>+8.3f}%  {r['oi_notional']:>14,.0f}  {r['day_volume_usd']:>14,.0f}")

    # Save full snapshot
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    df["scanned_at_utc"] = datetime.now(timezone.utc).isoformat()
    df.to_csv(args.out, index=False)
    print(f"\nSaved to {args.out}")

    # Split by direction
    long_side = df[df["annualized_pct"] > 0]   # longs pay -> short the perp
    short_side = df[df["annualized_pct"] < 0]  # shorts pay -> long the perp

    print()
    print(f"Direction breakdown:")
    print(f"  Positive funding (short perp): {len(long_side)} assets")
    print(f"  Negative funding (long perp):  {len(short_side)} assets")


if __name__ == "__main__":
    main()
