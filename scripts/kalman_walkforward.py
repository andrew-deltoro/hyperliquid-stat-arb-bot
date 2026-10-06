"""
Walk-forward PnL comparison: Kalman beta vs static OLS beta.

Same entry/exit rules applied to both. Same test window.
The only difference is how beta is estimated:
  - static: refit OLS every 4h on trailing 720h window, freeze between refits
  - Kalman:  recursive update every bar

Trade simulation:
  Entry:  |z| > entry_z
  Exit:   z crosses target_z (revert) OR |z| > divergence_z (against) OR hold > max_hold

PnL is in spread units (log-return per unit position).
"""
from __future__ import annotations
import sys, time
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")

import numpy as np
import statsmodels.api as sm

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
from src.hyperliquid_bot.pairs.kalman import KalmanBeta


def fit_ols(log_a, log_b):
    X = sm.add_constant(log_b, has_constant="add")
    try:
        m = sm.OLS(log_a, X).fit()
        return {"alpha": float(m.params[0]), "beta": float(m.params[1])}
    except Exception:
        return None


def simulate_trades(z_series, spread_series, entry_z=1.0, target_z=0.2,
                    divergence_z=3.0, max_hold=168):
    trades = []
    pos = None
    for i in range(len(z_series)):
        z = z_series[i]
        if np.isnan(z):
            continue
        if pos is None:
            if abs(z) > entry_z:
                pos = {
                    "entry_i": i,
                    "entry_z": z,
                    "entry_spread": spread_series[i],
                    "side": -1 if z > 0 else 1,   # z>0 -> short spread
                }
        else:
            hold = i - pos["entry_i"]
            hit_target = (
                (pos["side"] == -1 and z <= target_z) or
                (pos["side"] == 1 and z >= -target_z)
            )
            hit_divergence = (
                (pos["side"] == -1 and z > divergence_z) or
                (pos["side"] == 1 and z < -divergence_z)
            )
            hit_time = hold >= max_hold
            if hit_target or hit_divergence or hit_time:
                spread_exit = spread_series[i]
                pnl = pos["side"] * (spread_exit - pos["entry_spread"])
                trades.append({
                    "entry_i": pos["entry_i"],
                    "exit_i": i,
                    "hold": hold,
                    "entry_z": pos["entry_z"],
                    "exit_z": z,
                    "pnl": pnl,
                    "reason": "target" if hit_target else
                              ("divergence" if hit_divergence else "time"),
                })
                pos = None
    return trades


def summarize_trades(trades, label):
    if not trades:
        print(f"  {label}: 0 trades")
        return
    pnls = np.array([t["pnl"] for t in trades])
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    print(f"  {label}:")
    print(f"    n={len(pnls)}  total={pnls.sum():+.5f}  mean={pnls.mean():+.5f}  "
          f"std={pnls.std():.5f}")
    if pnls.std() > 0:
        sharpe = pnls.mean() / pnls.std()
        print(f"    sharpe={sharpe:+.3f}  win%={100*(pnls>0).mean():.1f}%  "
              f"avg_hold={np.mean([t['hold'] for t in trades]):.1f}")
    print(f"    reasons: {reasons}")
    # Side breakdown (connects to asymmetric-z finding)
    wide = [t for t in trades if t["entry_z"] > 0]
    tight = [t for t in trades if t["entry_z"] < 0]
    for side_name, side_trades in [("wide(z>0)", wide), ("tight(z<0)", tight)]:
        if side_trades:
            sp = np.array([t["pnl"] for t in side_trades])
            print(f"    {side_name}: n={len(sp)}  mean={sp.mean():+.5f}  "
                  f"win%={100*(sp>0).mean():.1f}%")


def rolling_z(spread, window):
    z = np.full(len(spread), np.nan)
    for i in range(window, len(spread)):
        w = spread[i-window:i]
        m = w.mean()
        s = w.std() + 1e-9
        z[i] = (spread[i] - m) / s
    return z


