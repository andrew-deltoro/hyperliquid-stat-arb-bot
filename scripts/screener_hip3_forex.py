"""HIP-3 forex screener. Screens EUR, JPY and other forex pairs for cointegration."""
from __future__ import annotations
import sys, time, json
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")

import requests
import numpy as np
from statsmodels.tsa.stattools import adfuller, coint
from pathlib import Path

HL_URL = "https://api.hyperliquid.xyz/info"


def post(req):
    r = requests.post(HL_URL, json=req, timeout=20)
    r.raise_for_status()
    return r.json()


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


print("Discovering HIP-3 assets...")
dexes = post({"type": "perpDexs"})

forex_candidates = []
for d in dexes:
    if d is None: continue
    name = d.get("name", "")
    if name != "xyz": continue
    try:
        meta = post({"type": "meta", "dex": name})
        for a in meta.get("universe", []):
            coin = a["name"]
            if any(x in coin.upper() for x in ["EUR", "JPY", "GBP", "AUD", "CAD", "CHF", "CNH"]):
                forex_candidates.append(coin)
    except Exception as e:
        print(f"  error on dex {name}: {e}")

print(f"Forex candidates: {forex_candidates}")

hedge_pairs = ["xyz:BTC", "xyz:ETH"]
all_symbols = list(set(forex_candidates + hedge_pairs))
print(f"Fetching {len(all_symbols)} symbols...")

prices = {}
for sym in all_symbols:
    c = fetch_candles(sym)
    if c and len(c) > 500:
        prices[sym] = c
        print(f"  {sym}: {len(c)} bars")
    time.sleep(0.1)

if len(prices) < 2:
    print(f"\nNot enough data. Available: {list(prices.keys())}")
    sys.exit(0)

shared = None
for c in prices.values():
    s = set(c.keys())
    shared = s if shared is None else shared & s
shared = sorted(shared)
print(f"\nCommon timestamps: {len(shared)}")

print(f"\n{'pair':>28}  {'EG p':>8}  {'ADF p':>8}  {'corr':>7}  {'beta':>7}")
print("-" * 66)
results = []
symbols = list(prices.keys())
for i in range(len(symbols)):
    for j in range(i+1, len(symbols)):
        a, b = symbols[i], symbols[j]
        log_a = np.log(np.array([prices[a][t] for t in shared]))
        log_b = np.log(np.array([prices[b][t] for t in shared]))
        try:
            eg_stat, eg_pval, _ = coint(log_a, log_b)
            beta = float(np.polyfit(log_b, log_a, 1)[0])
            residual = log_a - beta * log_b
            adf_stat, adf_pval, *_ = adfuller(residual, maxlag=24, autolag=None, result_object=False)
            corr = float(np.corrcoef(np.diff(log_a), np.diff(log_b))[0, 1])
            results.append({"pair": f"{a}/{b}", "eg_pval": float(eg_pval),
                            "adf_pval": float(adf_pval), "corr": corr, "beta": beta})
            if eg_pval < 0.20 or adf_pval < 0.20:
                print(f"{a+'/'+b:>28}  {eg_pval:>8.4f}  {adf_pval:>8.4f}  {corr:>7.4f}  {beta:>7.3f}")
        except Exception:
            pass

results.sort(key=lambda r: r["adf_pval"])
out = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports/screener_hip3_forex.json")
out.write_text(json.dumps(results, indent=2))
print(f"\nSaved {len(results)} candidates to {out}")
