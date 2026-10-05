"""
Funding rate signal generator.

Reads state/funding_history.db, ranks HL-native candidates by funding rate
quality, writes state/funding_candidates.json.

Usage:
    python -m src.hyperliquid_bot.funding.signal
"""
from __future__ import annotations
import os, sys, json, sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/home/hlbot/hyperliquid-stat-arb-bot")
sys.path.insert(0, str(REPO))

# The funding collector service (older, does not set HLBOT_RUNTIME_DIR)
# writes to REPO/state/. The live trading service uses RUNTIME_DIR.
# Look in both places and use whichever exists.
_CANDIDATE_DB_PATHS = [
    Path("/home/hlbot/hyperliquid-stat-arb-bot/state/funding_history.db"),
    Path("/home/hlbot/hlbot-runtime/live/state/funding_history.db"),
    Path("/home/hlbot/hlbot-runtime/dry/state/funding_history.db"),
]


def _resolve_db_path() -> Path:
    """Pick the most recently modified funding_history.db."""
    existing = [p for p in _CANDIDATE_DB_PATHS if p.exists()]
    if not existing:
        return _CANDIDATE_DB_PATHS[0]  # default for error messages
    return max(existing, key=lambda p: p.stat().st_mtime)


DB_FILE = _resolve_db_path()
OUT_FILE = Path("/home/hlbot/hyperliquid-stat-arb-bot/state/funding_candidates.json")

# HL-native funding candidates: must have BOTH spot and perp on HL
HL_NATIVE_WHITELIST = [
    "PURR", "TRUMP", "PUMP", "HYPE", "BERA", "MON", "STABLE", "AZTEC",
    # Add more as we discover them
]

# Signal thresholds
MIN_AVG_FUNDING_PCT = 20.0       # annualized
MIN_PERSISTENCE = 0.70           # 70% of snapshots above 10% floor
MIN_SNAPSHOTS = 24               # at least 6h of data (4 snapshots/hour × 6h)


def load_candidates(window_hours: int = 24) -> list[dict]:
    """
    Query funding_history.db for candidates in the last `window_hours`.
    Returns list of dicts sorted by funding score (higher = better).
    """
    if not DB_FILE.exists():
        print(f"ERROR: {DB_FILE} does not exist")
        return []

    conn = sqlite3.connect(DB_FILE)
    cutoff = f"datetime('now', '-{window_hours} hours')"

    # Query: per-coin stats over the window
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
        persistence = n_high / n if n > 0 else 0.0

        # Only keep HL-native whitelisted candidates
        if coin not in HL_NATIVE_WHITELIST:
            continue

        # Apply filters
        if avg_ann < MIN_AVG_FUNDING_PCT:
            continue
        if min_ann is not None and min_ann <= 0:
            continue
        if persistence < MIN_PERSISTENCE:
            continue

        # Score: average funding × persistence
        score = avg_ann * persistence

        results.append({
            "coin": coin,
            "score": round(score, 2),
            "avg_funding_pct": round(avg_ann, 2),
            "min_funding_pct": round(min_ann, 2) if min_ann is not None else None,
            "max_funding_pct": round(max_ann, 2) if max_ann is not None else None,
            "persistence": round(persistence, 3),
            "snapshots": n,
            "avg_oi_usd": int(avg_oi) if avg_oi else 0,
            "avg_daily_vol_usd": int(avg_vol) if avg_vol else 0,
        })

    conn.close()
    results.sort(key=lambda r: -r["score"])
    return results


def main():
    print("=== Funding signal scan ===")
    print(f"Database: {DB_FILE}")
    print()

    candidates = load_candidates(window_hours=24)

    print(f"Qualified candidates: {len(candidates)}")
    print()

    if candidates:
        print(f"{'coin':>8}  {'score':>8}  {'avg%':>8}  {'min%':>8}  {'persist':>8}  {'n':>5}  {'OI($)':>15}  {'vol($)':>15}")
        print("-" * 90)
        for c in candidates:
            print(f"{c['coin']:>8}  {c['score']:>8.2f}  {c['avg_funding_pct']:>7.2f}%  {c['min_funding_pct']:>7.2f}%  {c['persistence']:>7.1%}  {c['snapshots']:>5}  {c['avg_oi_usd']:>15,}  {c['avg_daily_vol_usd']:>15,}")
    else:
        print("No candidates qualified. Possible reasons:")
        print("  1. funding_history.db has < 24 snapshots per coin")
        print("  2. All averages are below the 20% threshold")
        print("  3. Whitelist filtering (candidate isn't on HL_NATIVE_WHITELIST)")
        print()
        print("Diagnostic: what coins are in the DB?")
        conn = sqlite3.connect(DB_FILE)
        for row in conn.execute("SELECT coin, COUNT(*), AVG(annualized_pct) FROM funding_snapshots GROUP BY coin ORDER BY COUNT(*) DESC LIMIT 15"):
            print(f"  {row[0]:>8}: n={row[1]:>4}  avg={row[2]:>+8.2f}%")
        conn.close()

    out = {
        "last_scan": datetime.now(timezone.utc).isoformat(),
        "window_hours": 24,
        "candidates": candidates,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print()
    print(f"Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
