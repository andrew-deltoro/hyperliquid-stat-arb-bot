"""
Cointegration-first screener.
For each within-sector pair, run Engle-Granger and ADF tests.
Keep pairs where p < 0.05 on ADF of residuals.
"""
import sys, time, json
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot/src")

import requests
import numpy as np
from statsmodels.tsa.stattools import adfuller, coint
from pathlib import Path

HL_URL = "https://api.hyperliquid.xyz/info"

# Sector universe (from screener v2)
SECTORS = {
    "btc_eth": ["BTC", "ETH"],
    "l1_major": ["SOL", "AVAX", "ATOM", "DOT", "NEAR", "APT", "SUI", "SEI", "TIA", "INJ", "FTM"],
    "l2": ["ARB", "OP", "STRK", "MANTA", "ZK"],
    "defi": ["AAVE", "COMP", "MKR", "UNI", "CRV", "LDO", "SNX", "GMX", "DYDX", "PENDLE", "SUSHI"],
    "oracle": ["LINK", "PYTH"],
    "meme": ["DOGE", "kPEPE", "WIF"],
    "ai": ["RNDR", "FET", "TAO"],
    "gaming": ["IMX", "APE"],
    "infra": ["FIL", "AR"],
}

def fetch_candles(coin, interval="1h", hours=24*90):
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - hours * 3600 * 1000
    try:
        r = requests.post(HL_URL, json={"type": "candleSnapshot", "req": {
            "coin": coin, "interval": interval,
            "startTime": start_ms, "endTime": end_ms}}, timeout=20)
        if r.status_code != 200: return None
        data = r.json()
        if not data: return None
        return {int(c["t"]): float(c["c"]) for c in data}
    except Exception:
        return None

# Fetch
print("Fetching prices...")
prices = {}
for sector, members in SECTORS.items():
    for coin in members:
        if coin in prices: continue
        c = fetch_candles(coin)
        if c and len(c) > 1000:
            prices[coin] = c
        time.sleep(0.05)

# Align
shared = None
for c in prices.values():
    s = set(c.keys())
    shared = s if shared is None else shared & s
shared = sorted(shared)
print(f"Common timestamps: {len(shared)}")
print(f"Coins with data: {list(prices.keys())}")

# Cointegration tests within each sector
print(f"\n{'pair':>15}  {'sector':>10}  {'EG p-val':>10}  {'ADF p-val':>10}  {'beta':>8}")
print("-" * 70)
results = []

for sector, members in SECTORS.items():
    present = [m for m in members if m in prices]
    for i in range(len(present)):
        for j in range(i+1, len(present)):
            a, b = present[i], present[j]
            log_a = np.log(np.array([prices[a][t] for t in shared]))
            log_b = np.log(np.array([prices[b][t] for t in shared]))
            try:
                # Engle-Granger: tests if log_a and log_b are cointegrated
                eg_stat, eg_pval, _ = coint(log_a, log_b)
                # Also compute ADF on the OLS residual directly
                from numpy.polynomial import polynomial as P
                beta = np.polyfit(log_b, log_a, 1)[0]
                residual = log_a - beta * log_b
                adf_stat, adf_pval, *_ = adfuller(residual, maxlag=24, autolag=None, result_object=False)
                if eg_pval < 0.10:  # filter loose, print all under 0.10
                    results.append({
                        "pair": f"{a}/{b}", "sector": sector,
                        "eg_pval": eg_pval, "adf_pval": adf_pval, "beta": beta,
                    })
                    print(f"{a + '/' + b:>15}  {sector:>10}  {eg_pval:>10.4f}  {adf_pval:>10.4f}  {beta:>8.4f}")
            except Exception as e:
                pass

# Sort by ADF p-value (lowest = most strongly cointegrated)
results.sort(key=lambda r: r["adf_pval"])
out_path = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports/screener_coint.json")
out_path.write_text(json.dumps(results, indent=2))
print(f"\nSaved {len(results)} candidates to {out_path}")
print("\nDecision rule: ADF p-val < 0.05 = cointegrated (tradeable)")
