"""End-to-end generate_signal on synthetic prices (small NUTS budgets)."""
from __future__ import annotations

import numpy as np
import pytest

from src.hyperliquid_bot.phase3.config import Phase3Config
from src.hyperliquid_bot.phase3.signals import generate_signal


def cfg(kelly=True):
    raw = {
        "fees": {"default_fee_pct": 0.00045, "slippage_bps": 3},
        "leverage": {"mode": "kelly"} if kelly else {},
    }
    return Phase3Config(
        pairs=[], data={"interval": "1h", "lookback_hours": 600},
        model={"coint_draws": 150, "coint_tune": 150, "ou_draws": 150,
               "ou_tune": 150, "chains": 2, "cores": 1},
        signal={"z_entry_min": 1.0, "edge_min": 0.05, "max_btc_vol": 0.55,
                "half_life_max_hours": 96, "stationarity_level": 0.10},
        position={"target_frac": 0.75, "stop_mult": 3.0, "exit_horizon_mult": 3.33,
                  "exit_mode": "cross", "cross_exit_frac": 0.75,
                  "base_notional": 1000.0},
        risk={}, ou_prior={}, raw=raw,
    )


def prices(kappa, n=600, seed=3, push=-0.03):
    rng = np.random.default_rng(seed)
    log_b = np.log(50.0) + np.cumsum(rng.normal(0, 0.01, n))
    s = np.zeros(n)
    for t in range(1, n):
        s[t] = (1 - kappa) * s[t - 1] + 0.004 * rng.standard_normal()
    s[-1] += push
    log_a = 0.2 + 0.9 * log_b + s
    return np.exp(log_a), np.exp(log_b)


@pytest.mark.filterwarnings("ignore")
def test_cointegrated_pair_produces_consistent_signal():
    a, b = prices(kappa=0.08)
    sig = generate_signal("AAA", "BBB", cfg(), btc_vol=0.3, prices=(a, b))
    assert sig.beta == pytest.approx(0.9, abs=0.15)
    assert sig.beta_sd > 0
    assert sig.eg_tstat < -3.04
    assert 0 < sig.half_life_hours < 96
    assert sig.trade_returns is not None and len(sig.trade_returns) > 0
    if sig.action == "OPEN":
        assert sig.direction == "LONG_A_SHORT_B"
        assert sig.exp_return > 0
    else:
        assert sig.skip_reason


@pytest.mark.filterwarnings("ignore")
def test_unknown_btc_vol_blocks_entry():
    a, b = prices(kappa=0.08)
    sig = generate_signal("AAA", "BBB", cfg(), btc_vol=None, prices=(a, b))
    assert sig.action == "SKIP"
    assert "btc_vol_unknown" in sig.skip_reason
