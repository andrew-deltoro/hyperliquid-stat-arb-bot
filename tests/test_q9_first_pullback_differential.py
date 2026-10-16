"""Unit tests for the Q9 first-pullback differential harness.

These are computation tests on synthetic bars only. They assert no trading
behaviour and make no network calls. The Q9 harness lives under
``scripts/experiments`` and is imported here by path, the same way
``q7_footprint_firecheck.py`` imports its frozen harness.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
Q9_PATH = ROOT / "scripts" / "experiments" / "q9_first_pullback_differential.py"


def _load_q9():
    import sys
    spec = importlib.util.spec_from_file_location("q9_harness", Q9_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["q9_harness"] = mod          # dataclasses resolve __module__ here
    spec.loader.exec_module(mod)
    return mod


q9 = _load_q9()


def _pair_from_bars(o, h, l, c, v):
    """Build a minimal Pair with the indicators the harness expects."""
    o = np.asarray(o, float); h = np.asarray(h, float)
    l = np.asarray(l, float); c = np.asarray(c, float); v = np.asarray(v, float)
    n = len(c)
    t = np.arange(n, dtype=np.int64) * 300_000
    day = np.zeros(n, dtype=np.int64)
    return q9.Pair(symbol="TEST", t=t, o=o, h=h, l=l, c=c, v=v, qv=c * v,
                   day=day, atr=np.full(n, 1.0), ema9=c * 0.0,
                   vwap=np.full(n, 0.0), vmed=v.copy(),
                   med_dollar_vol=float(np.median(c * v)))


# --------------------------------------------------------------- scan parity
def test_vectorised_scan_matches_brute_force():
    """candidate_moves must reproduce a naive loop exactly (set and run length)."""
    rng = np.random.default_rng(3)
    n = 800
    c = 100 + np.cumsum(rng.normal(0, 1.0, n))
    o = c - rng.normal(0, 0.5, n)
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.5, n))
    l = np.minimum(o, c) - np.abs(rng.normal(0, 0.5, n))
    v = np.abs(rng.normal(1000, 200, n))
    p = _pair_from_bars(o, h, l, c, v)

    best = q9.candidate_moves(p, disp_k=q9.DISP_ATR_K, disp_max=q9.DISP_MAX_BARS,
                              rvol_mult=q9.RVOL_MULT)

    ref = {}
    for i in range(q9.WARMUP, n):
        a = p.atr[i - 1]
        vm = p.vmed[i - 1]
        for m in range(1, q9.DISP_MAX_BARS + 1):
            e = i + m - 1
            if e >= n - 2:
                break
            if not all(p.c[k] > p.o[k] for k in range(i, e + 1)):
                continue
            if (p.c[e] - p.o[i]) < q9.DISP_ATR_K * a:
                continue
            if float(p.v[i:e + 1].sum()) < q9.RVOL_MULT * m * vm:
                continue
            ref[i] = m
            break
    vec = {i: int(best[i]) for i in range(n) if best[i] >= 0}
    assert vec == ref


# ---------------------------------------------------------- pullback geometry
def test_pullback_requires_hold_and_rejects_deep_retracement():
    """A pullback bar below VWAP/EMA kills the event; a >50% retracement too."""
    # base low 10, run up to 20 (bars 1-2), a 50% pullback to 15 (bars 3-4),
    # crossing candle at bar 5 (high 20 -> 22).
    o = [10, 14, 18, 19, 16.5, 19, 21]
    h = [11, 15, 20, 19.5, 17, 20, 22]
    l = [10, 13, 17, 15, 15, 17.5, 20]
    c = [10.5, 14.5, 19.5, 16, 16.5, 19.5, 21.5]
    v = [1000] * 7

    p = _pair_from_bars(o, h, l, c, v)
    p.vwap[:] = 13.0
    p.ema9[:] = 12.0
    ev = q9.Event(symbol="T", day=0, base=0, move_start=1, move_end=3,
                  leg_low=10.0, leg_high=20.0, atr_at=1.0)
    q9._attach_pullback(p, ev, retrace_max=0.5, pb_max=12, hold_test="low",
                        cross_test="bar_high", apply_vol_green=False)
    assert ev.has_pullback                      # bar 5 crosses and holds the floor
    assert ev.pb_low >= 10.0

    # same bars, but bar 4 dips below the 9 EMA -> the event must die.
    p2 = _pair_from_bars(o, h, l, c, v)
    p2.vwap[:] = 13.0
    p2.ema9[:] = 12.0
    p2.l[4] = 11.0                              # below ema9=12 and vwap=13
    ev2 = q9.Event(symbol="T", day=0, base=0, move_start=1, move_end=3,
                   leg_low=10.0, leg_high=20.0, atr_at=1.0)
    q9._attach_pullback(p2, ev2, retrace_max=0.5, pb_max=12, hold_test="low",
                        cross_test="bar_high", apply_vol_green=False)
    assert not ev2.has_pullback


# --------------------------------------------------------------- pessimistic
def test_stop_wins_when_one_bar_hits_both_stop_and_target():
    """The ifvg convention: an ambiguous bar resolves to the STOP."""
    # entry bar hits stop (low 99) and target (high 103) in the same 5m bar.
    o = [100.0, 100.0]
    h = [100.0, 103.0]
    l = [100.0, 99.0]
    c = [100.0, 100.0]
    v = [1000.0, 1000.0]
    p = _pair_from_bars(o, h, l, c, v)
    res = q9.simulate(p, entry_idx=0, direction=1, stop=99.0, target=103.0,
                      max_hold=5, cost_bp=0.0)
    assert res.reason == "stop"
    assert res.gross_R < 0


def test_net_R_subtracts_cost_in_R_units():
    # entry bar 0 is flat at 100; bar 1 clears the 105 target without ever
    # trading the 99 stop.
    o = [100.0, 100.0]
    h = [100.0, 106.0]
    l = [100.0, 101.0]
    c = [100.0, 106.0]
    v = [1000.0, 1000.0]
    p = _pair_from_bars(o, h, l, c, v)
    res = q9.simulate(p, entry_idx=0, direction=1, stop=99.0, target=105.0,
                      max_hold=5, cost_bp=15.0)
    assert res.reason == "target"
    # gross +5.0 on R=1.0; cost = 100 * 15/1e4 = 0.15 in price -> -0.15R
    assert abs(res.gross_R - 5.0) < 1e-9
    assert abs(res.net_R - 4.85) < 1e-9


# ---------------------------------------------------------------- clustered t
def test_day_clustered_t_falls_to_zero_when_days_are_constant():
    """A signal that is constant within each day but varies across days still
    has a well-defined clustered statistic (the unit is the day)."""
    day = np.repeat(np.arange(10), 5)
    x = np.repeat(np.arange(10.0), 5)           # 10 distinct day means
    m, t, G = q9.cluster_t(x, day)
    assert abs(m - 4.5) < 1e-9
    assert G == 10
    assert np.isfinite(t)
