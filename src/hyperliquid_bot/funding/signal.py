"""
Funding rate signal generator.

Reads state/funding_history.db, ranks candidates by funding quality,
flags which have Binance USDT spot pairs (hardcoded, expandable).

Output: state/funding_candidates.json

Path resolution follows the same rule as everything else (phase3/paths.py):
the runtime dir wins when HLBOT_RUNTIME_DIR is set, then the historical
locations. The collector writes to runtime state/, so the legacy repo path
is kept only as a fallback for the 52MB of history collected before the
move. Newest non-empty file wins if several exist.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_STATE = Path("/home/hlbot/hyperliquid-stat-arb-bot/state")


def _candidate_paths() -> list[Path]:
    """Newest-first list of every place funding_history.db has lived."""
    paths: list[Path] = []
    runtime = os.getenv("HLBOT_RUNTIME_DIR", "").strip()
    if runtime:
        paths.append(Path(runtime) / "state" / "funding_history.db")
    paths.append(_REPO_STATE / "funding_history.db")
    paths.append(Path("/home/hlbot/hlbot-runtime/live/state/funding_history.db"))
    paths.append(Path("/home/hlbot/hlbot-runtime/dry/state/funding_history.db"))
    return paths


def _resolve_db() -> Path:
    candidates = _candidate_paths()
    existing = [p for p in candidates if p.exists() and p.stat().st_size > 0]
    if not existing:
        # Nothing on disk yet: return the preferred (runtime if set) path so
        # the caller creates it in the right place.
        return candidates[0]
    return max(existing, key=lambda p: p.stat().st_mtime)


def _resolve_out_file() -> Path:
    # Keep the candidates file next to the DB it was derived from, so the
    # reader and writer never end up looking at different directories.
    return DB_FILE.parent / "funding_candidates.json"


DB_FILE = _resolve_db()
OUT_FILE = _resolve_out_file()

# Binance-listed spot pairs we can trade. Extend as we discover more.
# Format: HL coin name -> Binance spot symbol
BINANCE_SPOT = {
    "BOME": "BOMEUSDT",
    "FARTCOIN": "FARTCOINUSDT",
    "NEAR": "NEARUSDT",
    "ZEC": "ZECUSDT",
    "IOTA": "IOTAUSDT",
    "ICP": "ICPUSDT",
    "ENA": "ENAUSDT",
    "XMR": "XMRUSDT",
    "MORPHO": "MORPHOUSDT",   # if listed
    "SYRUP": "SYRUPUSDT",     # if listed
    # Add more as Binance listings are confirmed
}

# Signal thresholds
MIN_AVG_FUNDING_PCT = 20.0
MIN_PERSISTENCE = 0.70
MIN_SNAPSHOTS = 12           # 3h of data at 15-min cadence
MIN_OI_USD = 500_000         # liquidity floor
MIN_DAILY_VOL_USD = 100_000  # minimum trade volume


def scan(window_hours: int = 24) -> list[dict]:
    """Scan funding DB for qualified candidates over the given window."""
    if not DB_FILE.exists():
        print(f"ERROR: DB not found at {DB_FILE}")
        return []

    conn = sqlite3.connect(DB_FILE)
    cutoff = f"datetime('now', '-{window_hours} hours')"

    query = f"""
        SELECT
            coin,
            COUNT(*) AS n,
            AVG(annualized_pct) AS avg_ann,
            MIN(annualized_pct) AS min_ann,
            MAX(annualized_pct) AS max_ann,
            AVG(oi_notional) AS avg_oi,
            AVG(day_volume_usd) AS avg_vol,
            SUM(CASE WHEN annualized_pct > 10 THEN 1 ELSE 0 END) AS n_high
        FROM funding_snapshots
        WHERE ts_utc >= {cutoff}
        GROUP BY coin
        HAVING COUNT(*) >= {MIN_SNAPSHOTS}
    """

    results = []
    for row in conn.execute(query):
        coin, n, avg_ann, min_ann, max_ann, avg_oi, avg_vol, n_high = row
        persistence = n_high / n if n else 0.0

        # Filters
        if avg_ann is None or avg_ann < MIN_AVG_FUNDING_PCT:
            continue
        if min_ann is not None and min_ann <= 0:
            continue
        if persistence < MIN_PERSISTENCE:
            continue
        if avg_oi is None or avg_oi < MIN_OI_USD:
            continue
        if avg_vol is None or avg_vol < MIN_DAILY_VOL_USD:
            continue

        on_binance = coin in BINANCE_SPOT
        score = avg_ann * persistence

        results.append({
            "coin": coin,
            "score": round(score, 2),
            "avg_funding_pct": round(avg_ann, 2),
            "min_funding_pct": round(min_ann, 2) if min_ann is not None else None,
            "max_funding_pct": round(max_ann, 2) if max_ann is not None else None,
            "persistence": round(persistence, 4),
            "snapshots": n,
            "avg_oi_usd": int(avg_oi) if avg_oi else 0,
            "avg_daily_vol_usd": int(avg_vol) if avg_vol else 0,
            "binance_spot": BINANCE_SPOT.get(coin),  # None if not on Binance
            "tradeable": on_binance,
        })

    conn.close()
    results.sort(key=lambda r: (-r["tradeable"], -r["score"]))
    return results


def main():
    print(f"=== Funding Signal Scan ===")
    print(f"DB: {DB_FILE}")
    print()

    candidates = scan(window_hours=24)

    tradeable = [c for c in candidates if c["tradeable"]]
    research = [c for c in candidates if not c["tradeable"]]

    print(f"Total qualified: {len(candidates)}")
    print(f"Tradeable on Binance: {len(tradeable)}")
    print()

    print("=== TRADEABLE (Binance spot + HL perp) ===")
    if tradeable:
        print(f"{'coin':>10}  {'score':>8}  {'avg%':>8}  {'min%':>8}  {'persist':>8}  {'n':>5}  {'OI($)':>14}")
        print("-" * 78)
        for c in tradeable:
            print(f"{c['coin']:>10}  {c['score']:>8.2f}  {c['avg_funding_pct']:>+7.2f}%  {c['min_funding_pct']:>+7.2f}%  {c['persistence']:>7.1%}  {c['snapshots']:>5}  {c['avg_oi_usd']:>14,}")
    else:
        print("  (none qualified)")
    print()

    print("=== RESEARCH (funding OK, but no Binance spot) ===")
    print(f"{'coin':>10}  {'score':>8}  {'avg%':>8}  {'min%':>8}  {'persist':>8}")
    print("-" * 60)
    for c in research[:10]:
        print(f"{c['coin']:>10}  {c['score']:>8.2f}  {c['avg_funding_pct']:>+7.2f}%  {c['min_funding_pct']:>+7.2f}%  {c['persistence']:>7.1%}")

    out = {
        "last_scan": datetime.now(timezone.utc).isoformat(),
        "window_hours": 24,
        "db_file": str(DB_FILE),
        "candidates": candidates,
        "tradeable": tradeable,
        "research": research,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print()
    print(f"Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
