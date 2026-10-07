"""
Basket structure stability check.

For each sector, fit Johansen on rolling windows and measure:
  1. Rank stability (does the group always show the same rank?)
  2. Eigenvector stability (cosine similarity between consecutive windows)
  3. Sign consistency of the top-weighted legs
  4. ADF p-value distribution
  5. Leg overlap (which coins are always in the top-5 |weights|)

If the structure is stable, the same coins appear with consistent signs
and similar magnitudes across windows. If it's noise, weights swing wildly.

Usage:
    python scripts/basket_stability.py                     # all sectors of interest
    python scripts/basket_stability.py --sector defi       # one sector
"""
from __future__ import annotations
import sys, time, json, argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")

import numpy as np
import requests
from src.hyperliquid_bot.stats.basket_cointegration import fit_basket_johansen
from src.hyperliquid_bot.data.ohlcv import fetch_candles


HL_URL = "https://api.hyperliquid.xyz/info"
REPO = Path("/home/hlbot/hyperliquid-stat-arb-bot")
OUT_DIR = REPO / "reports"

SECTORS = {
    "l1_major": ["SOL", "AVAX", "ATOM", "DOT", "NEAR", "APT", "SUI", "SEI", "TIA", "INJ", "FTM"],
    "defi":     ["AAVE", "COMP", "MKR", "UNI", "CRV", "LDO", "SNX", "GMX", "DYDX", "PENDLE", "SUSHI"],
    "meme":     ["DOGE", "kPEPE", "WIF"],
}

# Window config: 5 windows of 60d each, spaced 30d apart = 180d total
LOOKBACK_HOURS = 24 * 180
WINDOW_HOURS = 24 * 90
STEP_HOURS = 24 * 30
INTERVAL = "1h"


