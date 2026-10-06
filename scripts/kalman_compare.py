"""
Compare static beta (OLS or PyMC MAP) against Kalman beta on the same pair.

Default: OLS static fit (fast, ~0.1ms per refit).
Optional: --method map for PyMC MAP (slow, matches live bot exactly).

Reports:
  - Beta trajectories (correlation, stddev)
  - Spread stationarity (ADF p-value)
  - Spread volatility
"""
from __future__ import annotations
import sys, time
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")

import numpy as np
from statsmodels.tsa.stattools import adfuller
import statsmodels.api as sm

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
from src.hyperliquid_bot.pairs.kalman import KalmanBeta


def fit_ols(log_a, log_b):
    """Fast OLS: log_a = alpha + beta*log_b + noise."""
    X = sm.add_constant(log_b, has_constant="add")
    try:
        m = sm.OLS(log_a, X).fit()
        return {"alpha": float(m.params[0]), "beta": float(m.params[1])}
    except Exception:
        return None


def fit_coint_map(log_a, log_b):
    """PyMC MAP fit (slow, matches live bot exactly)."""
    import pymc as pm
    lb_mean = float(np.mean(log_b))
    lb_c = log_b - lb_mean
    try:
        with pm.Model() as model:
            a_c = pm.Normal("alpha_c", mu=0.0, sigma=5.0)
            beta = pm.Normal("beta", mu=1.0, sigma=1.0)
            sigma = pm.HalfNormal("sigma", sigma=0.5)
            mu = a_c + beta * lb_c
            pm.Normal("obs", mu=mu, sigma=sigma, observed=log_a)
            me = pm.find_MAP(progressbar=False, method="L-BFGS-B")
        alpha = float(me["alpha_c"]) - float(me["beta"]) * lb_mean
        return {"alpha": alpha, "beta": float(me["beta"])}
    except Exception:
        return None


