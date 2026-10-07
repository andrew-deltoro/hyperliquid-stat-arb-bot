"""Posterior-predictive edge (H5) and leverage sizing."""
from __future__ import annotations

import numpy as np
import pytest

from src.hyperliquid_bot.phase3.leverage import (
    BookExposure,
    ExitPolicy,
    calibration_factor,
    kelly_fraction,
    simulate_trade_returns,
    size_trade,
)

N_DRAWS = 2000
POLICY = ExitPolicy(exit_mode="cross", cross_frac=0.75, stop_mult=3.0,
                    horizon_bars=120, check_every_bars=4, cost_frac=0.0012)


def draws(kappa, sigma=0.004, nu=6.0, sd=0.0):
    rng = np.random.default_rng(1)
    k = np.clip(kappa + sd * rng.standard_normal(N_DRAWS), 0.0, 1.0)
    return k, np.full(N_DRAWS, sigma), np.full(N_DRAWS, nu)


def test_random_walk_has_no_edge_and_zero_size():
    k, s, nu = draws(kappa=0.0)
    dist = simulate_trade_returns(-0.03, k, s, nu, POLICY)
    assert dist.exp_return <= 0
    assert kelly_fraction(dist.returns) == 0.0


def test_mean_reverting_spread_has_positive_edge():
    k, s, nu = draws(kappa=0.05, sd=0.01)
    dist = simulate_trade_returns(-0.03, k, s, nu, POLICY)
    assert dist.exp_return > 0
    assert 0 < dist.p_win < 1
    assert dist.stress_loss_frac > 0
    assert kelly_fraction(dist.returns) > 0


def test_cycle_checks_let_losses_run_past_the_stop():
    k, s, nu = draws(kappa=0.0, sigma=0.01)
    hourly = simulate_trade_returns(-0.02, k, s, nu,
                                    ExitPolicy(stop_mult=1.5, horizon_bars=96, check_every_bars=1))
    four_h = simulate_trade_returns(-0.02, k, s, nu,
                                    ExitPolicy(stop_mult=1.5, horizon_bars=96, check_every_bars=4))
    # Stop loss alone would be 0.5 * 0.5 * 0.02 = 0.005 per unit notional.
    assert four_h.stress_loss_frac > hourly.stress_loss_frac > 0.005


def test_simulation_is_deterministic_for_a_seed():
    k, s, nu = draws(kappa=0.03)
    a = simulate_trade_returns(-0.03, k, s, nu, POLICY, seed=7)
    b = simulate_trade_returns(-0.03, k, s, nu, POLICY, seed=7)
    assert np.array_equal(a.returns, b.returns)


def test_kelly_matches_closed_form_for_small_gaussian_bets():
    rng = np.random.default_rng(0)
    r = rng.normal(0.002, 0.02, 200_000)
    assert kelly_fraction(r) == pytest.approx(0.002 / 0.02**2, rel=0.1)


def test_calibration_prior_without_data_and_shrinks_on_bad_record():
    assert calibration_factor([], [], prior_mean=0.5) == 0.5
    predicted = [0.01] * 30
    realized = list(np.random.default_rng(0).normal(0.0, 0.02, 30))
    assert calibration_factor(predicted, realized, prior_mean=0.5) < 0.5
    assert calibration_factor(predicted, [0.03] * 30, prior_mean=0.5) == 1.0


def _dist():
    k, s, nu = draws(kappa=0.05, sd=0.01)
    return simulate_trade_returns(-0.03, k, s, nu, POLICY)


LEV = {"kelly_fraction": 0.25, "assumed_pair_correlation": 0.3,
       "max_concurrent_positions": 10, "max_trade_notional_frac": 0.15,
       "max_gross_leverage": 1.5, "stress_margin_buffer": 1.5}


def test_per_trade_cap_binds_on_empty_book():
    d = size_trade(_dist(), 1.0, BookExposure(equity=3000.0), 0.0, 3000.0,
                   0.05, 0.05, 0.5, LEV)
    assert d.notional <= 0.15 * 3000.0 + 1e-6
    assert d.binding in ("per_trade", "kelly")


def test_gross_leverage_cap_binds_when_book_is_full():
    d = size_trade(_dist(), 1.0, BookExposure(equity=3000.0), 4500.0, 3000.0,
                   0.05, 0.05, 0.5, LEV)
    assert d.notional == 0.0
    assert d.binding == "gross_leverage"


def test_stress_cap_binds_when_dex_margin_is_thin():
    book = BookExposure(equity=300.0, gross=800.0, maintenance=80.0, stress_loss=150.0)
    d = size_trade(_dist(), 1.0, book, 800.0, 3000.0, 0.1, 0.1, 0.5, LEV)
    assert d.binding == "liquidation_stress"
    assert d.notional == pytest.approx(
        (300.0 - 150.0 - 1.5 * 80.0) / (_dist().stress_loss_frac + 1.5 * 0.1), rel=1e-6)


def test_zero_calibration_means_zero_size():
    d = size_trade(_dist(), 1.0, BookExposure(equity=3000.0), 0.0, 3000.0,
                   0.05, 0.05, 0.0, LEV)
    assert d.notional == 0.0
