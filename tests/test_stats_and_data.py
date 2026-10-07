"""Cointegration diagnostics (H6), market data (M1, M2), BTC vol filter (H7)."""
from __future__ import annotations

import numpy as np
import pytest

from src.hyperliquid_bot.data import ohlcv
from src.hyperliquid_bot.data.ohlcv import Candle, DataQualityError, check_series, closed_only
from src.hyperliquid_bot.phase3 import scheduler
from src.hyperliquid_bot.stats.cointegration import (
    EG_CRITICAL,
    autocorr_inflation,
    engle_granger_tstat,
    residual_autocorr,
)

H = 3_600_000


def ou_path(kappa, n=720, seed=0):
    rng = np.random.default_rng(seed)
    s = np.zeros(n)
    for t in range(1, n):
        s[t] = (1 - kappa) * s[t - 1] + 0.01 * rng.standard_normal()
    return s


def test_engle_granger_rejects_independent_random_walks():
    rejected = 0
    for seed in range(20):
        rng = np.random.default_rng(seed)
        a = np.cumsum(rng.normal(0, 0.01, 720))
        b = np.cumsum(rng.normal(0, 0.01, 720))
        beta, alpha = np.polyfit(b, a, 1)
        if engle_granger_tstat(a - alpha - beta * b) > EG_CRITICAL[0.10]:
            rejected += 1
    assert rejected >= 16


def test_engle_granger_accepts_fast_reverting_spread():
    assert engle_granger_tstat(ou_path(0.05)) < EG_CRITICAL[0.01]


def test_beta_sd_inflation_grows_with_autocorrelation():
    rho = residual_autocorr(ou_path(0.02))
    assert rho > 0.9
    assert autocorr_inflation(rho) > 3
    assert autocorr_inflation(0.0) == 1.0


def test_in_progress_bar_is_dropped():
    now = 10 * H + H // 2
    bars = [Candle(t=i * H, o=1, h=1, l=1, c=1, v=1) for i in range(11)]
    kept = closed_only(bars, H, now_ms=now)
    assert kept[-1].t == 9 * H


def test_series_checks():
    now = 100 * H + 10
    ok = [i * H for i in range(1, 100)]
    check_series(ok, H, now_ms=now)
    with pytest.raises(DataQualityError, match="old"):
        check_series(ok[:-5], H, now_ms=now)
    with pytest.raises(DataQualityError, match="gap"):
        check_series(ok[:40] + ok[45:], H, now_ms=now)


def test_network_selects_info_url():
    ohlcv.configure("testnet")
    assert "testnet" in ohlcv.HL_INFO_URL
    ohlcv.configure("mainnet")
    assert ohlcv.HL_INFO_URL == ohlcv.MAINNET_INFO_URL


def test_btc_vol_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(scheduler, "fetch_candles", boom)
    assert scheduler._get_btc_vol_24h() is None


def test_cycle_lock_is_exclusive():
    first = scheduler._acquire_cycle_lock()
    try:
        assert first is not None
        assert scheduler._acquire_cycle_lock() is None
    finally:
        first.close()
    again = scheduler._acquire_cycle_lock()
    assert again is not None
    again.close()


def test_live_mode_requires_runtime_dir(monkeypatch, tmp_path):
    cfg = tmp_path / "live.yaml"
    cfg.write_text("pairs: []\ndata: {}\nmodel: {}\nsignal: {}\nposition: {}\n"
                   "risk: {}\nou_prior: {}\nexecutor: {mode: live}\n")
    monkeypatch.delenv("HLBOT_RUNTIME_DIR", raising=False)
    with pytest.raises(RuntimeError, match="HLBOT_RUNTIME_DIR"):
        scheduler.run_cycle(config_path=cfg)
