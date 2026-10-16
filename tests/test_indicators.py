"""Correctness tests for the indicator module.

Each indicator is checked against an independent reference (pandas, or a
hand-computed value) so the module cannot silently drift. These are
computation tests only; they assert no trading behaviour.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.hyperliquid_bot import indicators as ind


RNG = np.random.default_rng(7)
N = 300
_CLOSE = 100 + np.cumsum(RNG.normal(0, 1, N))
_HIGH = _CLOSE + np.abs(RNG.normal(0, 0.5, N))
_LOW = _CLOSE - np.abs(RNG.normal(0, 0.5, N))
_VOL = np.abs(RNG.normal(1000, 200, N))


# ---------------------------------------------------------------- helpers
def test_rejects_non_finite():
    with pytest.raises(ValueError):
        ind.sma([1.0, 2.0, float("nan")], 2)


def test_rejects_length_mismatch():
    with pytest.raises(ValueError):
        ind.vwap([1.0, 2.0], [1.0], 2)


def test_rejects_bad_window():
    with pytest.raises(ValueError):
        ind.sma([1.0, 2.0], 0)
    with pytest.raises(ValueError):
        ind.rsi([1.0, 2.0, 3.0], 1)


# ---------------------------------------------------------------- sma/ema
def test_sma_matches_pandas_and_warmup():
    got = ind.sma(_CLOSE, 20)
    assert np.all(np.isnan(got[:19]))
    assert not np.isnan(got[19])
    exp = pd.Series(_CLOSE).rolling(20).mean().to_numpy()
    np.testing.assert_allclose(got[19:], exp[19:], rtol=1e-10)


def test_ema_matches_pandas_adjust_false():
    got = ind.ema(_CLOSE, 12)
    exp = pd.Series(_CLOSE).ewm(span=12, adjust=False).mean().to_numpy()
    np.testing.assert_allclose(got, exp, rtol=1e-10)


# ---------------------------------------------------------------- rsi
def test_rsi_matches_wilder_reference():
    """Wilder RSI(14), computed independently with an explicit loop."""
    period = 14
    delta = np.diff(_CLOSE)
    gain = np.clip(delta, 0, None)
    loss = np.clip(-delta, 0, None)
    ag = gain[:period].mean()
    al = loss[:period].mean()
    exp = np.full(N, np.nan)
    exp[period] = 100 - 100 / (1 + ag / al)
    for i in range(period + 1, N):
        ag = (ag * (period - 1) + gain[i - 1]) / period
        al = (al * (period - 1) + loss[i - 1]) / period
        exp[i] = 100 - 100 / (1 + ag / al)
    got = ind.rsi(_CLOSE, period)
    np.testing.assert_allclose(got[period:], exp[period:], rtol=1e-10)


def test_rsi_bounds_and_edges():
    up = np.arange(1, 60, dtype=float)
    assert ind.rsi(up, 14)[-1] == pytest.approx(100.0)
    down = np.arange(60, 1, -1, dtype=float)
    assert ind.rsi(down, 14)[-1] == pytest.approx(0.0)
    flat = np.full(60, 5.0)
    assert ind.rsi(flat, 14)[-1] == pytest.approx(50.0)
    assert np.all(np.isnan(ind.rsi(_CLOSE, 14)[:14]))  # warm-up


# ---------------------------------------------------------------- vwap
def test_vwap_matches_manual_rolling():
    w = 24
    c = pd.Series(_CLOSE)
    v = pd.Series(_VOL)
    exp = (c * v).rolling(w).sum() / v.rolling(w).sum()
    got = ind.vwap(_CLOSE, _VOL, w)
    np.testing.assert_allclose(got[w - 1:], exp.to_numpy()[w - 1:], rtol=1e-10)


def test_vwap_zero_volume_is_nan():
    got = ind.vwap([1.0, 2.0, 3.0], [0.0, 0.0, 0.0], 2)
    assert np.all(np.isnan(got))


# ---------------------------------------------------------------- macd
def test_macd_matches_pandas():
    close = pd.Series(_CLOSE)
    line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    sig = line.ewm(span=9, adjust=False).mean()
    got_line, got_sig, got_hist = ind.macd(_CLOSE)
    np.testing.assert_allclose(got_line, line.to_numpy(), rtol=1e-9)
    np.testing.assert_allclose(got_sig, sig.to_numpy(), rtol=1e-9)
    np.testing.assert_allclose(got_hist, (line - sig).to_numpy(), rtol=1e-9)
    with pytest.raises(ValueError):
        ind.macd(_CLOSE, fast=26, slow=12)


# ---------------------------------------------------------------- bollinger
def test_bollinger_population_std():
    w = 20
    c = pd.Series(_CLOSE)
    mid = c.rolling(w).mean()
    sd = c.rolling(w).std(ddof=0)
    gm, gu, gl = ind.bollinger(_CLOSE, w, 2.0)
    np.testing.assert_allclose(gm[w - 1:], mid.to_numpy()[w - 1:], rtol=1e-10)
    np.testing.assert_allclose(gu[w - 1:], (mid + 2 * sd).to_numpy()[w - 1:], rtol=1e-9)
    np.testing.assert_allclose(gl[w - 1:], (mid - 2 * sd).to_numpy()[w - 1:], rtol=1e-9)


# ---------------------------------------------------------------- stochastic
def test_stochastic_matches_independent_reference():
    """Slow stochastic 14,3,3 computed with an explicit rolling reference."""
    kp, ks, ds = 14, 3, 3
    hh = pd.Series(_HIGH).rolling(kp).max()
    ll = pd.Series(_LOW).rolling(kp).min()
    raw = 100 * (pd.Series(_CLOSE) - ll) / (hh - ll)
    exp_k = raw.rolling(ks).mean()
    exp_d = exp_k.rolling(ds).mean()
    got_k, got_d = ind.stochastic(_HIGH, _LOW, _CLOSE, kp, ks, ds)
    # compare only where the reference is defined and finite
    m = exp_k.notna().to_numpy()
    np.testing.assert_allclose(got_k[m], exp_k.to_numpy()[m], rtol=1e-10)
    m2 = exp_d.notna().to_numpy()
    np.testing.assert_allclose(got_d[m2], exp_d.to_numpy()[m2], rtol=1e-10)


def test_stochastic_warmup_and_bounds():
    got_k, got_d = ind.stochastic(_HIGH, _LOW, _CLOSE, 14, 3, 3)
    assert np.all(np.isnan(got_k[:15]))       # kp + ks - 2 = 15
    assert np.isfinite(got_k[15])
    assert np.all(np.isnan(got_d[:17]))       # + ds - 1 = 17
    fin = got_k[np.isfinite(got_k)]
    assert fin.min() >= 0.0 and fin.max() <= 100.0


def test_stochastic_flat_range_is_fifty():
    flat = np.full(40, 10.0)
    k, d = ind.stochastic(flat, flat, flat, 14, 3, 3)
    assert k[-1] == pytest.approx(50.0)
    assert d[-1] == pytest.approx(50.0)


def test_stochastic_rejects_bad_params():
    with pytest.raises(ValueError):
        ind.stochastic(_HIGH, _LOW, _CLOSE, 0, 3, 3)
    with pytest.raises(ValueError):
        ind.stochastic(_HIGH, _LOW, _CLOSE, 14, 0, 3)


# ---------------------------------------------------------------- atr
def test_true_range_first_bar_is_high_low():
    tr = ind.true_range([10.0, 12.0], [8.0, 11.0], [9.0, 11.5])
    assert tr[0] == pytest.approx(2.0)
    # second bar: max(12-11, |12-9|, |11-9|) = 3
    assert tr[1] == pytest.approx(3.0)


def test_atr_matches_wilder_reference():
    period = 14
    tr = ind.true_range(_HIGH, _LOW, _CLOSE)
    exp = np.full(N, np.nan)
    exp[period - 1] = tr[:period].mean()
    for i in range(period, N):
        exp[i] = (exp[i - 1] * (period - 1) + tr[i]) / period
    got = ind.atr(_HIGH, _LOW, _CLOSE, period)
    np.testing.assert_allclose(got[period - 1:], exp[period - 1:], rtol=1e-10)


# ---------------------------------------------------------------- snapshot
def test_snapshot_keys_and_last_values():
    snap = ind.indicator_snapshot(_HIGH, _LOW, _CLOSE, _VOL)
    for k in ("close", "rsi", "vwap", "macd", "macd_signal", "macd_hist",
              "bb_mid", "bb_upper", "bb_lower", "bb_pct_b", "ma", "atr", "atr_pct"):
        assert k in snap, k
    # last snapshot values equal the final array value of each indicator
    assert snap["close"] == pytest.approx(_CLOSE[-1])
    assert snap["rsi"] == pytest.approx(ind.rsi(_CLOSE, 14)[-1])
    assert snap["ma"] == pytest.approx(ind.sma(_CLOSE, 200)[-1])
    assert 0.0 <= snap["bb_pct_b"] <= 1.0 or np.isnan(snap["bb_pct_b"])


def test_snapshot_short_series_is_nan_not_error():
    snap = ind.indicator_snapshot([1.0, 2.0], [0.5, 1.0], [0.8, 1.5], [10.0, 20.0])
    assert np.isnan(snap["rsi"])
    assert np.isnan(snap["ma"])
    assert np.isfinite(snap["close"])