def _post(req):
    r = requests.post(HL_URL, json=req, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_series(coin: str) -> dict[int, float] | None:
    try:
        candles = fetch_candles(coin, interval=INTERVAL, lookback_hours=LOOKBACK_HOURS)
        if not candles or len(candles) < 1000:
            return None
        return {c.t: c.c for c in candles}
    except Exception:
        return None


def build_matrix(series: dict[str, dict[int, float]]) -> tuple[list[str], np.ndarray, list[int]]:
    ts_set = None
    for s in series.values():
        tset = set(s.keys())
        ts_set = tset if ts_set is None else ts_set & tset
    shared = sorted(ts_set)
    symbols = list(series.keys())
    M = np.column_stack([
        np.log(np.array([series[sym][t] for t in shared]))
        for sym in symbols
    ])
    return symbols, M, shared


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-12 or nb < 1e-12:
        return 0.0
    return float(abs(np.dot(a, b)) / (na * nb))


def top_k_by_abs(w: np.ndarray, symbols: list[str], k: int = 5):
    idx = np.argsort(-np.abs(w))[:k]
    return [(symbols[i], float(w[i])) for i in idx]


def sign_of_top(w: np.ndarray, symbols: list[str], k: int = 5) -> dict[str, int]:
    """Sign of top-|w| legs. +1, -1."""
    out = {}
    for name, val in top_k_by_abs(w, symbols, k):
        out[name] = 1 if val > 0 else (-1 if val < 0 else 0)
    return out


def stability_report(sector_name: str, series: dict, window_hours: int, step_hours: int):
    symbols, M, ts = build_matrix(series)
    n_bars = M.shape[0]
    print(f"\n=== {sector_name} stability ({len(symbols)} legs, {n_bars} bars) ===")
    print(f"Symbols: {symbols}")

    window_bars = window_hours
    step_bars = step_hours

    windows = []
    start = 0
    while start + window_bars <= n_bars:
        end = start + window_bars
        windows.append((start, end))
        start += step_bars

    print(f"Windows: {len(windows)} × {window_bars} bars, step {step_bars} bars")
    print()

    if len(windows) < 2:
        print("  Not enough data for multiple windows")
        return None

    # Fit Johansen in each window
    vectors_per_window = []
    ranks_per_window = []
    adfs_per_window = []
    ts_per_window = []

    for i, (s, e) in enumerate(windows):
        Mwin = M[s:e]
        fit = fit_basket_johansen(Mwin, symbols)
        if fit is None or fit.rank == 0:
            ranks_per_window.append(0)
            vectors_per_window.append(None)
            adfs_per_window.append(None)
            ts_per_window.append(ts[s])
            print(f"  win {i}: rank=0 (no coint)")
            continue
        v0 = fit.vectors[0]
        vectors_per_window.append(v0.weights)
        ranks_per_window.append(fit.rank)
        adfs_per_window.append(v0.adf_pval)
        ts_per_window.append(ts[s])
        dt = datetime.fromtimestamp(ts[s] / 1000).strftime("%Y-%m-%d")
        print(f"  win {i} ({dt}): rank={fit.rank} ADF={v0.adf_pval:.4f} "
              f"HL={v0.half_life_hours:.1f}h")

    # Stability metrics
    print()
    print("--- Rank stability ---")
    print(f"  ranks: {ranks_per_window}")
    rank_mode = max(set(ranks_per_window), key=ranks_per_window.count)
    rank_stable = all(r == rank_mode for r in ranks_per_window)
    print(f"  mode={rank_mode}  {'STABLE' if rank_stable else 'UNSTABLE'}")

    print()
    print("--- ADF p-value stability ---")
    adfs_valid = [a for a in adfs_per_window if a is not None]
    if adfs_valid:
        print(f"  p-values: {[round(a, 4) for a in adfs_valid]}")
        print(f"  mean={np.mean(adfs_valid):.4f}  max={max(adfs_valid):.4f}")
        if max(adfs_valid) < 0.05:
            print("  ALL windows pass ADF < 0.05  ✓")
        elif max(adfs_valid) < 0.10:
            print("  Most windows pass ADF < 0.10  ~")
        else:
            print("  Some windows fail ADF  ✗")

    # Cosine similarity between consecutive windows
    print()
    print("--- Eigenvector stability (cosine similarity) ---")
    sims = []
    for i in range(1, len(vectors_per_window)):
        if vectors_per_window[i] is None or vectors_per_window[i - 1] is None:
            continue
        s = cosine_sim(vectors_per_window[i - 1], vectors_per_window[i])
        sims.append(s)
        print(f"  win {i-1} → win {i}: cos={s:.3f}")
    if sims:
        print(f"  mean={np.mean(sims):.3f}  min={min(sims):.3f}")
        if np.mean(sims) > 0.9:
            print("  STRONG stability")
        elif np.mean(sims) > 0.7:
            print("  MODERATE stability")
        else:
            print("  WEAK stability (structure is unstable)")

    # Sign consistency of top-5 legs
    print()
    print("--- Top-5 leg sign consistency ---")
    signs_per_window = []
    top5_per_window = []
    for w, syms in [(v, symbols) for v in vectors_per_window if v is not None]:
        signs_per_window.append(sign_of_top(w, syms, 5))
        top5_per_window.append([s for s, _ in top_k_by_abs(w, syms, 5)])

    if signs_per_window:
        # For each coin, collect its signs across windows where it appeared
        coin_signs: dict[str, list[int]] = {}
        for sign_dict in signs_per_window:
            for coin, sgn in sign_dict.items():
                coin_signs.setdefault(coin, []).append(sgn)

        print(f"  {'coin':>8}  {'appearances':>11}  {'signs':>20}  {'consistent?':>11}")
        for coin, signs in sorted(coin_signs.items(),
                                    key=lambda x: -len(x[1])):
            consistent = len(set(signs)) == 1
            print(f"  {coin:>8}  {len(signs):>11}  {str(signs):>20}  "
                  f"{'YES' if consistent else 'NO':>11}")

    # Top-5 leg overlap (Jaccard across consecutive windows)
    print()
    print("--- Top-5 leg overlap (Jaccard) ---")
    jaccards = []
    for i in range(1, len(top5_per_window)):
        a = set(top5_per_window[i - 1])
        b = set(top5_per_window[i])
        j = len(a & b) / len(a | b) if (a | b) else 0
        jaccards.append(j)
        print(f"  win {i-1} → win {i}: J={j:.2f}  "
              f"(overlap: {sorted(a & b)})")
    if jaccards:
        print(f"  mean Jaccard={np.mean(jaccards):.2f}")

    print()
    print("--- VERDICT ---")
    if rank_stable and sims and np.mean(sims) > 0.85 and np.mean(jaccards) > 0.5:
        print(f"  ✅ {sector_name}: structure is STABLE across windows")
    elif rank_stable and sims and np.mean(sims) > 0.6:
        print(f"  ⚠️  {sector_name}: structure is WEAKLY stable")
    else:
        print(f"  ❌ {sector_name}: structure is UNSTABLE")

    return {
        "sector": sector_name,
        "symbols": symbols,
        "ranks": ranks_per_window,
        "adfs": adfs_per_window,
        "cosine_sims": sims,
        "top5_overlap": jaccards,
        "top5_per_window": top5_per_window,
        "signs_per_window": signs_per_window,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sector", default="all",
                        help="sector name or 'all'")
    parser.add_argument("--top-n", type=int, default=0,
                        help="restrict to top-N by volume per sector (0=all)")
    args = parser.parse_args()

    print("Basket stability check")
    print("=" * 70)
    print(f"Lookback: {LOOKBACK_HOURS//24}d  Window: {WINDOW_HOURS//24}d  "
          f"Step: {STEP_HOURS//24}d")

    if args.sector == "all":
        sectors_to_run = SECTORS
    else:
        sectors_to_run = {args.sector: SECTORS[args.sector]}

    # Fetch all needed series once
    all_coins = set()
    for members in sectors_to_run.values():
        all_coins.update(members)

    print(f"\nFetching {len(all_coins)} coins...")
    series_cache: dict[str, dict[int, float]] = {}
    for i, coin in enumerate(sorted(all_coins)):
        if i % 5 == 0:
            print(f"  [{i}/{len(all_coins)}]...")
        s = fetch_series(coin)
        if s:
            series_cache[coin] = s
        time.sleep(0.05)

    results = {}
    for name, members in sectors_to_run.items():
        sector_series = {m: series_cache[m] for m in members if m in series_cache}
        if args.top_n > 0 and len(sector_series) > args.top_n:
            # Rank by 24h volume; needs a volumes dict, which we'll fetch
            import requests as _rq
            try:
                _r = _rq.post("https://api.hyperliquid.xyz/info",
                              json={"type": "metaAndAssetCtxs"}, timeout=20)
                _d = _r.json()
                _meta, _ctxs = _d[0], _d[1]
                _vols = {}
                for _i, _a in enumerate(_meta.get("universe", [])):
                    _vols[_a["name"]] = float((_ctxs[_i] or {}).get("dayNtlVlm", 0) or 0)
            except Exception:
                _vols = {}
            sector_series = dict(sorted(sector_series.items(),
                                         key=lambda kv: -_vols.get(kv[0], 0))[:args.top_n])
        if len(sector_series) < 3:
            print(f"\n=== {name} === skipped ({len(sector_series)} coins with data)")
            continue
        r = stability_report(name, sector_series, WINDOW_HOURS, STEP_HOURS)
        if r:
            results[name] = r

    # Save
    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"basket_stability_{stamp}.json"

    def default(o):
        if isinstance(o, (np.integer, np.floating)):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        return str(o)

    out_path.write_text(json.dumps(results, indent=2, default=default))
    print(f"\nSaved: {out_path}")

    # Global summary
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, r in results.items():
        sims = r["cosine_sims"]
        jacs = r["top5_overlap"]
        print(f"  {name:>10}: mean cos={np.mean(sims):.3f}  "
              f"mean Jaccard={np.mean(jacs):.2f}  "
              f"ranks={r['ranks']}")


if __name__ == "__main__":
    main()
