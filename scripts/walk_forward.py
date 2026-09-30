"""
Walk-forward backtest for the stat-arb bot.

For each day T over the last N days:
  1. Fetch candles up to T (only past data)
  2. Fit Bayesian cointegration with MAP (fast)
  3. Fit OU mean-reversion with MAP
  4. Compute signal (z-score, P(revert), edge)
  5. If OPEN signal, simulate the trade forward
  6. Record outcome: PnL, exit reason, duration

Aggregate: out-of-sample win rate, Sharpe, max drawdown, equity curve.

Uses pm.find_MAP() for speed (100x faster than MCMC). Same model
structure as the live bot.
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(line_buffering=True)

import numpy as np
import pandas as pd
import pymc as pm
import gc


from src.hyperliquid_bot.data.ohlcv import aligned_close_prices


# ============================================================
# CONFIG
# ============================================================
import os
PAIR_FILTER = os.getenv("PAIR_FILTER", "all")

ALL_PAIRS = [
    ("LINK", "PYTH"),
    ("UNI", "SUSHI"),
    ("ETH", "BTC"),
    ("SOL", "AVAX"),
    ("AAVE", "COMP"),
]

if PAIR_FILTER == "all":
    PAIRS = ALL_PAIRS
else:
    PAIRS = [p for p in ALL_PAIRS if f"{p[0]}/{p[1]}" == PAIR_FILTER]


INTERVAL = "1h"
LOOKBACK_HOURS = 24 * 120     # 120 days of raw data
FIT_WINDOW_HOURS = 24 * 30    # 30-day fit window
TEST_WINDOW_HOURS = 24 * 90   # test last 90 days
HOLD_HORIZON_HOURS = 24 * 3   # 72h hold max

# Entry rules (same as live)
Z_ENTRY_MIN = 1.5
EDGE_MIN = 0.05

# Exit rules (same as live)
TARGET_FRAC = 0.5
STOP_MULT = 3.0
BETA_SHIFT_MAX = 0.20


# ============================================================
# FAST FIT WITH MAP
# ============================================================
def fit_coint_map(log_a: np.ndarray, log_b: np.ndarray) -> dict:
    """Fit cointegration model with MAP. Fast."""
    with pm.Model() as model:
        alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
        beta = pm.Normal("beta", mu=1.0, sigma=1.0)
        sigma = pm.HalfCauchy("sigma", beta=1.0)
        nu = pm.Exponential("nu", lam=1/10.0)

        mu = alpha + beta * log_b
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)

        try:
            # Bound optimization to avoid hangs on ill-conditioned posteriors.
            # maxeval caps the number of loss evaluations.
            map_est = pm.find_MAP(
                progressbar=False,
                maxeval=500,
                method="L-BFGS-B",
            )
        except Exception as e:
            return {"error": str(e)}


    return {
        "alpha": float(map_est["alpha"]),
        "beta": float(map_est["beta"]),
        "sigma": float(map_est["sigma"]),
        "nu": float(map_est["nu"]),
    }


def fit_ou_map(spread: np.ndarray) -> dict:
    """Fit OU mean-reversion model with MAP. Fast."""
    y = np.asarray(spread, dtype=float)
    y_centered = y - y.mean()
    y_lag = y_centered[:-1]
    y_next = y_centered[1:]

    with pm.Model() as model:
        kappa = pm.Beta("kappa", alpha=2.0, beta=2.0)
        sigma = pm.HalfCauchy("sigma", beta=1.0)
        nu = pm.Exponential("nu", lam=1/10.0)

        mu = (1 - kappa) * y_lag
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=y_next)

        try:
            # Bound optimization to avoid hangs on ill-conditioned posteriors.
            # maxeval caps the number of loss evaluations.
            map_est = pm.find_MAP(
                progressbar=False,
                maxeval=500,
                method="L-BFGS-B",
            )
        except Exception as e:
            return {"error": str(e)}


    kappa_val = float(map_est["kappa"])
    if 1e-6 < kappa_val < 1:
        half_life = float(np.log(0.5) / np.log(1 - kappa_val))
    else:
        half_life = float("inf")

    return {
        "kappa": kappa_val,
        "sigma": float(map_est["sigma"]),
        "nu": float(map_est["nu"]),
        "half_life_hours": half_life,
    }


# ============================================================
# SIGNAL LOGIC (mirrors live bot)
# ============================================================
def compute_signal(log_a, log_b, coint, ou):
    """Given fits, compute z-score, p_revert, edge."""
    spread = log_a - coint["alpha"] - coint["beta"] * log_b
    spread_centered = spread - spread.mean()
    current = float(spread_centered[-1])
    sd = float(spread_centered.std())

    z = current / (sd + 1e-9)
    if abs(z) < Z_ENTRY_MIN:
        return None  # no entry

    # Simplified P(revert): probability spread reverts to half its
    # deviation within HOLD_HORIZON based on OU kappa
    kappa = ou["kappa"]
    if kappa <= 0 or kappa >= 1:
        return None

    # Analytical OU reversion probability (first-passage approximation)
    hl = ou["half_life_hours"]
    horizon = HOLD_HORIZON_HOURS
    # Prob of reversion ~ 1 - exp(-horizon / (2 * hl))
    p_revert = 1.0 - np.exp(-horizon / (2.0 * hl + 1e-9))
    edge = p_revert - 0.5

    if edge < EDGE_MIN:
        return None

    direction = "SHORT_A_LONG_B" if current > 0 else "LONG_A_SHORT_B"

    return {
        "spread": float(current),
        "z": z,
        "p_revert": float(p_revert),
        "edge": float(edge),
        "direction": direction,
        "half_life_hours": hl,
    }


# ============================================================
# SIMULATE A TRADE
# ============================================================
def simulate_trade(log_a_path, log_b_path, coint, signal):
    """
    Given future log_a and log_b paths, simulate the trade with
    the same exit rules as the live bot.

    Returns dict with pnl_pct, exit_reason, elapsed_hours.
    """
    entry_spread = signal["spread"]
    direction = signal["direction"]
    is_long_a = direction == "LONG_A_SHORT_B"

    target = TARGET_FRAC * entry_spread
    stop = STOP_MULT * entry_spread

    # Simulate forward, checking exit at each hour
    for t in range(len(log_a_path)):
        # Recompute current spread using entry-time alpha/beta
        spread_now = (
            log_a_path[t]
            - coint["alpha"]
            - coint["beta"] * log_b_path[t]
        )
        # Approximate centered spread by subtracting entry mean
        # (we don't have the exact mean here, use entry_spread baseline)
        delta = spread_now - (
            coint["alpha"] + coint["beta"] * log_b_path[0]
        )
        # Instead, use the actual spread move:
        # Since entries and exits are both on same coordinate system,
        # use relative spread change
        # Actually we need to compute the entry-aligned centered spread
        # For simplicity, approximate: current centered spread == entry_spread + (spread_now - spread_at_entry)
        # where spread_at_entry = log_a_path[0] - alpha - beta*log_b_path[0]
        spread_at_entry = (
            log_a_path[0] - coint["alpha"] - coint["beta"] * log_b_path[0]
        )
        current_centered = entry_spread + (spread_now - spread_at_entry)

        # Check exits
        if is_long_a:
            if current_centered >= target:
                return {
                    "pnl_pct": _pnl_pct(current_centered - entry_spread, is_long_a, coint),
                    "exit_reason": "target",
                    "elapsed_hours": t,
                }
            if current_centered <= stop:
                return {
                    "pnl_pct": _pnl_pct(current_centered - entry_spread, is_long_a, coint),
                    "exit_reason": "stop",
                    "elapsed_hours": t,
                }
        else:
            if current_centered <= target:
                return {
                    "pnl_pct": _pnl_pct(current_centered - entry_spread, is_long_a, coint),
                    "exit_reason": "target",
                    "elapsed_hours": t,
                }
            if current_centered >= stop:
                return {
                    "pnl_pct": _pnl_pct(current_centered - entry_spread, is_long_a, coint),
                    "exit_reason": "stop",
                    "elapsed_hours": t,
                }

    # Time stop reached
    final_centered = current_centered
    return {
        "pnl_pct": _pnl_pct(final_centered - entry_spread, is_long_a, coint),
        "exit_reason": "time_stop",
        "elapsed_hours": len(log_a_path),
    }


def _pnl_pct(spread_delta, is_long_a, coint):
    """
    Approximate PnL in % of notional from spread change.

    For a long-A-short-B position sized to be beta-neutral, a positive
    spread move in the trade's favor yields ~spread_delta returns
    (in log space, which is close enough for small moves).
    """
    # For LONG_A_SHORT_B: we profit when spread rises
    # For SHORT_A_LONG_B: we profit when spread falls
    sign = 1.0 if is_long_a else -1.0
    return float(sign * spread_delta * 100)  # convert to %


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 70)
    print("WALK-FORWARD BACKTEST")
    print("=" * 70)
    print(f"Pairs:       {len(PAIRS)}")
    print(f"Fit window:  {FIT_WINDOW_HOURS // 24} days")
    print(f"Test window: {TEST_WINDOW_HOURS // 24} days")
    print(f"Hold horizon: {HOLD_HORIZON_HOURS} hours")
    print()

    all_trades = []

    for coin_a, coin_b in PAIRS:
        pair = f"{coin_a}/{coin_b}"
        print(f"--- {pair} ---")

        try:
            ts, ca, cb = aligned_close_prices(
                coin_a, coin_b,
                interval=INTERVAL,
                lookback_hours=LOOKBACK_HOURS,
            )
        except Exception as e:
            print(f"  fetch failed: {e}")
            continue

        if len(ts) < FIT_WINDOW_HOURS + TEST_WINDOW_HOURS:
            print(f"  insufficient data: {len(ts)} bars")
            continue

        log_a = np.log(np.array(ca))
        log_b = np.log(np.array(cb))

        # Walk-forward loop
        test_end = len(ts)
        test_start = test_end - TEST_WINDOW_HOURS

        pair_trades = []

        # Refit once per day, not once per hour. 24x speedup.
        for T in range(test_start, test_end - HOLD_HORIZON_HOURS, 48):
            # Progress print every 10 iterations
            iters_done = T - test_start
            if iters_done > 0 and iters_done % (48 * 5) == 0:
                days_done = iters_done // 48
                total_days = (test_end - HOLD_HORIZON_HOURS - test_start) // 48
                print(f"  ... day {days_done}/{total_days} done, "
                      f"{len(pair_trades)} trades so far", flush=True)


            # Fit coint on [fit_start, T]
            fit_start = T - FIT_WINDOW_HOURS
            # Force garbage collection every N iterations to prevent
            # PyMC tensor memory leak across model builds
            if (T - test_start) % (48 * 5) == 0 and T > test_start:
                gc.collect()

            coint = fit_coint_map(log_a[fit_start:T], log_b[fit_start:T])
            if "error" in coint:
                continue

            # Compute spread on fit window
            spread_fit = (
                log_a[fit_start:T]
                - coint["alpha"]
                - coint["beta"] * log_b[fit_start:T]
            )

            # Fit OU on spread
            ou = fit_ou_map(spread_fit)
            if "error" in ou:
                continue

            # Compute signal on latest bar
            signal = compute_signal(
                log_a[fit_start:T],
                log_b[fit_start:T],
                coint, ou,
            )
            if signal is None:
                continue

            # Simulate forward from T
            future_a = log_a[T:T + HOLD_HORIZON_HOURS + 1]
            future_b = log_b[T:T + HOLD_HORIZON_HOURS + 1]
            if len(future_a) < HOLD_HORIZON_HOURS + 1:
                continue

            outcome = simulate_trade(future_a, future_b, coint, signal)
            outcome["pair"] = pair
            outcome["entry_idx"] = T
            outcome["entry_z"] = signal["z"]
            outcome["entry_edge"] = signal["edge"]
            outcome["entry_p_revert"] = signal["p_revert"]
            outcome["entry_half_life"] = signal["half_life_hours"]
            outcome["direction"] = signal["direction"]

            pair_trades.append(outcome)

        print(f"  Trades: {len(pair_trades)}")
        all_trades.extend(pair_trades)

    # ============================================================
    # AGGREGATE METRICS
    # ============================================================
    if not all_trades:
        print("\nNo trades generated. Nothing to analyze.")
        return

    df = pd.DataFrame(all_trades)

    # Filter: exit after entry to be safe
    df = df[df["elapsed_hours"] > 0]

    print()
    print("=" * 70)
    print("OUT-OF-SAMPLE RESULTS")
    print("=" * 70)
    print(f"Total trades:        {len(df)}")
    print(f"Win rate:            {(df.pnl_pct > 0).mean():.1%}")
    print(f"Mean PnL:            {df.pnl_pct.mean():+.4f}%")
    print(f"Std PnL:             {df.pnl_pct.std():.4f}%")
    print(f"Mean duration:       {df.elapsed_hours.mean():.1f}h")
    print()

    if df.pnl_pct.std() > 0:
        sharpe_trade = df.pnl_pct.mean() / df.pnl_pct.std()
        mean_dur = df.elapsed_hours.mean()
        trades_per_year = (365 * 24) / mean_dur if mean_dur > 0 else 0
        sharpe_annual = sharpe_trade * np.sqrt(trades_per_year)
        print(f"Sharpe per trade:    {sharpe_trade:.3f}")
        print(f"Trades per year:     {trades_per_year:.0f}")
        print(f"Annualized Sharpe:   {sharpe_annual:.2f}")
    print()

    # Equity curve
    df = df.sort_values("entry_idx").reset_index(drop=True)
    df["cum_pnl_pct"] = df.pnl_pct.cumsum()
    peak = df.cum_pnl_pct.cummax()
    dd = df.cum_pnl_pct - peak
    print(f"Final cum PnL:       {df.cum_pnl_pct.iloc[-1]:+.2f}%")
    print(f"Max drawdown:        {dd.min():.2f}%")
    print()

    print("--- Exit reasons ---")
    print(df.exit_reason.value_counts())
    print()

    print("--- Per-pair ---")
    print(df.groupby("pair").pnl_pct.agg(["count", "sum", "mean"]).round(4))
    print()

    # Save results
    out_path = Path(__file__).resolve().parent.parent / "reports" / "walk_forward.csv"
    out_path.parent.mkdir(exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"Saved: {out_path}")

    # Verdict
    print()
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    if "sharpe_annual" in dir() or 'sharpe_annual' in locals():
        if sharpe_annual >= 1.0:
            print(f"✅ Sharpe {sharpe_annual:.2f} ≥ 1.0 → edge is real. Scale.")
        elif sharpe_annual >= 0.5:
            print(f"⚠️  Sharpe {sharpe_annual:.2f} — modest edge. Scale carefully.")
        else:
            print(f"❌ Sharpe {sharpe_annual:.2f} < 0.5 → diagnose before scaling.")


if __name__ == "__main__":
    main()