def main(coin_a="SNX", coin_b="DYDX", static_method="ols"):
    print(f"=== Kalman comparison: {coin_a}/{coin_b} ===")
    print(f"Static method: {static_method}")
    print(flush=True)

    t0 = time.time()
    ts, ca, cb = aligned_close_prices(coin_a, coin_b, interval="1h", lookback_hours=24*180)
    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    n = len(log_a)
    print(f"Data points: {n}  (~{n//24} days)   fetch={time.time()-t0:.1f}s")
    print(flush=True)

    TRAIN = 60 * 24
    if n < TRAIN + 30 * 24:
        print(f"Not enough data. Need {TRAIN + 30*24} bars.")
        return

    fit = fit_coint_map if static_method == "map" else fit_ols

    t1 = time.time()
    initial = fit(log_a[:TRAIN], log_b[:TRAIN])
    if initial is None:
        print("Initial fit failed")
        return
    print(f"Initial β: {initial['beta']:.4f}  α: {initial['alpha']:.4f}   fit={time.time()-t1:.2f}s")
    print(flush=True)

    # Kalman warmup
    kf = KalmanBeta(
        beta_init=initial["beta"],
        alpha_init=initial["alpha"],
        q=1e-5,
        r=1e-4,
    )
    kf.smooth(log_a[:TRAIN], log_b[:TRAIN])
    print("Kalman warmup complete")
    print(flush=True)

    test_start = TRAIN
    test_end = n
    FIT_WINDOW = 30 * 24
    REFIT_EVERY = 4

    static_betas, kalman_betas, kalman_alphas, timestamps_4h = [], [], [], []
    n_test = test_end - test_start
    print(f"Running test window: {n_test} bars, refit every {REFIT_EVERY}h")
    print(flush=True)

    t_loop = time.time()
    next_log = 200
    fit_failures = 0

    for i in range(test_start, test_end):
        state = kf.update(float(log_a[i]), float(log_b[i]))
        kalman_betas.append(state.beta)
        kalman_alphas.append(state.alpha)

        if (i - test_start) % REFIT_EVERY == 0:
            start = i - FIT_WINDOW
            if start >= 0:
                s = fit(log_a[start:i], log_b[start:i])
                if s is not None:
                    static_betas.append(s["beta"])
                    timestamps_4h.append(i)
                else:
                    fit_failures += 1

        done = i - test_start + 1
        if done >= next_log:
            elapsed = time.time() - t_loop
            rate = done / elapsed
            eta = (n_test - done) / rate
            print(f"  [{done}/{n_test}]  {rate:.0f} bars/s  ETA {eta:.0f}s  "
                  f"fits={len(static_betas)}  fail={fit_failures}")
            next_log += 200

    print(f"\nLoop time: {time.time()-t_loop:.2f}s")
    print(flush=True)

    kalman_betas = np.array(kalman_betas)
    kalman_alphas = np.array(kalman_alphas)
    static_betas = np.array(static_betas)

    if len(static_betas) < 3:
        print("Not enough static fits succeeded")
        return

    print(f"\nTest window: {n_test} bars  "
          f"({len(static_betas)} static fits, {len(kalman_betas)} kalman updates)")
    print()

    print("=== β trajectories ===")
    print(f"  Static:  mean={static_betas.mean():.4f}  std={static_betas.std():.4f}  "
          f"range=[{static_betas.min():.3f}, {static_betas.max():.3f}]")
    print(f"  Kalman:  mean={kalman_betas.mean():.4f}  std={kalman_betas.std():.4f}  "
          f"range=[{kalman_betas.min():.3f}, {kalman_betas.max():.3f}]")
    kalman_at_4h = kalman_betas[[t - test_start for t in timestamps_4h]]
    corr = float(np.corrcoef(static_betas, kalman_at_4h)[0, 1])
    print(f"  Correlation on 4h grid: {corr:.4f}")
    print()

    static_spread = log_a[test_start:] - np.median(static_betas) * log_b[test_start:]
    kalman_spread = log_a[test_start:] - kalman_betas * log_b[test_start:] - kalman_alphas

    print("=== Spread properties ===")
    for label, spread in [("Static", static_spread), ("Kalman", kalman_spread)]:
        try:
            _, adf_p, *_ = adfuller(spread, maxlag=24, autolag=None)
            print(f"  {label:>7}: std={spread.std():.6f}  ADF p={adf_p:.4f}")
        except Exception as e:
            print(f"  {label:>7}: ADF failed ({e})")
    print()

    # Verdict: ADF stationarity of Kalman vs Static spread is the primary signal.
    # Correlation is secondary because static β can itself be noisy.
    static_adf = adfuller(static_spread, maxlag=24, autolag=None)[1]
    kalman_adf = adfuller(kalman_spread, maxlag=24, autolag=None)[1]
    static_std = float(static_spread.std())
    kalman_std = float(kalman_spread.std())

    print("=== Verdict ===")
    smoother = kalman_betas.std() < static_betas.std()
    tighter = kalman_std < static_std
    more_stationary = kalman_adf < static_adf

    if more_stationary and tighter and smoother:
        print("  ✓ Kalman spread is MORE stationary, TIGHTER, and β is SMOOTHER")
        print("    → strong candidate for integration")
    elif more_stationary and tighter:
        print("  ✓ Kalman spread better on stationarity + tightness")
        print("    → likely worth integration; check correlation doesn't mean divergence")
    elif not more_stationary:
        print("  ✗ Kalman spread LESS stationary → reject or tune Q,R")
    else:
        print(f"  ? Mixed: smoother={smoother} tighter={tighter} stat={more_stationary}")
        print("    → inspect pair-specific behavior")


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else "SNX"
    b = sys.argv[2] if len(sys.argv) > 2 else "DYDX"
    method = sys.argv[3] if len(sys.argv) > 3 else "ols"
    main(a, b, method)
