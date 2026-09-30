"""
Quick screener: test cointegration across a curated candidate list.
Not the full dynamic screener — this is the 1-hour concept test.
"""
import sys, time
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot/src")

import numpy as np
from hyperliquid_bot.data.ohlcv import aligned_close_prices
from hyperliquid_bot.stats.cointegration import fit_cointegration

# Candidate pairs grouped by semantic similarity.
# These are the most likely cointegrated pairs based on sector / correlation structure.
CANDIDATES = [
    # Layer 1 majors (BTC-correlated)
    ("ETH", "BTC"), ("SOL", "AVAX"), ("AVAX", "MATIC"), ("MATIC", "ETH"),
    ("SOL", "MATIC"), ("APT", "SUI"), ("APT", "ARB"), ("SUI", "ARB"),
    # Layer 2s
    ("ARB", "OP"), ("ARB", "MATIC"), ("OP", "MATIC"),
    # DeFi
    ("AAVE", "COMP"), ("AAVE", "MKR"), ("COMP", "MKR"),
    ("CRV", "LDO"), ("CRV", "SNX"), ("LDO", "SNX"),
    ("UNI", "SUSHI"), ("UNI", "CRV"), ("UNI", "AAVE"),
    # Oracles
    ("LINK", "PYTH"), ("LINK", "BAND"),
    # Memes
    ("DOGE", "kPEPE"), ("DOGE", "WIF"), ("kPEPE", "WIF"), ("kPEPE", "BONK"),
    # Alt-L1s
    ("DYDX", "GMX"), ("DYDX", "SNX"), ("GMX", "SNX"),
    # Storage / infra
    ("FIL", "AR"),
    # Staking
    ("STX", "RNDR"),
    # Cross-sector possible
    ("AAVE", "UNI"), ("LINK", "AAVE"),
]

LOOKBACK_HOURS = 24 * 30  # 30 days for fitting

print(f"{'pair':>15}  {'beta':>8}  {'sigma':>8}  {'nu':>6}  {'conv':>5}  {'time':>6}")
print("-" * 65)

results = []
for coin_a, coin_b in CANDIDATES:
    pair = f"{coin_a}/{coin_b}"
    try:
        ts, ca, cb = aligned_close_prices(coin_a, coin_b, interval="1h", lookback_hours=LOOKBACK_HOURS)
        if len(ts) < 500:
            print(f"{pair:>15}  insufficient data ({len(ts)})")
            continue
        log_a = np.log(np.array(ca))
        log_b = np.log(np.array(cb))
        t0 = time.time()
        fit = fit_cointegration(log_a, log_b, draws=1000, tune=1000, chains=2, cores=2)
        elapsed = time.time() - t0
        results.append({
            "pair": pair,
            "beta": fit.beta_mean,
            "sigma": fit.sigma_mean,
            "nu": fit.nu_mean,
            "converged": fit.converged,
            "elapsed": elapsed,
        })
        flag = "✓" if fit.converged else "✗"
        print(f"{pair:>15}  {fit.beta_mean:+8.4f}  {fit.sigma_mean:8.4f}  {fit.nu_mean:6.2f}  {flag:>5}  {elapsed:5.1f}s")
    except Exception as e:
        print(f"{pair:>15}  ERROR: {e}")

print()
print("=" * 65)
print("TOP CANDIDATES BY LOW SIGMA (tightest spread = best signal)")
print("=" * 65)
ok = [r for r in results if r["converged"]]
ok.sort(key=lambda r: r["sigma"])
for r in ok[:10]:
    print(f"  {r['pair']:>15}  beta={r['beta']:+.4f}  sigma={r['sigma']:.4f}  nu={r['nu']:.1f}")
