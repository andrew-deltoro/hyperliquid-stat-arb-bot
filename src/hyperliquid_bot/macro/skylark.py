"""
Skylark macro regime fetcher.

Fetches the weekly data CSV from Skylark's published Google Sheet,
extracts the Monthly FLP value, and writes a regime overlay to state.

Data source: Skylark Research Terminal (friend's platform).
Weekly cadence — his sheet updates weekly.
"""
from __future__ import annotations
import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# The two URLs the user shared. Use the first (has FLP + FLI columns).
# If that fails, fall back to the second.
SKYLARK_URLS = [
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vT0wFeLanhrsgYBVRth-g5VBYTvODZZWKyv1TtuoZfyFRBoUWWmHE48rbs-22K9VgBNkQYROQU83xVS/pub?output=csv",
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vTszaupgmVN21RxKni6ImMPTKNrEWMozYmezs35q4YG6PcmHzcIyabU26Cc7-HyMs8rNJ-a7veeNcju/pub?output=csv",
]

REPO = Path("/home/hlbot/hyperliquid-stat-arb-bot")
STATE_DIR = Path("/home/hlbot/hlbot-runtime/live/state")
if not STATE_DIR.exists():
    STATE_DIR = REPO / "state"

OUT_FILE = STATE_DIR / "macro_regime.json"


def fetch_csv() -> str | None:
    """Try each URL until one returns valid CSV."""
    for url in SKYLARK_URLS:
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200 and len(r.text) > 100:
                return r.text
        except Exception as e:
            print(f"  fetch failed for {url[:60]}...: {e}")
    return None


def parse_latest(csv_text: str) -> dict | None:
    """Parse CSV, return the most recent row with a non-empty FLP value."""
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return None

    # The header contains "Monthly FLP" — find the exact key
    headers = list(rows[0].keys())
    flp_key = None
    for k in headers:
        if "FLP" in k and "Monthly" in k:
            flp_key = k
            break
    if flp_key is None:
        # Fallback — first header containing "FLP"
        for k in headers:
            if "FLP" in k:
                flp_key = k
                break
    if flp_key is None:
        print(f"  Could not find FLP column in: {headers}")
        return None

    gbc_key = next((k for k in headers if "GBC" in k), None)
    date_key = headers[0]  # Date is first column

    # Find latest row with a valid FLP
    for row in rows:
        flp_val = row.get(flp_key, "").strip()
        if not flp_val:
            continue
        try:
            flp = float(flp_val)
        except ValueError:
            continue
        try:
            gbc = float(row.get(gbc_key, "").strip()) if gbc_key and row.get(gbc_key, "").strip() else None
        except ValueError:
            gbc = None

        return {
            "date": row.get(date_key, "").strip(),
            "flp_monthly": flp,
            "gbc": gbc,
            "flp_column": flp_key,
            "gbc_column": gbc_key,
        }
    return None


def classify_regime(flp: float) -> tuple[str, float]:
    """Return (zone_name, sizing_multiplier)."""
    if flp < 50:
        return "favorable", 1.00
    elif flp < 60:
        return "low_transitional", 0.75
    elif flp < 65:
        return "high_transitional", 0.50
    else:
        return "defensive", 0.00


def main():
    print("=== Skylark Macro Fetch ===")
    csv_text = fetch_csv()
    if not csv_text:
        print("ERROR: Could not fetch any URL")
        sys.exit(1)

    latest = parse_latest(csv_text)
    if not latest:
        print("ERROR: Could not parse latest row")
        sys.exit(1)

    zone, multiplier = classify_regime(latest["flp_monthly"])

    payload = {
        "last_fetch": datetime.now(timezone.utc).isoformat(),
        "source": "skylark",
        "flp_monthly": latest["flp_monthly"],
        "flp_date": latest["date"],
        "gbc": latest["gbc"],
        "regime": zone,
        "sizing_multiplier": multiplier,
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"FLP (Monthly): {latest['flp_monthly']:.2f}  (as of {latest['date']})")
    if latest["gbc"] is not None:
        print(f"GBC:          {latest['gbc']:.2f}")
    print(f"Regime:       {zone}")
    print(f"Multiplier:   {multiplier:.2f}x")
    print()
    print(f"Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
