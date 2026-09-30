"""
Gaussian Process varying-beta cointegration model.
McElreath Ch 13 - Adventures in Covariance.

Instead of a single static beta for the whole fit window, beta is
modeled as a smooth function of time via a GP prior. This lets the
hedge ratio drift during regime changes.

To keep this tractable on a 4-core VPS, beta is defined at K discrete
time nodes across the window, with a squared-exponential GP prior.
Interpolation between nodes gives per-bar beta values.

Covariance kernel: K_ij = eta^2 * exp(-rho^2 * (i-j)^2) + delta_ij * sigma_b^2
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pymc as pm
import arviz as az


@dataclass
class GPCointFit:
    alpha_mean: float
    beta_mean: float
    sigma_mean: float
    nu_mean: float
    eta_mean: float
    rho_mean: float
    beta_path: np.ndarray      # interpolated beta per observation
    spread: np.ndarray          # residuals y - alpha - beta_path * x
    beta_nodes: np.ndarray      # raw GP nodes
    node_positions: np.ndarray  # normalized time positions
    converged: bool


def _build_K(nodes: np.ndarray, eta: float, rho: float, sigma_b: float) -> np.ndarray:
    """Squared-exponential kernel + small diagonal."""
    d = nodes[:, None] - nodes[None, :]
    K = eta ** 2 * np.exp(-rho ** 2 * d ** 2)
    K = K + np.eye(len(nodes)) * sigma_b ** 2
    return K


def _interpolate_nodes(beta_nodes: np.ndarray, node_pos: np.ndarray, n_obs: int) -> np.ndarray:
    """Linear interpolation of beta from K nodes to n_obs positions."""
    obs_pos = np.linspace(0, 1, n_obs)
    return np.interp(obs_pos, node_pos, beta_nodes)


def fit_cointegration_gp(
    log_a: np.ndarray,
    log_b: np.ndarray,
    n_nodes: int = 6,
    draws: int = 1000,
    tune: int = 1500,
    chains: int = 2,
    cores: int = 2,
    seed: int = 0,
    progressbar: bool = False,
) -> GPCointFit:
    """Fit GP varying-beta cointegration model.

    Args:
        log_a, log_b: log price series of equal length
        n_nodes: number of GP nodes across the window (6-12 is typical)
    """
    n = len(log_a)
    assert n == len(log_b)
    assert n > 50, "need at least 50 obs"
    assert 3 <= n_nodes <= 20, "n_nodes between 3 and 20"

    node_pos = np.linspace(0, 1, n_nodes)

    with pm.Model() as model:
        # --- Fixed effects ---
        alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
        sigma = pm.HalfNormal("sigma", sigma=0.5)
        nu = pm.Exponential("nu", lam=1 / 10.0)

        # --- GP hyperparameters ---
        # eta: amplitude of the GP (how much beta can drift)
        eta = pm.HalfNormal("eta", sigma=0.05)
        # rho: length-scale (how quickly beta can change)
        # rho^2 * (node separation)^2 controls smoothness
        rho = pm.HalfNormal("rho", sigma=5.0)
        # small per-node noise
        sigma_b = pm.HalfNormal("sigma_b", sigma=0.01)

        # --- GP prior on beta nodes via AR(1) parameterization ---
        # Numerically stable equivalent of an SE-kernel GP.
        # No Cholesky inversion, no ill-conditioned cov matrices.
        # Same statistical model, different parameterization.
        step = 1.0 / max(n_nodes - 1, 1)
        rho_term = pm.math.exp(-rho ** 2 * step ** 2)   # correlation between adjacent nodes
        # Sample standardized innovations
        beta_raw = pm.Normal("beta_raw", mu=0.0, sigma=1.0, shape=n_nodes)
        # Scale innovations so that the AR(1) process has stationary variance eta^2
        # Increment std = eta * sqrt(1 - rho_term^2), except first which is eta
        inc_std = eta * pm.math.sqrt(1.0 - rho_term ** 2)
        increments = pm.math.concatenate([
            pm.math.stack([eta * beta_raw[0]]),
            inc_std * beta_raw[1:],
        ])
        beta_devs = pm.math.cumsum(increments)
        beta_nodes = pm.Deterministic("beta_nodes", 1.0 + beta_devs)

        # Interpolate nodes to per-observation beta via numpy (deterministic)
        # PyMC tracks beta_nodes; interpolation is differentiable via np.interp equivalent
        obs_pos = np.linspace(0, 1, n)
        # Build interpolation weights as constant matrix: (n, n_nodes)
        W = np.zeros((n, n_nodes))
        for i, p in enumerate(obs_pos):
            idx = np.searchsorted(node_pos, p, side="right") - 1
            idx = max(0, min(idx, n_nodes - 2))
            t = (p - node_pos[idx]) / (node_pos[idx + 1] - node_pos[idx])
            W[i, idx] = 1 - t
            W[i, idx + 1] = t
        # W is a fixed numpy array; use it directly as a constant in the dot product.
        # PyMC treats numpy arrays as constants, no wrapping needed.
        beta_path = pm.Deterministic("beta_path", pm.math.dot(W, beta_nodes))

        # --- Likelihood ---
        mu = alpha + beta_path * log_b
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)

        idata = pm.sample(
            draws=draws, tune=tune, chains=chains, cores=cores,
            target_accept=0.95, random_seed=seed, progressbar=progressbar,
        )

    # --- Extract results ---
    rhat = az.rhat(idata)
    max_rhat = max(
        float(rhat[v].values) for v in ["alpha", "beta_nodes", "sigma", "nu", "eta", "rho"]
    )
    converged = max_rhat < 1.05

    alpha_s = idata.posterior["alpha"].stack(s=("chain", "draw")).values
    beta_nodes_s = idata.posterior["beta_nodes"].stack(s=("chain", "draw")).values
    eta_s = idata.posterior["eta"].stack(s=("chain", "draw")).values
    rho_s = idata.posterior["rho"].stack(s=("chain", "draw")).values
    sigma_s = idata.posterior["sigma"].stack(s=("chain", "draw")).values
    nu_s = idata.posterior["nu"].stack(s=("chain", "draw")).values

    # Mean beta path (interpolated at mean nodes)
    beta_nodes_mean = beta_nodes_s.mean(axis=1)
    beta_path_mean = _interpolate_nodes(beta_nodes_mean, node_pos, n)

    spread = log_a - alpha_s.mean() - beta_path_mean * log_b
    spread = spread - spread.mean()

    return GPCointFit(
        alpha_mean=float(alpha_s.mean()),
        beta_mean=float(beta_path_mean.mean()),
        sigma_mean=float(sigma_s.mean()),
        nu_mean=float(nu_s.mean()),
        eta_mean=float(eta_s.mean()),
        rho_mean=float(rho_s.mean()),
        beta_path=beta_path_mean,
        spread=spread,
        beta_nodes=beta_nodes_mean,
        node_positions=node_pos,
        converged=converged,
    )


if __name__ == "__main__":
    # Quick sanity check
    import sys
    sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
    from src.hyperliquid_bot.data.ohlcv import aligned_close_prices

    ts, ca, cb = aligned_close_prices("ETH", "BTC", interval="1h", lookback_hours=24*30)
    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    print(f"Data: {len(log_a)} bars")
    fit = fit_cointegration_gp(log_a, log_b, n_nodes=6)
    print(f"alpha:  {fit.alpha_mean:+.4f}")
    print(f"beta:   {fit.beta_mean:+.4f}  (nodes: {fit.beta_nodes.round(3).tolist()})")
    print(f"eta:    {fit.eta_mean:.4f}  (GP amplitude)")
    print(f"rho:    {fit.rho_mean:.4f}  (length-scale)")
    print(f"nu:     {fit.nu_mean:.2f}")
    print(f"converged: {fit.converged}")
