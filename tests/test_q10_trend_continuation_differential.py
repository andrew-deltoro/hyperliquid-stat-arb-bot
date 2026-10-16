"""Unit tests for the Q10 trend-continuation differential harness.

These are computation tests on synthetic bars only. They assert no trading
behaviour and make no network calls. The Q10 harness lives under
``scripts/experiments`` and is imported here by path, the same way the Q9 test
imports its frozen harness.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
Q10_PATH = ROOT / "scripts" / "experiments" / "q10_trend_continuation_differential.py"


def _load_q10():
    import sys
    spec = importlib.util.spec_from_file_location("q10_harness", Q10_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["q10_harness"] = mod          # dataclasses resolve __module__ here
    spec.loader.exec_module(mod)
    return mod


q10 = _load_q10()


def _panel(o, h, l, c, day=None):
    o = np.asarray(o, float); h = np.asarray(h, float)
    l = np.asarray(l, float); c = np.asarray(c, float)
    n = len(c)
    return q10.Panel(symbol="TEST", t=np.arange(n, dtype=np.int64) * 300_000,
                     o=o, h=h, l=l, c=c, v=np.full(n, 1.0), qv=c,
                     day=(np.zeros(n, dtype=np.int64) if day is None else day),
                     atr5=np.ones(n))


# ------------------------------------------------------------- structure
def test_zigzag_enforces_alternation():
    """Pivot types alternate in time; a same-type pair is replaced by the more
    extreme of the two."""
    h = np.array([1, 3, 1, 5, 1, 4, 1], float)
    l = np.array([-1, 1, -2, 2, -3, 2, -1], float)
    hb, hp, lb, lp = q10.zigzag(h, l, p=1)
    piv = sorted([(int(b), 1) for b in hb] + [(int(b), -1) for b in lb])
    types = [t for _, t in piv]
    assert all(types[i] != types[i + 1] for i in range(len(types) - 1))


def test_broken_low_level_activates_below_and_is_invalidated_above():
    """A swing low is broken by a close BELOW it (the down-side impulse), and is
    invalidated by a close back ABOVE it. Activation and invalidation are
    opposite inequalities; the earlier bug made them the same, which left the
    whole short side dead (3232-long / 45-short)."""
    c = np.array([100.0, 99.0, 98.0, 103.0])
    k = np.ones(4)
    act = q10._activation_bars(np.array([0]), np.array([100.0]), c, k, 4,
                               direction=-1)
    assert act[0] == 2                       # first close < 100 - 1 is index 2
    lvl = q10._level_series(4, act, np.array([100.0]), c, k, direction=-1)
    assert lvl[2] == 100.0                   # live while c stays below 100+1
    assert np.isnan(lvl[3])                  # c=103 > 101 -> level failed


def test_broken_high_level_activates_above_and_is_invalidated_below():
    """A swing high is broken by a close ABOVE it, and invalidated by a close
    back below `level - k*ATR`. A close above `level + k*ATR` must NOT kill it."""
    c = np.array([100.0, 101.0, 102.0, 97.0])
    k = np.ones(4)
    act = q10._activation_bars(np.array([0]), np.array([100.0]), c, k, 4,
                               direction=1)
    assert act[0] == 2                       # c > 100 + 1 first at index 2
    lvl = q10._level_series(4, act, np.array([100.0]), c, k, direction=1)
    assert lvl[2] == 100.0
    assert np.isnan(lvl[3])                  # c=97 < 99 -> level failed


def test_build_htf_produces_both_broken_highs_and_lows():
    """Regression guard: a bug previously left level_lo almost always nan and
    produced a 3232-long / 45-short touch split. On a volatile walk that makes
    both new highs and new lows, both level series must be populated."""
    rng = np.random.default_rng(7)
    n = 4000
    c = 100.0 + np.cumsum(rng.normal(0, 0.6, n))
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.3, n))
    l = np.minimum(o, c) - np.abs(rng.normal(0, 0.3, n))
    p = _panel(o, h, l, c)
    htf = q10.build_htf(p, swing_p=q10.SWING_P, impulse_k=q10.IMPULSE_ATR_K)
    assert np.isfinite(htf.level_hi).sum() >= 10
    assert np.isfinite(htf.level_lo).sum() >= 10


# ------------------------------------------------------------- pattern
def test_double_top_confirm_and_close_above_disqualifies():
    # two peaks at 103, then a red confirming candle
    o = [100, 100, 101, 101, 102, 100]
    h = [101, 103, 102, 103, 102, 100]
    l = [99, 100, 100, 100, 99, 98]
    c = [100, 102, 101, 102, 100, 99]        # bar 4 is red -> confirm
    p = _panel(o, h, l, c)
    ok, entry, rej = q10.confirm_pattern(p, t=0, direction=-1, atr5=1.0,
                                         win=12, tol_atr=0.5, conf_bars=6)
    assert ok and entry == 5 and abs(rej - 103.0) < 1e-9

    # same bars but a close ABOVE the double-top zone -> continuation, reject
    c2 = np.array(c, float)
    c2[3] = 104.0                            # closes above the 103 top
    p2 = _panel(o, h, l, c2)
    ok2, _, _ = q10.confirm_pattern(p2, t=0, direction=-1, atr5=1.0,
                                    win=12, tol_atr=0.5, conf_bars=6)
    assert not ok2


def test_double_bottom_confirm_for_longs():
    o = [100, 100, 99, 99, 98, 100]
    h = [101, 101, 100, 100, 100, 102]
    l = [99, 97, 98, 97, 98, 99]
    c = [100, 98, 99, 98, 100, 101]          # bar 4 is green -> confirm
    p = _panel(o, h, l, c)
    ok, entry, rej = q10.confirm_pattern(p, t=0, direction=1, atr5=1.0,
                                         win=12, tol_atr=0.5, conf_bars=6)
    assert ok and entry == 5 and abs(rej - 97.0) < 1e-9


# ------------------------------------------------------------- pessimistic
def test_stop_wins_when_one_bar_hits_both_stop_and_target():
    """The ifvg convention: an ambiguous bar resolves to the STOP."""
    o = [100.0, 100.0]
    h = [100.0, 103.0]
    l = [100.0, 99.0]
    c = [100.0, 100.0]
    p = _panel(o, h, l, c)
    reason, net_R, gross_R = q10.simulate(p, entry_idx=0, direction=1,
                                          stop=99.0, target=103.0, max_hold=5,
                                          cost_bp=0.0)
    assert reason == "stop"
    assert gross_R < 0


def test_net_R_subtracts_cost_in_R_units():
    o = [100.0, 100.0]
    h = [100.0, 106.0]
    l = [100.0, 101.0]
    c = [100.0, 106.0]
    p = _panel(o, h, l, c)
    reason, net_R, gross_R = q10.simulate(p, entry_idx=0, direction=1,
                                          stop=99.0, target=105.0, max_hold=5,
                                          cost_bp=15.0)
    assert reason == "target"
    assert abs(gross_R - 5.0) < 1e-9         # +5 on R=1
    # cost = 100 * 15/1e4 = 0.15 price -> -0.15R
    assert abs(net_R - 4.85) < 1e-9


# ------------------------------------------------------------- clustered t
def test_day_clustered_t_matches_a_hand_computation():
    """The point estimate is the exact difference in means. The clustered SE
    needs between-day variation in the influence sums, so both arms appear on
    every day and the *magnitude* of the treated arm varies across days."""
    day = np.repeat(np.arange(8), 4)
    g = np.tile([1, 1, 0, 0], 8)                 # both arms present every day
    treated = np.array([2.0, 2.5, 1.5, 3.0, 2.2, 1.8, 2.7, 2.1])
    x = np.empty(32)
    for d in range(8):
        x[d * 4:d * 4 + 4] = [treated[d], treated[d], 1.0, 1.0]
    m, t, G = q10.cluster_t(x, g, day)
    assert G == 8
    assert abs(m - (treated.mean() - 1.0)) < 1e-9
    assert np.isfinite(t) and t > 0


def test_cluster_t_needs_two_arms():
    day = np.repeat(np.arange(4), 3)
    x = np.arange(12.0)
    g = np.ones(12)                          # only one arm
    m, t, G = q10.cluster_t(x, g, day)
    assert np.isnan(m) and np.isnan(t)
