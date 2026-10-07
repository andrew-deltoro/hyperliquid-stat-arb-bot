"""
Kalshi market scanner (v2, uses correct API field names).

Steps:
  1. Fetch all series -> build ticker prefix -> category map
  2. Fetch all open markets (paginated)
  3. Extract real quotes from *_dollars fields
  4. Map each market to its series' category
  5. Filter to tradeable (bid > 0, ask > 0, ask > bid, 24h volume > 0)
  6. Sort by 24h volume, dump to state file
"""
from __future__ import annotations
import sys, json, time
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
REPO = Path("/home/hlbot/hyperliquid-stat-arb-bot")
STATE_DIR = Path("/home/hlbot/hlbot-runtime/live/state")
if not STATE_DIR.exists():
    STATE_DIR = REPO / "state"
OUT_FILE = STATE_DIR / "kalshi_markets.json"


def get(path: str, params: dict | None = None, retries: int = 3) -> dict | None:
    for attempt in range(retries):
        try:
            r = requests.get(f"{BASE}{path}", params=params or {}, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                print(f"  {path} failed: {e}")
                return None
            time.sleep(1)
    return None


def fetch_all_series() -> dict[str, str]:
    """Return {series_ticker: category}."""
    out: dict[str, str] = {}
    cursor = None
    pages = 0
    while pages < 200:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = get("/series", params)
        if not d:
            break
        batch = d.get("series", [])
        for s in batch:
            t = s.get("ticker")
            c = s.get("category") or "other"
            if t:
                out[t] = c
        cursor = d.get("cursor")
        pages += 1
        if not cursor or not batch:
            break
        time.sleep(0.1)
    return out


def fetch_all_markets(max_pages: int = 200) -> list[dict]:
    out = []
    cursor = None
    for page in range(max_pages):
        params = {"limit": 200, "status": "open", "mve_filter": "exclude"}
        if cursor:
            params["cursor"] = cursor
        d = get("/markets", params)
        if not d:
            break
        batch = d.get("markets", [])
        out.extend(batch)
        cursor = d.get("cursor")
        if not cursor or not batch:
            break
        time.sleep(0.1)
    return out


def parse_dollars(v) -> float | None:
    """Convert '0.4500' -> 0.45. Return None on failure."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except Exception:
        return None


def series_ticker_of(market: dict) -> str | None:
    """Extract series ticker from event_ticker (strip date-suffix)."""
    et = market.get("event_ticker") or ""
    # Format: SERIES-YYMMMDDHHMMSTRIKE  or  SERIES-Sxxxx
    # Series is everything before the first "-"
    if "-" in et:
        return et.split("-", 1)[0]
    return et or None


def normalize(market: dict, series_cat: dict[str, str]) -> dict:
    yb = parse_dollars(market.get("yes_bid_dollars"))
    ya = parse_dollars(market.get("yes_ask_dollars"))
    vol = parse_dollars(market.get("volume_24h_fp")) or 0.0
    oi = parse_dollars(market.get("open_interest_fp")) or 0.0
    last = parse_dollars(market.get("last_price_dollars"))

    st = series_ticker_of(market)
    cat = series_cat.get(st, "other") if st else "other"

    # Tradeable = both sides quoted with valid spread, some volume
    tradeable = (
        yb is not None and ya is not None
        and ya > yb
        and vol >= 100  # at least $100 of 24h volume
    )

    return {
        "ticker": market.get("ticker"),
        "event_ticker": market.get("event_ticker"),
        "series_ticker": st,
        "title": market.get("title") or market.get("yes_sub_title"),
        "category": cat,
        "yes_bid": yb,
        "yes_ask": ya,
        "mid": (yb + ya) / 2 if (yb and ya) else None,
        "spread": (ya - yb) if (yb and ya) else None,
        "last": last,
        "volume_24h": vol,
        "open_interest": oi,
        "close_time": market.get("close_time"),
        "status": market.get("status"),
        "tradeable": tradeable,
    }


def main():
    print("Kalshi market scanner v2")
    print("=" * 70)

    print("\n[1/2] Fetching series (for category mapping)...")
    t0 = time.time()
    series_cat = fetch_all_series()
    print(f"  fetched {len(series_cat)} series in {time.time()-t0:.1f}s")

    print("\n[2/2] Fetching markets...")
    t0 = time.time()
    raw = fetch_all_markets()
    # Defensive: strip any MVE markets that slip through
    raw = [m for m in raw
           if not m.get("mve_collection_ticker")
           and not m.get("mve_selected_legs")]
    print(f"  fetched {len(raw)} markets in {time.time()-t0:.1f}s")

    if not raw:
        print("No markets returned. Check API.")
        return

    normalized = [normalize(m, series_cat) for m in raw]
    tradeable = [m for m in normalized if m["tradeable"]]

    print(f"\n--- Coverage ---")
    print(f"  total markets:    {len(normalized)}")
    print(f"  tradeable:        {len(tradeable)}")
    print(f"  with quotes:      {sum(1 for m in normalized if m['mid'] is not None)}")
    print(f"  with >0 24h vol:  {sum(1 for m in normalized if m['volume_24h'] > 0)}")

    # Category breakdown of tradeable
    print(f"\n--- Tradeable by category ---")
    for cat, n in Counter(m["category"] for m in tradeable).most_common():
        print(f"  {cat:>20}: {n}")

    # Sort by volume
    tradeable.sort(key=lambda m: -m["volume_24h"])
    print(f"\n--- Top 40 tradeable markets by 24h volume ---")
    print(f"{'ticker':>42}  {'cat':>14}  {'mid':>5}  {'spr':>5}  {'vol24':>10}  title")
    print("-" * 140)
    for m in tradeable[:40]:
        title = (m["title"] or "")[:55]
        print(f"{(m['ticker'] or '')[:42]:>42}  {m['category']:>14}  "
              f"{m['mid']:>5.2f}  {m['spread']:>5.2f}  "
              f"{m['volume_24h']:>10,.0f}  {title}")

    # Save
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total_markets": len(normalized),
        "tradeable_markets": len(tradeable),
        "series_count": len(series_cat),
        "markets": normalized,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nSaved: {OUT_FILE}")


if __name__ == "__main__":
    main()