def main(coin_a="SNX", coin_b="DYDX"):
    print(f"=== Kalman walk-forward: {coin_a}/{coin_b} ===")

    ts, ca, cb = aligned_close_prices(
        coin_a, coin_b, interval="1h", lookback_hours=24*180,
    )
    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    n = len(log_a)
    print(f"Data: {n} bars (~{n//24} days)")

    TRAIN = 60 * 24
    LOOKBACK = 720
    REFIT_EVERY = 4
    print(f"Train: {TRAIN} bars, Test: {n - TRAIN} bars, Lookback: {LOOKBACK} bars\n")

    # --- Static: refit every 4h, freeze between ---
    print("Fitting static OLS...", flush=True)
    t0 = time.time()
    static_beta = np.full(n, np.nan)
    static_alpha = np.full(n, np.nan)
    for i in range(TRAIN, n, REFIT_EVERY):
        if i < LOOKBACK:
            continue
        s = fit_ols(log_a[i-LOOKBACK:i], log_b[i-LOOKBACK:i])
        if s is None:
            continue
        end = min(i + REFIT_EVERY, n)
        static_beta[i:end] = s["beta"]
        static_alpha[i:end] = s["alpha"]
    print(f"  done in {time.time()-t0:.1f}s")

    # --- Kalman: update every bar ---
    print("Running Kalman filter...", flush=True)
    t0 = time.time()
    init = fit_ols(log_a[:TRAIN], log_b[:TRAIN])
    kf = KalmanBeta(beta_init=init["beta"], alpha_init=init["alpha"],
                    q=1e-5, r=1e-4)
    for i in range(TRAIN):
        kf.update(float(log_a[i]), float(log_b[i]))
    kalman_beta = np.full(n, np.nan)
    kalman_alpha = np.full(n, np.nan)
    for i in range(TRAIN, n):
        s = kf.update(float(log_a[i]), float(log_b[i]))
        kalman_beta[i] = s.beta
        kalman_alpha[i] = s.alpha
    print(f"  done in {time.time()-t0:.1f}s\n")

    # --- Spread series over test window ---
    idx = np.arange(TRAIN, n)
    static_spread = log_a[idx] - static_beta[idx] * log_b[idx] - static_alpha[idx]
    kalman_spread = log_a[idx] - kalman_beta[idx] * log_b[idx] - kalman_alpha[idx]

    # --- Rolling z-scores ---
    static_z = rolling_z(static_spread, LOOKBACK)
    kalman_z = rolling_z(kalman_spread, LOOKBACK)

    print("z-series stats:")
    for label, z in [("Static", static_z), ("Kalman", kalman_z)]:
        zz = z[~np.isnan(z)]
        print(f"  {label}: n={len(zz)}  mean={zz.mean():+.3f}  std={zz.std():.3f}  "
              f"min={zz.min():+.2f}  max={zz.max():+.2f}  "
              f"|z|>1: {(np.abs(zz)>1).sum()}")
    print()

    # --- Simulate ---
    print("=== Trade simulation (entry_z=1.0, target_z=0.2, "
          "divergence_z=3.0, max_hold=168h) ===")
    static_trades = simulate_trades(static_z, static_spread)
    kalman_trades = simulate_trades(kalman_z, kalman_spread)

    summarize_trades(static_trades, "Static OLS")
    print()
    summarize_trades(kalman_trades, "Kalman")
    print()

    # --- Verdict ---
    print("=== Verdict ===")
    if not static_trades or not kalman_trades:
        print("  insufficient trades")
        return
    sp = np.array([t["pnl"] for t in static_trades])
    kp = np.array([t["pnl"] for t in kalman_trades])
    if sp.std() > 0 and kp.std() > 0:
        s_sharpe = sp.mean() / sp.std()
        k_sharpe = kp.mean() / kp.std()
        print(f"  Static sharpe: {s_sharpe:+.3f}  n={len(sp)}  total={sp.sum():+.5f}")
        print(f"  Kalman sharpe: {k_sharpe:+.3f}  n={len(kp)}  total={kp.sum():+.5f}")
        if k_sharpe > 1.3 * s_sharpe and len(kp) >= 20:
            print("  → Kalman substantially better")
        elif k_sharpe > s_sharpe and len(kp) >= 20:
            print("  → Kalman marginally better")
        elif len(kp) < 20:
            print("  → Kalman produces too few trades to be useful")
        else:
            print("  → Static is equal or better")


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else "SNX"
    b = sys.argv[2] if len(sys.argv) > 2 else "DYDX"
    main(a, b)
