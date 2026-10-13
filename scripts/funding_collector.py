"""
funding_collector.py — Poll HL funding rates every cycle, store to SQLite.

Writes to the SAME runtime state dir as the rest of the bot
(HLBOT_RUNTIME_DIR/state via phase3.paths), not the repo. The repo path is
only used as a fallback when path resolution is unavailable, and an existing
repo-side DB is migrated once so the 52MB of collected history is not
stranded.

Usage:
    python funding_collector.py --once      # single poll
    python funding_collector.py --loop      # poll forever (interval 15m)
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HL_URL = "https://api.hyperliquid.xyz/info"
REPO = Path("/home/hlbot/hyperliquid-stat-arb-bot")
_LEGACY_DB = REPO / "state" / "funding_history.db"


def _runtime_state_dir() -> Path:
    """Runtime state dir, matching where the funding bot reads from.

    Priority: HLBOT_RUNTIME_DIR if set, then the live runtime dir, then the
    repo. The live runtime dir is the default because the funding-collector
    systemd unit sets no HLBOT_RUNTIME_DIR while hlbot-funding-paper.service
    runs with HLBOT_RUNTIME_DIR=/home/hlbot/hlbot-runtime/live -- so without
    this, the collector and its only reader silently disagree and the history
    splits across two files (which is exactly what had happened).
    """
    env = os.getenv("HLBOT_RUNTIME_DIR", "").strip()
    if env:
        return Path(env) / "state"
    live = Path("/home/hlbot/hlbot-runtime/live/state")
    if live.parent.exists():
        return live
    return REPO / "state"


def resolve_db_path() -> Path:
    """Where the DB lives. Migrates the legacy repo file once if needed."""
    target = _runtime_state_dir() / "funding_history.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    if (target.exists() and target.stat().st_size > 0) or not _LEGACY_DB.exists():
        return target
    # One-time migration: never leave a grown history behind.
    try:
        if target.exists():
            target.unlink()
        shutil.move(str(_LEGACY_DB), str(target))
        print(f"[funding_collector] migrated {_LEGACY_DB} -> {target}")
    except Exception as e:  # noqa: BLE001
        print(f"[funding_collector] migration failed ({e}); using legacy path")
        return _LEGACY_DB
    return target


DB_PATH = resolve_db_path()


def init_db(conn: sqlite3.Connection):
    # WAL keeps the 15-min writer from blocking readers; synchronous=NORMAL is
    # the standard WAL pairing (durable across process crash, just not a hard
    # power loss) and avoids fsync on every one of the 234-row inserts.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS funding_snapshots (
            ts_utc TEXT NOT NULL,
            coin TEXT NOT NULL,
            funding_hourly REAL,
            annualized_pct REAL,
            mark_px REAL,
            oracle_px REAL,
            premium_pct REAL,
            oi_notional REAL,
            day_volume_usd REAL,
            PRIMARY KEY (ts_utc, coin)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_coin_ts
        ON funding_snapshots (coin, ts_utc)
    """)
    conn.commit()


def fetch_and_store(conn: sqlite3.Connection) -> int:
    r = requests.post(HL_URL, json={"type": "metaAndAssetCtxs"}, timeout=20)
    r.raise_for_status()
    data = r.json()
    meta, ctxs = data[0], data[1]
    names = [a["name"] for a in meta["universe"]]

    ts = datetime.now(timezone.utc).isoformat()
    rows = []
    for name, ctx in zip(names, ctxs):
        try:
            funding = float(ctx.get("funding", 0))
            oi = float(ctx.get("openInterest", 0))
            mark_px = float(ctx.get("markPx", 0))
            oracle_px = float(ctx.get("oraclePx", 0))
            day_vol = float(ctx.get("dayNtlVlm", 0))
            if mark_px <= 0:
                continue
            ann = funding * 24 * 365 * 100
            oi_notional = oi * mark_px
            premium = (mark_px - oracle_px) / oracle_px * 100 if oracle_px > 0 else 0
            rows.append((ts, name, funding, ann, mark_px, oracle_px, premium, oi_notional, day_vol))
        except Exception:
            continue

    conn.executemany("""
        INSERT OR REPLACE INTO funding_snapshots
        (ts_utc, coin, funding_hourly, annualized_pct, mark_px, oracle_px, premium_pct, oi_notional, day_volume_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    return len(rows)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--interval", type=int, default=900, help="Seconds between polls (default 900 = 15min)")
    args = p.parse_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.once or not args.loop:
        n = fetch_and_store(conn)
        print(f"[{datetime.now(timezone.utc).isoformat()}] Stored {n} snapshots")
        return

    print(f"Looping every {args.interval}s. Ctrl+C to stop.")
    while True:
        try:
            n = fetch_and_store(conn)
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Stored {n} snapshots")
        except Exception as e:
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] ERROR: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
