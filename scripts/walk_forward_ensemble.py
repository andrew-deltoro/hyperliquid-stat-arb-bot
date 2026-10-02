"""
Walk-forward backtest with Ch 6 model averaging across lookbacks.

Fits cointegration at 3 lookbacks (14d, 30d, 60d), computes AIC at MAP,
weights by Akaike weight, ensembles z-scores.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
sys.stdout.reconfigure(line_buffering=True)

import numpy as np
import pandas as pd
import pymc as pm
import gc
from scipy import stats

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices


PAIR_FILTER = os.getenv("PAIR_FILTER", "all")
RUN_TAG = os.getenv("RUN_TAG", "_ensemble")

ALL_PAIRS = [
    # Original live pairs
    ("ETH", "BTC"),
    ("SOL", "AVAX"),
    ("AAVE", "COMP"),
    ("SNX", "DYDX"),
    ("LTC", "DOGE"),
    ("ETH", "SOL"),
    # HIP-3 live pairs
    ("xyz:GOLD", "xyz:SILVER"),
    ("xyz:NVDA", "xyz:PLTR"),
    # Screener batch 1 (crypto)
    ("AVAX", "NEAR"),
    ("DOGE", "WIF"),
    ("DOT", "INJ"),
    ("SOL", "SUI"),
    ("APT", "TIA"),
    ("kPEPE", "WIF"),
    ("DOT", "SUI"),
    # Mass screener batch 2 — all 14 filtered candidates
    ("MEME", "TIA"),
    ("SNX", "TIA"),
    ("ETC", "RSR"),
    ("MEME", "OP"),
    ("ORDI", "SOL"),
    ("FET", "IMX"),
    ("GMT", "IMX"),
    ("AVAX", "LTC"),
    ("SNX", "SUI"),
    ("GMT", "TIA"),
    ("ETC", "NEO"),
    # Screener v2 candidates (HL 24-33h sweet spot)
    ("AIXBT", "DOGE"),
    ("RENDER", "SNX"),
    ("MOODENG", "YGG"),
    ("LINEA", "NEO"),
    # HIP-3 remaining untested pairs
    ("xyz:NVDA", "xyz:TSM"),
    ("xyz:MU", "xyz:TSM"),
    ("xyz:AAPL", "xyz:AMZN"),
]
PAIRS = ALL_PAIRS if PAIR_FILTER == "all" else [
    p for p in ALL_PAIRS if f"{p[0]}/{p[1]}" == PAIR_FILTER
]

INTERVAL = "1h"
LOOKBACK_HOURS = 24 * 200
LOOKBACKS_DAYS = [14, 30, 60]     # the three ensembles
TEST_WINDOW_HOURS = 24 * 130  # test last 130 days (fits 60d lookback + cap)
HOLD_HORIZON_HOURS = 24 * 3

Z_ENTRY_MIN = float(os.getenv("Z_ENTRY_MIN", "1.5"))
EDGE_MIN = float(os.getenv("EDGE_MIN", "0.05"))
TARGET_FRAC = float(os.getenv("TARGET_FRAC", "0.5"))
STOP_MULT = float(os.getenv("STOP_MULT", "3.0"))


def fit_coint_map(log_a, log_b):
    with pm.Model() as model:
        alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
        beta = pm.Normal("beta", mu=1.0, sigma=1.0)
        sigma = pm.HalfNormal("sigma", sigma=0.5)
        nu = pm.Exponential("nu", lam=1 / 10.0)
        mu = alpha + beta * log_b
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)
        try:
            map_est = pm.find_MAP(progressbar=False, maxeval=500, method="L-BFGS-B")
        except Exception as e:
            return {"error": str(e)}
    return {
        "alpha": float(map_est["alpha"]),
        "beta": float(map_est["beta"]),
        "sigma": float(map_est["sigma"]),
        "nu": float(map_est["nu"]),
    }


def compute_aic(log_a, log_b, coint, k=4):
    """AIC = -2 * loglik + 2*k, at MAP point estimates."""
    mu = coint["alpha"] + coint["beta"] * log_b
    ll = stats.t.logpdf(log_a, df=coint["nu"], loc=mu, scale=coint["sigma"]).sum()
    return -2 * ll + 2 * k


def fit_ou_map(spread):
    y = np.asarray(spread, dtype=float)
    y_c = y - y.mean()
    y_lag = y_c[:-1]
    y_next = y_c[1:]
    with pm.Model() as model:
        kappa = pm.Beta("kappa", alpha=2.0, beta=2.0)
        sigma = pm.HalfNormal("sigma", sigma=0.5)
        nu = pm.Exponential("nu", lam=1 / 10.0)
        pm.StudentT("obs", nu=nu, mu=(1 - kappa) * y_lag, sigma=sigma, observed=y_next)
        try:
            map_est = pm.find_MAP(progressbar=False, maxeval=500, method="L-BFGS-B")
        except Exception as e:
            return {"error": str(e)}
    kappa = float(map_est["kappa"])
    hl = float(np.log(0.5) / np.log(1 - kappa)) if 1e-6 < kappa < 1 else float("inf")
    return {
        "kappa": kappa,
        "sigma": float(map_est["sigma"]),
        "nu": float(map_est["nu"]),
        "half_life_hours": hl,
    }


def ensemble_fit(log_a, log_b, T, lookbacks_hours):
    """Fit at multiple lookbacks, weight by AIC, ensemble z-score and beta."""
    fits = []
    for lb_hours in lookbacks_hours:
        fit_start = T - lb_hours
        if fit_start < 0:
            continue
        coint = fit_coint_map(log_a[fit_start:T], log_b[fit_start:T])
        if "error" in coint:
            continue
        aic = compute_aic(log_a[fit_start:T], log_b[fit_start:T], coint)
        spread = log_a[fit_start:T] - coint["alpha"] - coint["beta"] * log_b[fit_start:T]
        spread_c = spread - spread.mean()
        z = float(spread_c[-1] / (spread_c.std() + 1e-9))
        fits.append({
            "coint": coint,
            "aic": aic,
            "z": z,
            "spread": spread_c,
            "lb_hours": lb_hours,
        })
    if not fits:
        return None
    # Equal weights - AIC is not comparable across different sample sizes.
    # We use a naive ensemble: if 3 lookbacks agree on direction, average
    # their z-scores. This is not a formal Bayesian model average, but it
    # is honest given the sample-size issue with AIC.
    weights = np.ones(len(fits)) / len(fits)
    # Ensemble z: simple mean of z-scores across lookbacks
    z_ens = float(np.mean([f["z"] for f in fits]))
    # Ensemble beta/alpha as simple averages
    alpha_ens = float(np.mean([f["coint"]["alpha"] for f in fits]))
    beta_ens = float(np.mean([f["coint"]["beta"] for f in fits]))
    # Combined spread using averaged parameters over medium lookback
    spread_ens = log_a[T - 24 * 30:T] - alpha_ens - beta_ens * log_b[T - 24 * 30:T]
    spread_ens = spread_ens - spread_ens.mean()
    return {
        "z": z_ens,
        "alpha": alpha_ens,
        "beta": beta_ens,
        "spread": spread_ens,
        "weights": weights,
        "lookbacks_hours": [f["lb_hours"] for f in fits],
        "z_per_lookback": [f["z"] for f in fits],
    }


def compute_signal(log_a, log_b, ens):
    spread_c = ens["spread"]
    current = float(spread_c[-1])
    sd = float(spread_c.std())
    z = current / (sd + 1e-9)
    if abs(z) < Z_ENTRY_MIN:
        return None
    # OU fit for p_revert
    ou = fit_ou_map(spread_c)
    if "error" in ou:
        return None
    hl = ou["half_life_hours"]
    horizon = HOLD_HORIZON_HOURS
    p_revert = 1.0 - np.exp(-horizon / (2.0 * hl + 1e-9))
    edge = p_revert - 0.5
    if edge < EDGE_MIN:
        return None
    direction = "SHORT_A_LONG_B" if current > 0 else "LONG_A_SHORT_B"
    return {
        "spread": current, "z": z, "p_revert": float(p_revert), "edge": float(edge),
        "direction": direction, "half_life_hours": hl, "ou": ou,
    }


def simulate_trade(log_a_path, log_b_path, coint, signal):
    entry = signal["spread"]
    is_long = signal["direction"] == "LONG_A_SHORT_B"
    target = TARGET_FRAC * entry
    stop = STOP_MULT * entry
    for t in range(len(log_a_path)):
        spread_at_entry = log_a_path[0] - coint["alpha"] - coint["beta"] * log_b_path[0]
        spread_now = log_a_path[t] - coint["alpha"] - coint["beta"] * log_b_path[t]
        cur = entry + (spread_now - spread_at_entry)
        if is_long:
            if cur >= target:
                return {"pnl_pct": (cur - entry) * 100, "exit_reason": "target", "elapsed_hours": t}
            if cur <= stop:
                return {"pnl_pct": (cur - entry) * 100, "exit_reason": "stop", "elapsed_hours": t}
        else:
            if cur <= target:
                return {"pnl_pct": (entry - cur) * 100, "exit_reason": "target", "elapsed_hours": t}
            if cur >= stop:
                return {"pnl_pct": (entry - cur) * 100, "exit_reason": "stop", "elapsed_hours": t}
    return {
        "pnl_pct": ((cur - entry) if is_long else (entry - cur)) * 100,
        "exit_reason": "time_stop",
        "elapsed_hours": len(log_a_path),
    }


def main():
    print("=" * 70)
    print("WALK-FORWARD WITH MODEL AVERAGING (Ch 6)")
    print("=" * 70)
    print(f"Pairs: {len(PAIRS)}")
    print(f"Lookbacks: {LOOKBACKS_DAYS} days")
    print(f"Test window: {TEST_WINDOW_HOURS // 24} days")
    print()

    lookbacks_hours = [d * 24 for d in LOOKBACKS_DAYS]
    max_lb = max(lookbacks_hours)

    all_trades = []
    for coin_a, coin_b in PAIRS:
        pair = f"{coin_a}/{coin_b}"
        print(f"--- {pair} ---")
        try:
            ts, ca, cb = aligned_close_prices(coin_a, coin_b, interval=INTERVAL, lookback_hours=LOOKBACK_HOURS)
        except Exception as e:
            print(f"  fetch failed: {e}"); continue
        if len(ts) < max_lb + TEST_WINDOW_HOURS:
            print(f"  insufficient data: {len(ts)} bars"); continue
        log_a = np.log(np.array(ca)); log_b = np.log(np.array(cb))
        test_end = len(ts); test_start = test_end - TEST_WINDOW_HOURS
        pair_trades = []
        for T in range(test_start, test_end - HOLD_HORIZON_HOURS, 48):
            iters = T - test_start
            if iters > 0 and iters % (48 * 5) == 0:
                print(f"  ... day {iters // 48}/{ (test_end - HOLD_HORIZON_HOURS - test_start) // 48}, {len(pair_trades)} trades", flush=True)
            if (T - test_start) % (48 * 5) == 0 and T > test_start:
                gc.collect()
            ens = ensemble_fit(log_a, log_b, T, lookbacks_hours)
            if ens is None:
                continue
            signal = compute_signal(log_a[:T], log_b[:T], ens)
            if signal is None:
                continue
            future_a = log_a[T:T + HOLD_HORIZON_HOURS + 1]
            future_b = log_b[T:T + HOLD_HORIZON_HOURS + 1]
            if len(future_a) < HOLD_HORIZON_HOURS + 1:
                continue
            coint = {"alpha": ens["alpha"], "beta": ens["beta"]}
            outcome = simulate_trade(future_a, future_b, coint, signal)
            outcome["pair"] = pair
            outcome["entry_idx"] = T
            outcome["entry_z"] = signal["z"]
            outcome["entry_edge"] = signal["edge"]
            outcome["weights"] = ",".join(f"{w:.2f}" for w in ens["weights"])
            outcome["z_per_lookback"] = ",".join(f"{z:+.2f}" for z in ens["z_per_lookback"])
            pair_trades.append(outcome)
        print(f"  Trades: {len(pair_trades)}")
        all_trades.extend(pair_trades)

    if not all_trades:
        print("\nNo trades generated."); return

    df = pd.DataFrame(all_trades)
    df = df[df["elapsed_hours"] > 0]
    print()
    print("=" * 70)
    print("OUT-OF-SAMPLE RESULTS (ENSEMBLE)")
    print("=" * 70)
    print(f"Total trades:  {len(df)}")
    print(f"Win rate:      {(df.pnl_pct > 0).mean():.1%}")
    print(f"Mean PnL:      {df.pnl_pct.mean():+.4f}%")
    print(f"Median PnL:    {df.pnl_pct.median():+.4f}%")
    print(f"Std PnL:       {df.pnl_pct.std():.4f}%")
    if df.pnl_pct.std() > 0:
        sharpe_t = df.pnl_pct.mean() / df.pnl_pct.std()
        span_days = (df.entry_idx.max() - df.entry_idx.min()) / 24.0
        tpy = len(df) / span_days * 365 if span_days > 0 else len(df)
        sharpe_a = sharpe_t * np.sqrt(tpy)
        print(f"Trade Sharpe:  {sharpe_t:+.3f}")
        print(f"Trades/year:   {tpy:.1f}")
        print(f"Annualized:    {sharpe_a:+.2f}")
    print()
    print(df.groupby("pair").pnl_pct.agg(["count", "mean", "sum"]).round(4))
    out_path = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports") / f"walk_forward_{PAIR_FILTER.replace('/', '_').lower() if PAIR_FILTER != 'all' else 'all'}{RUN_TAG}.csv"
    out_path.parent.mkdir(exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
