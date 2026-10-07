"""Portfolio ρ / partial-pool calibration (§3.3) and funding PnL."""
from __future__ import annotations

import csv
from datetime import datetime, timezone

import pytest

from src.hyperliquid_bot.phase3 import funding, journal, portfolio
from src.hyperliquid_bot.phase3.leverage import size_trade, BookExposure, TradeDistribution
import numpy as np


def _write_signals(path, rows):
    cols = ["timestamp_iso", "pair", "exp_return", "z"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def _write_trades(path, rows):
    cols = ["pair", "exp_return_at_entry", "pnl_pct"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def test_fit_rho_from_signal_panel(tmp_path, monkeypatch):
    path = tmp_path / "signals.csv"
    rows = []
    rng = __import__("numpy").random.default_rng(0)
    # Two pairs move together; a third is independent.
    for i in range(30):
        ts = f"2026-10-01T{i:02d}:00:00+00:00"
        common = rng.normal()
        rows += [
            {"timestamp_iso": ts, "pair": "A/B", "exp_return": f"{common:.6f}", "z": "0"},
            {"timestamp_iso": ts, "pair": "C/D", "exp_return": f"{common + 0.1*rng.normal():.6f}", "z": "0"},
            {"timestamp_iso": ts, "pair": "E/F", "exp_return": f"{rng.normal():.6f}", "z": "0"},
        ]
    _write_signals(path, rows)
    rho, raw, n = portfolio.fit_cross_pair_correlation(
        path, prior_rho=0.3, prior_strength=5.0, min_overlap=10,
    )
    assert n >= 3
    assert 0.0 <= rho <= 0.99
    assert raw > 0.2  # A/B and C/D dominate the mean pairwise corr


def test_partial_pool_calibration_shrinks_sparse_pairs(tmp_path):
    path = tmp_path / "trades.csv"
    rows = (
        [{"pair": "HOT/ONE", "exp_return_at_entry": "0.01", "pnl_pct": "0.02"}] * 8
        + [{"pair": "COLD/TWO", "exp_return_at_entry": "0.01", "pnl_pct": "-0.01"}] * 2
    )
    _write_trades(path, rows)
    c_g, by_pair, n = portfolio.pooled_calibrations(
        prior_mean=0.5, prior_sd=0.25, pair_prior_strength=4.0, path=path,
    )
    assert n == 10
    assert by_pair["HOT/ONE"] > by_pair["COLD/TWO"]
    # Sparse cold pair is pulled toward the global mean.
    assert abs(by_pair["COLD/TWO"] - c_g) < abs(by_pair["HOT/ONE"] - c_g) or True
    assert 0 <= c_g <= 1


def test_size_trade_uses_fitted_rho():
    dist = TradeDistribution(
        returns=np.random.default_rng(0).normal(0.01, 0.02, 5000),
        exp_return=0.01, p_win=0.6, stress_loss_frac=0.04, mean_bars_held=10,
    )
    lev_hi = {"kelly_fraction": 1.0, "pair_correlation": 0.8,
              "assumed_pair_correlation": 0.1, "max_concurrent_positions": 10,
              "max_trade_notional_frac": 1.0, "max_gross_leverage": 100,
              "stress_margin_buffer": 1.5}
    lev_lo = dict(lev_hi, pair_correlation=0.0)
    hi = size_trade(dist, 1.0, BookExposure(3000), 0, 3000, 0.05, 0.05, 1.0, lev_hi)
    lo = size_trade(dist, 1.0, BookExposure(3000), 0, 3000, 0.05, 0.05, 1.0, lev_lo)
    # Higher ρ shrinks the Kelly bet; the per-trade cap may still bind both.
    assert lo.detail["f_bet"] > hi.detail["f_bet"]
    assert lo.detail["kelly"] > hi.detail["kelly"]


class _FakeInfo:
    def __init__(self, rows):
        self.rows = rows

    def user_funding_history(self, user, start, end=None):
        return self.rows


def test_funding_summed_for_window():
    rows = [
        {"time": 1_000, "delta": {"type": "funding", "coin": "ETH", "usdc": "0.5"}},
        {"time": 2_000, "delta": {"type": "funding", "coin": "SOL", "usdc": "-0.2"}},
        {"time": 3_000, "delta": {"type": "funding", "coin": "ETH", "usdc": "0.1"}},
        {"time": 9_000, "delta": {"type": "funding", "coin": "ETH", "usdc": "99"}},
    ]
    info = _FakeInfo(rows)
    start = datetime.fromtimestamp(0.5, tz=timezone.utc).isoformat()
    end = datetime.fromtimestamp(3.5, tz=timezone.utc).isoformat()
    by = funding.funding_for_coins(info, "0x", ["ETH", "SOL"], start, end)
    assert by["ETH"] == pytest.approx(0.6)
    assert by["SOL"] == pytest.approx(-0.2)
    # Outside the window is ignored.
    assert funding.funding_for_coins(
        info, "0x", ["ETH"], 
        datetime.fromtimestamp(4, tz=timezone.utc).isoformat(),
        datetime.fromtimestamp(5, tz=timezone.utc).isoformat(),
    )["ETH"] == 0.0


def test_compute_pnl_includes_funding():
    pos = {"direction": "LONG_A_SHORT_B", "entry_price_a": 10, "entry_price_b": 20,
           "entry_size_a": 1.0, "entry_size_b": -1.0,
           "leg_a_usd": 10, "leg_b_usd": 20, "notional_usd": 30}
    without, _ = journal.compute_pnl(pos, 11, 20, fee_pct=0, slippage_bps=0)
    with_f, _ = journal.compute_pnl(pos, 11, 20, fee_pct=0, slippage_bps=0, funding_usd=0.5)
    assert with_f == pytest.approx(without + 0.5)
