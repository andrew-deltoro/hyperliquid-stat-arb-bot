#!/usr/bin/env python
"""Freeze the deepest available SPY 1-minute RTH price-bar panel.

This is a DATA-FREEZE tool, not a study. It pulls bars, de-duplicates by
timestamp, sorts ascending, and reports ONLY the panel's structure
(counts and timestamps). It deliberately computes no return, VWAP,
deviation, decile, t-statistic or any other price statistic -- see the
header of docs/DAY_TRADE_EDGE_MATRIX.md ("How a row is allowed to become
a test"). Structure may be described; contents may not.

Two harvest routes
------------------
The Webull OpenAPI is reachable two ways, and this host has historically
had only one of them working at a time:

  * ``--source sdk``   (default) uses the isolated Webull venv and the
    connector's ``WebullData.fetch_stock_history`` (start_time/end_time
    pagination). Needs ``WEBULL_APP_KEY`` / ``WEBULL_APP_SECRET`` or
    ``~/.config/webull/credentials.json``. Invoke with
    ``/home/hlbot/webull-venv/bin/python`` -- the SDK is NOT in the repo
    venv and must not be added to it.

  * ``--source pages`` merges raw one-symbol ``get_stock_bars`` JSON page
    dumps previously saved to ``--pages-dir``. This mirrors the route
    that produced ``{SPY,QQQ}_5m_raw.json`` (raw MCP tool payloads
    written to disk, then merged). Use it when the credentials path is
    unavailable but interactive harvest is.

Idempotent
----------
The frozen panel is written to ``reports/.webull_cache/SPY_1m_RTH_frozen.json``
(gitignored). If that file already spans back to the requested floor, the
script no-ops. Progress is checkpointed to a sidecar file after every
page, so a re-run resumes instead of re-harvesting blindly. Pass
``--force`` to ignore existing output and pull from scratch.

Usage
-----
    /home/hlbot/webull-venv/bin/python scripts/feeds/freeze_spy_1m.py
    /home/hlbot/webull-venv/bin/python scripts/feeds/freeze_spy_1m.py --floor-ms 1622505600000
    /home/hlbot/webull-venv/bin/python scripts/feeds/freeze_spy_1m.py --source pages --pages-dir reports/.webull_cache/pages

Output: reports/.webull_cache/SPY_1m_RTH_frozen.json (a list of raw bar
dicts, ascending by time) plus a structure report on stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "reports" / ".webull_cache"
OUT = CACHE / "SPY_1m_RTH_frozen.json"
PROGRESS = CACHE / "SPY_1m_RTH_frozen.progress.json"

SYMBOL = "SPY"
CATEGORY = "US_ETF"
INTERVAL = "1m"
SESSIONS = "RTH"

# 2021-06-01 UTC. The 1m floor is "known to be ~mid-2021"; the walk stops
# when the API starts returning empty pages, whichever comes first.
DEFAULT_FLOOR_MS = 1_622_505_600_000
COUNT = 1650          # 1m supports up to 1650 bars/page
RTH_MIN_PER_DAY = 390  # ~390 RTH minutes per US equity session


# --------------------------------------------------------------- helpers
def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")


def row_ms(row: dict) -> int:
    t = row["time"]
    return int(parse_ts(t).timestamp() * 1000)


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text())


def save(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))


def merge(rows_iter) -> list[dict]:
    """De-duplicate by timestamp (first wins), sort ascending."""
    seen: dict[int, dict] = {}
    for r in rows_iter:
        seen.setdefault(row_ms(r), r)
    return [seen[k] for k in sorted(seen)]


# --------------------------------------------------------------- harvest
def harvest_sdk(floor_ms: int, force: bool) -> list[dict]:
    """Backward pagination via the connector, checkpointed per page."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from feeds.webull_connector import WebullData  # noqa: E402

    wd = WebullData()
    agg: dict[int, dict] = {}
    if PROGRESS.exists() and not force:
        agg = {row_ms(r): r for r in json.loads(PROGRESS.read_text())}
        print(f"resuming from checkpoint: {len(agg)} bars")

    cursor = int(time.time() * 1000)
    if agg:
        cursor = min(agg)
    pages = 0
    while cursor > floor_ms:
        rows = wd.get_stock_bars(SYMBOL, CATEGORY, INTERVAL, COUNT,
                                 start_ms=floor_ms, end_ms=cursor, sessions=SESSIONS)
        rows = wd._unwrap(rows, SYMBOL)
        if not rows:
            print(f"empty page at end_ms={cursor}; floor reached")
            break
        before = len(agg)
        for r in rows:
            agg.setdefault(row_ms(r), r)
        oldest = min(row_ms(r) for r in rows)
        pages += 1
        print(f"page {pages:4d}: +{len(agg) - before:5d} new, "
              f"total {len(agg):7d}, oldest {rows[0]['time'][:10]}  "
              f"(cursor {datetime.fromtimestamp(floor_ms/1000, timezone.utc).date()}"
              f" .. {datetime.fromtimestamp(oldest/1000, timezone.utc).date()})")
        save([agg[k] for k in sorted(agg)], PROGRESS)
        if oldest <= floor_ms or oldest >= cursor:
            break
        cursor = oldest
    return [agg[k] for k in sorted(agg)]


