"""
HIP-3 cointegration screener - working version.
Only screens the 'xyz' dex (only one with live candle data).
Assets are named like 'xyz:TSLA' - strip prefix for matching.
"""
import sys, time, json
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")

import requests
import numpy as np
from statsmodels.tsa.stattools import adfuller, coint
from pathlib import Path

HL_URL = "https://api.hyperliquid.xyz/info"

def post(req):
    r = requests.post(HL_URL, json=req, timeout=15)
    r.raise_for_status()
    return r.json()

# Broad sector groupings for xyz: assets (all prefixed forms)
SECTORS = {
    "semis": ["xyz:NVDA", "xyz:AMD", "xyz:INTC", "xyz:MU", "xyz:SMCI", "xyz:TSM"],
    "big_tech": ["xyz:AAPL", "xyz:MSFT", "xyz:GOOGL", "xyz:META", "xyz:AMZN"],
    "crypto_equities": ["xyz:COIN", "xyz:MSTR", "xyz:MARA", "xyz:HOOD", "xyz:CRCL"],
    "finance": ["xyz:JPM", "xyz:GS", "xyz:BAC", "xyz:WFC"],
    "ev": ["xyz:TSLA", "xyz:RIVN", "xyz:LCID", "xyz:NIO"],
    "indices": ["xyz:XYZ100", "xyz:SP500", "xyz:USTECH", "xyz:USA500"],
    "metals": ["xyz:GOLD", "xyz:SILVER", "xyz:COPPER"],
    "energy": ["xyz:OIL", "xyz:NATGAS"],
    "ai_compute": ["xyz:NVDA", "xyz:PLTR", "xyz:SMCI", "xyz:AMD"],
    "entertainment": ["xyz:NFLX", "xyz:DIS", "xyz:SPOT"],
}

def fetch_candles(coin, hours=24*90):
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - hours * 3600 * 1000
    try:
        r = requests.post(HL_URL, json={"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1h",
            "startTime": start_ms, "endTime": end_ms}}, timeout=20)
        if r.status_code != 200: return None
        data = r.json()
        if not data: return None
        return {int(c["t"]): float(c["c"]) for c in data}
    except Exception:
        return None

# Discover xyz: assets
print("Discovering HIP-3 assets...")
dexes = post({"type": "perpDexs"})
xyz_coins = []
for d in dexes:
    if d is None: continue
    name = d.get("name")
    if name != "xyz":
        continue
    try:
        meta = post({"type": "meta", "dex": name})
        xyz_coins = [a["name"] for a in meta.get("universe", [])]
    except Exception:
        pass

print(f"xyz: assets found: {len(xyz_coins)}")

# Filter to known sectors
sector_coins = set()
for members in SECTORS.values():
    sector_coins.update(members)

candidates = [c for c in xyz_coins if c in sector_coins]
print(f"Candidates in known sectors: {len(candidates)}")
print(candidates)

if len(candidates) < 3:
    print("\nNot enough candidates matched sectors. Available xyz assets are:")
    print(xyz_coins)
    sys.exit(0)

# Fetch prices
print("\nFetching candles (90 days each)...")
prices = {}
for coin in candidates:
    c = fetch_candles(coin)
    if c and len(c) > 500:
        prices[coin] = c
        print(f"  {coin}: {len(c)} bars")
    else:
        print(f"  {coin}: skip (only {len(c) if c else 0} bars)")
    time.sleep(0.1)

if len(prices) < 3:
    print("Not enough data")
    sys.exit(0)

# Align timestamps
shared = None
for c in prices.values():
    s = set(c.keys())
    shared = s if shared is None else shared & s
shared = sorted(shared)
print(f"\nCommon timestamps: {len(shared)}")
print(f"Coins with data: {list(prices.keys())}")

# Cointegration tests
print(f"\n{'pair':>24}  {'sector':>12}  {'EG p':>8}  {'ADF p':>8}  {'beta':>8}")
print("-" * 72)
results = []

for sector, members in SECTORS.items():
    present = [m for m in members if m in prices]
    for i in range(len(present)):
        for j in range(i+1, len(present)):
            a, b = present[i], present[j]
            log_a = np.log(np.array([prices[a][t] for t in shared]))
            log_b = np.log(np.array([prices[b][t] for t in shared]))
            try:
                eg_stat, eg_pval, _ = coint(log_a, log_b)
                beta = np.polyfit(log_b, log_a, 1)[0]
                residual = log_a - beta * log_b
                adf_stat, adf_pval, *_ = adfuller(residual, maxlag=24, autolag=None, result_object=False)
                if adf_pval < 0.10:
                    results.append({
                        "pair": f"{a}/{b}", "sector": sector,
                        "eg_pval": float(eg_pval), "adf_pval": float(adf_pval),
                        "beta": float(beta),
                    })
                    print(f"{a+'/'+b:>24}  {sector:>12}  {eg_pval:>8.4f}  {adf_pval:>8.4f}  {beta:>8.4f}")
            except Exception as e:
                pass

results.sort(key=lambda r: r["adf_pval"])
out = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports/screener_hip3.json")
out.write_text(json.dumps(results, indent=2))
print(f"\nSaved {len(results)} candidates to {out}")
print("ADF p-val < 0.05 = strongly cointegrated")
