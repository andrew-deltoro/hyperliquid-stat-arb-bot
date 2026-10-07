"""Joint error-correction model (audit H6)."""
from __future__ import annotations

import numpy as np
import pytest

from src.hyperliquid_bot.phase3.leverage import ExitPolicy, simulate_trade_returns
from src.hyperliquid_bot.stats.cointegration import fit_cointegration
from src.hyperliquid_bot.stats.error_correction import fit_error_correction
from src.hyperliquid_bot.stats.mean_reversion import fit_ou


def _ou_spread(kappa, n=400, seed=0, sigma=0.004):
    rng = np.random.default_rng(seed)
    s = np.zeros(n)
    for t in range(1, n):
        s[t] = (1 - kappa) * s[t - 1] + sigma * rng.standard_normal()
    return s


def _cointegrated(kappa=0.08, n=400, seed=1, push=-0.03):
    rng = np.random.default_rng(seed)
    log_b = np.log(50.0) + np.cumsum(rng.normal(0, 0.01, n))
    s = _ou_spread(kappa, n=n, seed=seed + 7)
    s[-1] += push
    log_a = 0.2 + 0.9 * log_b + s
    return log_a, log_b


@pytest.mark.filterwarnings("ignore")
def test_joint_ecm_recovers_beta_and_kappa():
    log_a, log_b = _cointegrated(kappa=0.08)
    fit = fit_error_correction(
        log_a, log_b, draws=200, tune=200, chains=2, cores=1,
        progressbar=False, seed=0,
    )
    assert fit.converged
    assert fit.beta_mean == pytest.approx(0.9, abs=0.12)
    assert fit.kappa_mean > 0.03
    assert fit.p_kappa_gt > 0.9
    assert fit.eg_tstat < -3.0
    # Serial dependence should live in κ, not in the innovations.
    assert abs(fit.innovation_rho) < 0.25


@pytest.mark.filterwarnings("ignore")
def test_joint_ecm_rejects_independent_random_walks():
    rejected = 0
    for seed in range(8):
        rng = np.random.default_rng(seed)
        a = np.cumsum(rng.normal(0, 0.01, 400))
        b = np.cumsum(rng.normal(0, 0.01, 400))
        fit = fit_error_correction(
            a, b, draws=150, tune=150, chains=2, cores=1,
            progressbar=False, seed=seed,
        )
        if fit.p_kappa_gt < 0.80 or fit.eg_tstat > -3.04:
            rejected += 1
    assert rejected >= 6


@pytest.mark.filterwarnings("ignore")
def test_joint_beta_sd_exceeds_iid_two_step_on_ar_residuals():
    log_a, log_b = _cointegrated(kappa=0.03, n=500, seed=2)
    ecm = fit_error_correction(
        log_a, log_b, draws=200, tune=200, chains=2, cores=1,
        progressbar=False, seed=0,
    )
    coint = fit_cointegration(
        log_a, log_b, draws=200, tune=200, chains=2, cores=1,
        progressbar=False, seed=0,
    )
    # i.i.d. regression sd is too small; joint (or inflation-corrected) is larger.
    assert ecm.beta_sd > coint.beta_sd_iid
    assert coint.beta_sd >= coint.beta_sd_iid  # inflation already applied in two-step


def test_simulate_trade_returns_accepts_per_draw_entry_spreads():
    rng = np.random.default_rng(0)
    n = 500
    k = np.clip(0.05 + 0.01 * rng.standard_normal(n), 0, 1)
    sig = np.full(n, 0.004)
    nu = np.full(n, 6.0)
    # Same mean entry, but with β noise the terminal residual disperses.
    s0 = -0.03 + 0.01 * rng.standard_normal(n)
    policy = ExitPolicy(exit_mode="cross", cross_frac=0.75, stop_mult=3.0,
                        horizon_bars=80, check_every_bars=4, cost_frac=0.001)
    with_noise = simulate_trade_returns(s0, k, sig, nu, policy, seed=0)
    without = simulate_trade_returns(-0.03, k, sig, nu, policy, seed=0)
    # Entry-level uncertainty should widen the loss tail.
    assert with_noise.stress_loss_frac >= without.stress_loss_frac - 1e-6
