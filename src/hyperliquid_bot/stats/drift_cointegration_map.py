"""
MAP version of the linear-drift cointegration model.
Same math, but uses find_MAP instead of full MCMC.
~100ms per fit instead of ~60s.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pymc as pm


@dataclass
class DriftCointFitMAP:
    alpha: float
    beta0: float
    beta1: float
    sigma: float
    nu: float
    spread: np.ndarray


def fit_cointegration_drift_map(
    log_a: np.ndarray,
    log_b: np.ndarray,
) -> DriftCointFitMAP | None:
    n = len(log_a)
    if n < 50:
        return None

    t = np.linspace(-1.0, 1.0, n)

    with pm.Model() as model:
        alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
        beta0 = pm.Normal("beta0", mu=1.0, sigma=1.0)
        beta1 = pm.Normal("beta1", mu=0.0, sigma=0.2)
        sigma = pm.HalfNormal("sigma", sigma=0.5)
        nu = pm.Exponential("nu", lam=1 / 10.0)

        beta_path = beta0 + beta1 * t
        mu = alpha + beta_path * log_b
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)

        try:
            map_est = pm.find_MAP(
                progressbar=False, maxeval=500, method="L-BFGS-B",
            )
        except Exception:
            return None

    alpha_m = float(map_est["alpha"])
    beta0_m = float(map_est["beta0"])
    beta1_m = float(map_est["beta1"])
    beta_path_m = beta0_m + beta1_m * t
    spread = log_a - alpha_m - beta_path_m * log_b
    spread = spread - spread.mean()

    return DriftCointFitMAP(
        alpha=alpha_m,
        beta0=beta0_m,
        beta1=beta1_m,
        sigma=float(map_est["sigma"]),
        nu=float(map_est["nu"]),
        spread=spread,
    )


if __name__ == "__main__":
    import sys, time
    sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
    from src.hyperliquid_bot.data.ohlcv import aligned_close_prices

    ts, ca, cb = aligned_close_prices("ETH", "BTC", interval="1h", lookback_hours=24*30)
    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    print(f"Data: {len(log_a)} bars")

    t0 = time.time()
    fit = fit_cointegration_drift_map(log_a, log_b)
    elapsed = time.time() - t0

    if fit is None:
        print("fit failed")
    else:
        print(f"Elapsed:   {elapsed:.3f}s")
        print(f"alpha:     {fit.alpha:+.4f}")
        print(f"beta0:     {fit.beta0:+.4f}")
        print(f"beta1:     {fit.beta1:+.4f}  (drift per unit time)")
        print(f"sigma:     {fit.sigma:.4f}")
        print(f"nu:        {fit.nu:.2f}")
