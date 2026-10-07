"""
Basket cointegration screener.

For each sector group, run Johansen on:
  (a) the full sector (all members with data)
  (b) the top-5-by-volume subset

Rank every cointegrating vector by ADF p-value, then report:
  - Basket weights (normalized)
  - Spread ADF p-value
  - OU half-life (hours)
  - Current z-score

Compare best basket in each sector vs best pair from the same sector,
so we can see whether baskets actually beat pairs on the same metrics.

Output:
  reports/basket_screen_YYYYMMDD_HHMMSS.json
  reports/basket_screen_YYYYMMDD_HHMMSS.csv
"""
from __future__ import annotations
import sys, time, json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")

import numpy as np
import pandas as pd
import requests

from src.hyperliquid_bot.stats.basket_cointegration import fit_basket_johansen
from src.hyperliquid_bot.stats.cointegration import fit_cointegration
from src.hyperliquid_bot.data.ohlcv import fetch_candles


HL_URL = "https://api.hyperliquid.xyz/info"
REPO = Path("/home/hlbot/hyperliquid-stat-arb-bot")
OUT_DIR = REPO / "reports"

SECTORS = {
    "l1_major": ["SOL", "AVAX", "ATOM", "DOT", "NEAR", "APT", "SUI", "SEI", "TIA", "INJ", "FTM"],
    "l2":       ["ARB", "OP", "STRK", "MANTA", "ZK"],
    "defi":     ["AAVE", "COMP", "MKR", "UNI", "CRV", "LDO", "SNX", "GMX", "DYDX", "PENDLE", "SUSHI"],
    "ai":       ["RNDR", "FET", "TAO"],
    "gaming":   ["IMX", "APE"],
    "infra":    ["FIL", "AR"],
    "meme":     ["DOGE", "kPEPE", "WIF"],
    "oracle":   ["LINK", "PYTH"],
}

LOOKBACK_HOURS = 24 * 90
INTERVAL = "1h"
MIN_BARS = 1500
TOP_N_SUBSET = 5


def post(req):
    r = requests.post(HL_URL, json=req, timeout=20)
    r.raise_for_status()
    return r.json()


def get_volumes() -> dict[str, float]:
    """24h notional volume per coin from HL."""
    try:
        data = post({"type": "metaAndAssetCtxs"})
        meta, ctxs = data[0], data[1]
        out = {}
        for i, asset in enumerate(meta.get("universe", [])):
            name = asset["name"]
            ctx = ctxs[i] if i < len(ctxs) else {}
            out[name] = float(ctx.get("dayNtlVlm", 0) or 0)
        return out
    except Exception as e:
        print(f"  volume fetch failed: {e}")
        return {}


def fetch_price_series(coin: str) -> dict[int, float] | None:
    try:
        candles = fetch_candles(coin, interval=INTERVAL, lookback_hours=LOOKBACK_HOURS)
        if not candles or len(candles) < MIN_BARS:
            return None
        return {c.t: c.c for c in candles}
    except Exception:
        return None


def build_matrix(series: dict[str, dict[int, float]]) -> tuple[list[str], np.ndarray]:
    """Align series on common timestamps, return (symbols, log_price_matrix)."""
    symbols = list(series.keys())
    ts_set = None
    for s in series.values():
        tset = set(s.keys())
        ts_set = tset if ts_set is None else ts_set & tset
    shared = sorted(ts_set)
    if not shared:
        return [], np.empty((0, 0))
    M = np.column_stack([
        np.log(np.array([series[sym][t] for t in shared]))
        for sym in symbols
    ])
    return symbols, M


def best_pair_in_group(symbols: list[str], log_prices: np.ndarray) -> dict | None:
    """Best pair from the same group, using simple OLS + ADF. Cheap."""
    from statsmodels.tsa.stattools import adfuller
    best = None
    k = len(symbols)
    for i in range(k):
        for j in range(i + 1, k):
            la = log_prices[:, i]
            lb = log_prices[:, j]
            try:
                beta = float(np.polyfit(lb, la, 1)[0])
                resid = la - beta * lb
                adf_p = float(adfuller(resid, maxlag=24, autolag=None,
                                        result_object=False)[1])
            except Exception:
                continue
            if best is None or adf_p < best["adf_pval"]:
                best = {
                    "pair": f"{symbols[i]}/{symbols[j]}",
                    "adf_pval": adf_p,
                    "beta": beta,
                }
    return best


def half_life(spread: np.ndarray) -> float:
    y = spread - spread.mean()
    lag = y[:-1]
    diff = np.diff(y)
    if lag.std() < 1e-12:
        return float("inf")
    theta = float(np.polyfit(lag, diff, 1)[0])
    return float(-np.log(2) / theta) if theta < 0 else float("inf")