def harvest_pages(pages_dir: Path) -> list[dict]:
    """Merge raw one-symbol get_stock_bars page dumps from disk."""
    rows = []
    files = sorted(pages_dir.glob("*.json"))
    if not files:
        raise SystemExit(f"no page dumps in {pages_dir}; harvest pages first")
    for f in files:
        payload = json.loads(f.read_text())
        if isinstance(payload, dict):
            payload = payload.get("result", payload)
        for grp in payload:
            if grp.get("symbol") == SYMBOL:
                rows.extend(grp.get("result", []))
    return merge(rows)


# --------------------------------------------------------------- report
def report(rows: list[dict], path: Path) -> None:
    """Structure only: counts and timestamps, never a price statistic."""
    ts = [parse_ts(r["time"]) for r in rows]
    days = Counter(t.date() for t in ts)
    per_day = sorted(days.values())
    sessions = Counter(r.get("trading_session", "?") for r in rows)

    # intraday contiguity: expected spacing is 60s inside a session
    gaps = Counter(int((ts[i + 1] - ts[i]).total_seconds()) for i in range(len(ts) - 1))
    intraday = sum(n for g, n in gaps.items() if g <= 120)
    boundary = sum(n for g, n in gaps.items() if g > 120)

    print("\n" + "=" * 64)
    print("SPY 1m RTH frozen panel -- STRUCTURE ONLY")
    print("=" * 64)
    print(f"file            : {path}  ({path.stat().st_size:,} bytes)")
    print(f"bar count       : {len(rows):,}")
    print(f"first timestamp : {ts[0].isoformat()}  ({int(ts[0].timestamp()*1000)} ms)")
    print(f"last timestamp  : {ts[-1].isoformat()}  ({int(ts[-1].timestamp()*1000)} ms)")
    print(f"calendar span   : {(ts[-1] - ts[0]).days:,} days")
    print(f"distinct days   : {len(days):,}")
    print(f"bars/day min    : {per_day[0]}")
    print(f"bars/day median : {per_day[len(per_day)//2]}")
    print(f"bars/day max    : {per_day[-1]}")
    print(f"session tags    : {dict(sessions)}")
    print(f"duplicate ts    : 0 (deduped by construction)")
    print(f"60s gaps        : {intraday:,}")
    print(f"session boundaries (>120s gaps): {boundary:,}")
    print(f"clears 120 days : {'YES' if len(days) >= 120 else 'NO'}")
    print("=" * 64)


# --------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["sdk", "pages"], default="sdk")
    ap.add_argument("--pages-dir", type=Path, default=CACHE / "pages")
    ap.add_argument("--floor-ms", type=int, default=DEFAULT_FLOOR_MS,
                    help="earliest millisecond to walk back to")
    ap.add_argument("--force", action="store_true",
                    help="ignore existing output/checkpoint and re-harvest")
    args = ap.parse_args()

    existing = load(OUT)
    if existing and not args.force:
        first = row_ms(existing[0])
        if first <= args.floor_ms:
            print(f"already frozen: {OUT} ({len(existing)} bars, "
                  f"floor reached). Use --force to re-pull.")
            report(existing, OUT)
            return 0
        print(f"existing panel is partial ({len(existing)} bars, "
              f"first {existing[0]['time'][:10]}); re-harvesting")

    rows = harvest_pages(args.pages_dir) if args.source == "pages" else harvest_sdk(
        args.floor_ms, args.force)
    if not rows:
        raise SystemExit("no bars returned; nothing frozen")

    rows = merge(rows)
    save(rows, OUT)
    PROGRESS.unlink(missing_ok=True)
    report(rows, OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
