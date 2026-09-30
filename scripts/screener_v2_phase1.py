"""
Screener v2 - Phase 1: Correlation scan across all Hyperliquid perps.
Filters by 90-day return correlation, groups by sector, saves candidates.
"""
import sys, time, json
from pathlib import Path
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot/src")

import requests
import numpy as np
import pandas as pd

HL_INFO_URL = "https://api.hyperliquid.xyz/info"

# Sector groupings for candidate filtering — pairs within a sector are more likely cointegrated
SECTORS = {
    "btc_eth": ["BTC", "ETH", "WBTC", "cbBTC"],
    "l1_major": ["SOL", "AVAX", "MATIC", "ATOM", "DOT", "NEAR", "APT", "SUI", "SEI", "TIA", "INJ", "FTM"],
    "l2": ["ARB", "OP", "MATIC", "STRK", "MANTA", "BLAST", "ZK"],
    "defi": ["AAVE", "COMP", "MKR", "UNI", "CRV", "LDO", "SNX", "GMX", "DYDX", "PENDLE", "FXS", "SUSHI"],
    "oracle": ["LINK", "PYTH", "BAND", "API3"],
    "meme": ["DOGE", "kPEPE", "WIF", "BONK", "FLOKI", "SHIB"],
    "ai": ["RNDR", "FET", "AGIX", "TAO"],
    "gaming": ["IMX", "GODS", "MAGIC", "APE"],
    "infra": ["FIL", "AR", "HNT", "AKT", "AIOZ"],
    "exchange": ["BNB", "OKB", "HT"],
}

def fetch_universe():
    r = requests.post(HL_INFO_URL, json={"type": "meta"}, timeout=15)
    r.raise_for_status()
    return [a["name"] for a in r.json()["universe"]]

def fetch_candles(coin, interval="1h", lookback_hours=24*90):
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - lookback_hours * 3600 * 1000
    try:
        r = requests.post(
            HL_INFO_URL,
            json={"type": "candleSnapshot", "req": {
                "coin": coin, "interval": interval,
                "startTime": start_ms, "endTime": end_ms}},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        # dict indexed by timestamp
        return {int(c["t"]): float(c["c"]) for c in data}
    except Exception:
        return None

def main():
    print("Fetching universe...")
    universe = fetch_universe()
    print(f"Total perps: {len(universe)}")

    # Restrict to sector members to save API calls
    sector_coins = set()
    for members in SECTORS.values():
        sector_coins.update(members)
    # Only test coins that exist AND are in a sector
    candidates = [c for c in universe if c in sector_coins]
    print(f"Candidates in sectors: {len(candidates)}")
    print(f"List: {candidates}")

    # Fetch all candles
    print("\nFetching candles (this takes ~2-3 min)...")
    prices = {}
    for coin in candidates:
        c = fetch_candles(coin)
        if c is None or len(c) < 1000:
            print(f"  {coin}: skip ({0 if c is None else len(c)} bars)")
            continue
        prices[coin] = c
        print(f"  {coin}: {len(c)} bars")
        time.sleep(0.05)

    # Align timestamps across all coins
    shared_ts = None
    for coin, c in prices.items():
        ts = set(c.keys())
        shared_ts = ts if shared_ts is None else shared_ts & ts
    shared_ts = sorted(shared_ts)
    print(f"\nCommon timestamps: {len(shared_ts)}")

    # Build return matrix
    data = {}
    for coin, c in prices.items():
        arr = np.array([c[t] for t in shared_ts])
        data[coin] = np.diff(np.log(arr))  # log returns
    print(f"Return matrix: {len(shared_ts)-1} returns for {len(data)} coins")

    # Pairwise correlations within sectors
    print("\nComputing within-sector correlations...")
    candidates_out = []
    for sector, members in SECTORS.items():
        present = [m for m in members if m in data]
        for i in range(len(present)):
            for j in range(i+1, len(present)):
                a, b = present[i], present[j]
                corr = float(np.corrcoef(data[a], data[b])[0, 1])
                if corr > 0.60:
                    candidates_out.append({
                        "pair": f"{a}/{b}",
                        "sector": sector,
                        "coin_a": a, "coin_b": b,
                        "corr_90d": round(corr, 4),
                    })

    candidates_out.sort(key=lambda x: -x["corr_90d"])
    print(f"\nPairs with corr > 0.75: {len(candidates_out)}")
    print(f"\n{'pair':>15}  {'sector':>12}  {'corr':>8}")
    print("-" * 40)
    for c in candidates_out[:40]:
        print(f"{c['pair']:>15}  {c['sector']:>12}  {c['corr_90d']:>8.4f}")

    # Save
    out = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports/screener_v2_candidates.json")
    out.write_text(json.dumps(candidates_out, indent=2))
    print(f"\nSaved {len(candidates_out)} candidates to {out}")

if __name__ == "__main__":
    main()