def run_sector(name: str, members: list[str], volumes: dict[str, float]):
    print(f"\n=== {name} ({len(members)} members) ===")

    # Fetch series
    series = {}
    for m in members:
        s = fetch_price_series(m)
        if s:
            series[m] = s
            time.sleep(0.05)
    if len(series) < 3:
        print(f"  only {len(series)} members with data — skip")
        return []

    symbols_all, M_all = build_matrix(series)
    print(f"  aligned: {M_all.shape[0]} bars × {M_all.shape[1]} legs")

    out = []

    # --- Full-group Johansen ---
    if M_all.shape[1] >= 3:
        fit = fit_basket_johansen(M_all, symbols_all)
        if fit and fit.vectors:
            print(f"  full-group rank: {fit.rank}")
            for i, v in enumerate(fit.vectors):
                out.append({
                    "sector": name,
                    "scope": "full",
                    "rank_in_group": i,
                    "group_rank": fit.rank,
                    "n_legs": len(symbols_all),
                    "weights": {s: round(float(w), 4)
                                for s, w in zip(symbols_all, v.weights)},
                    "adf_pval": round(v.adf_pval, 5),
                    "half_life_hours": round(v.half_life_hours, 2)
                        if v.half_life_hours < 1e5 else None,
                    "z_current": round(v.z_current, 3),
                    "spread_sd": round(v.spread_sd, 5),
                    "eigenvalue": round(v.eigenvalue, 5),
                })
                print(f"    vec {i}: ADF p={v.adf_pval:.4f} "
                      f"HL={v.half_life_hours:.1f}h z={v.z_current:+.2f} "
                      f"weights={ {s: round(float(w),3) for s,w in zip(symbols_all, v.weights)} }")

    # --- Top-N subset Johansen ---
    ranked = sorted(series.keys(), key=lambda c: -volumes.get(c, 0))
    top = ranked[:TOP_N_SUBSET]
    if len(top) >= 3:
        sub_series = {c: series[c] for c in top}
        symbols_sub, M_sub = build_matrix(sub_series)
        if M_sub.shape[1] >= 3:
            fit = fit_basket_johansen(M_sub, symbols_sub)
            if fit and fit.vectors:
                print(f"  top-{TOP_N_SUBSET} rank: {fit.rank}  subset={symbols_sub}")
                for i, v in enumerate(fit.vectors):
                    out.append({
                        "sector": name,
                        "scope": f"top{TOP_N_SUBSET}",
                        "rank_in_group": i,
                        "group_rank": fit.rank,
                        "n_legs": len(symbols_sub),
                        "weights": {s: round(float(w), 4)
                                    for s, w in zip(symbols_sub, v.weights)},
                        "adf_pval": round(v.adf_pval, 5),
                        "half_life_hours": round(v.half_life_hours, 2)
                            if v.half_life_hours < 1e5 else None,
                        "z_current": round(v.z_current, 3),
                        "spread_sd": round(v.spread_sd, 5),
                        "eigenvalue": round(v.eigenvalue, 5),
                    })
                    print(f"    vec {i}: ADF p={v.adf_pval:.4f} "
                          f"HL={v.half_life_hours:.1f}h z={v.z_current:+.2f}")

    # --- Best pair in same group (baseline) ---
    best_pair = best_pair_in_group(symbols_all, M_all)
    if best_pair:
        out.append({
            "sector": name,
            "scope": "best_pair",
            "rank_in_group": 0,
            "group_rank": 1,
            "n_legs": 2,
            "weights": best_pair["pair"],
            "adf_pval": round(best_pair["adf_pval"], 5),
            "half_life_hours": None,
            "z_current": None,
            "spread_sd": None,
            "eigenvalue": None,
            "pair_beta": round(best_pair["beta"], 4),
        })
        print(f"  best pair: {best_pair['pair']} ADF p={best_pair['adf_pval']:.4f}")

    return out


def main():
    print("Basket cointegration screener")
    print("=" * 70)
    print(f"Lookback: {LOOKBACK_HOURS//24}d  Interval: {INTERVAL}  "
          f"Top-N subset: {TOP_N_SUBSET}")
    print()

    volumes = get_volumes()
    print(f"Loaded volumes for {len(volumes)} coins")

    results = []
    for name, members in SECTORS.items():
        try:
            results.extend(run_sector(name, members, volumes))
        except Exception as e:
            print(f"  sector {name} failed: {e}")

    if not results:
        print("\nNo results.")
        return

    df = pd.DataFrame(results)
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUT_DIR / f"basket_screen_{stamp}.csv"
    json_path = OUT_DIR / f"basket_screen_{stamp}.json"

    df.to_csv(csv_path, index=False)
    json_path.write_text(json.dumps(results, indent=2))

    # --- Ranked summary ---
    print("\n" + "=" * 90)
    print("RANKED BY ADF P-VALUE (lower = more stationary)")
    print("=" * 90)
    ranked = df.sort_values("adf_pval").reset_index(drop=True)
    for _, row in ranked.head(20).iterrows():
        w = row["weights"]
        w_str = str(w) if isinstance(w, str) else str({k: round(v, 2) for k, v in w.items()})
        print(f"  {row['sector']:>8} | {row['scope']:>9} | legs={row['n_legs']} | "
              f"ADF={row['adf_pval']:.4f} | HL={row['half_life_hours']} | "
              f"z={row['z_current']} | {w_str}")

    print(f"\nSaved: {csv_path}")
    print(f"Saved: {json_path}")


if __name__ == "__main__":
    main()
