"""Shock detector: features, cooldown, and the default-off guarantee."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.hyperliquid_bot.data.ohlcv import Candle
from src.hyperliquid_bot.phase3 import shock as shock_mod
from src.hyperliquid_bot.phase3.shock import compute_shock, pair_shock_state


@dataclass
class _SignalCfg:
    signal: dict
    data: dict


def _cfg(signal: dict, lookback_hours: int = 200) -> _SignalCfg:
    return _SignalCfg(signal=signal, data={"interval": "1h", "lookback_hours": lookback_hours})


def _bars(closes, volumes, t0: int = 1_700_000_000_000):
    return [
        Candle(t=t0 + i * 3_600_000, o=c, h=c, l=c, c=c, v=v)
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


def _flat_then_spike(spike_idx: int, n: int = 200, spike_ret: float = 0.10,
                     base_vol: float = 100.0, spike_vol: float = 5000.0):
    """Constant price with a small deterministic jitter, then one big move."""
    rng = np.random.default_rng(0)
    closes = np.full(n, 100.0)
    for i in range(1, n):
        step = 0.001 if i % 7 == 0 else -0.0005 if i % 5 == 0 else 0.0
        closes[i] = closes[i - 1] * (1.0 + step)
    if spike_idx is not None:
        closes[spike_idx] = closes[spike_idx - 1] * (1.0 + spike_ret)
        for j in range(spike_idx + 1, n):
            closes[j] = closes[spike_idx]
    vols = np.full(n, base_vol) + rng.normal(0, 1.0, n)
    if spike_idx is not None:
        vols[spike_idx] = spike_vol
    return _bars(closes, vols)


def test_detects_qualifying_shock_on_latest_bar():
    bars = _flat_then_spike(spike_idx=199)
    st = compute_shock(bars, "TEST")
    assert st is not None
    assert st.is_shock is True
    assert st.hours_since == 0
    assert st.magnitude > 0.05
    assert st.vol_z >= 3.0


def test_no_shock_when_move_too_small():
    bars = _flat_then_spike(spike_idx=199, spike_ret=0.01)
    st = compute_shock(bars, "TEST")
    assert st is not None
    assert st.is_shock is False
    assert st.hours_since is None


def test_no_shock_when_volume_quiet():
    bars = _flat_then_spike(spike_idx=199, spike_ret=0.10, spike_vol=100.0)
    st = compute_shock(bars, "TEST")
    assert st is not None
    assert st.is_shock is False


def test_hours_since_counts_bars_after_shock():
    bars = _flat_then_spike(spike_idx=190)
    st = compute_shock(bars, "TEST")
    assert st is not None
    assert st.is_shock is False
    assert st.hours_since == 9


def test_old_shock_is_not_in_cooldown_but_is_reported(monkeypatch):
    """A shock 9h ago is outside a 6h cooldown yet still journalled."""
    a = _flat_then_spike(spike_idx=190)
    b = _flat_then_spike(spike_idx=None)
    monkeypatch.setattr(shock_mod, "fetch_candles",
                        lambda coin, interval="1h", lookback_hours=720: a if coin == "AAA" else b)
    cfg = _cfg({"shock_guard": {"block": True, "block_hours": 6}})
    _, _, summary = pair_shock_state("AAA", "BBB", cfg)
    assert summary["is_shock"] is False
    assert summary["hours_since"] == 9
    assert summary["in_cooldown"] is False
    assert summary["magnitude"] > 0.05


def test_returns_none_when_history_too_short():
    bars = _flat_then_spike(spike_idx=None, n=50)
    assert compute_shock(bars, "TEST") is None


def test_default_is_descriptive_only():
    """The whole safety property: absent/empty config, nothing blocks."""
    assert shock_mod.DEFAULTS["block"] is False
    p = shock_mod._params(_cfg({}))
    assert p["block"] is False


def test_pair_summary_reports_side_and_cooldown(monkeypatch):
    a = _flat_then_spike(spike_idx=199)
    b = _flat_then_spike(spike_idx=None)

    def fake_fetch(coin, interval="1h", lookback_hours=720):
        return a if coin == "AAA" else b

    monkeypatch.setattr(shock_mod, "fetch_candles", fake_fetch)
    cfg = _cfg({"shock_guard": {"block": True, "block_hours": 6}})
    _, _, summary = pair_shock_state("AAA", "BBB", cfg)
    assert summary["side"] == "A"
    assert summary["is_shock"] is True
    assert summary["in_cooldown"] is True
    assert summary["block"] is True


def test_pair_summary_no_shock(monkeypatch):
    b = _flat_then_spike(spike_idx=None)
    monkeypatch.setattr(shock_mod, "fetch_candles",
                        lambda coin, interval="1h", lookback_hours=720: b)
    cfg = _cfg({"shock_guard": {"block": True}})
    _, _, summary = pair_shock_state("AAA", "BBB", cfg)
    assert summary["side"] is None
    assert summary["is_shock"] is False
    assert summary["in_cooldown"] is False


def test_fetch_failure_does_not_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(shock_mod, "fetch_candles", boom)
    cfg = _cfg({"shock_guard": {"block": True}})
    sa, sb, summary = pair_shock_state("AAA", "BBB", cfg)
    assert sa is None and sb is None
    assert summary["is_shock"] is False
    assert summary["in_cooldown"] is False
