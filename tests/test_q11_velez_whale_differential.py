"""Unit tests for the Q11 Velez "whale" differential harness.

These are computation tests on synthetic bars only. They assert no trading
behaviour and make no network calls. The Q11 harness lives under
``scripts/experiments`` and is imported here by path, the same way the Q9/Q10
tests import their frozen harnesses.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
Q11_PATH = ROOT / "scripts" / "experiments" / "q11_velez_whale_differential.py"


def _load_q11():
    import sys
    spec = importlib.util.spec_from_file_location("q11_harness", Q11_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["q11_harness"] = mod          # dataclasses resolve __module__ here
    spec.loader.exec_module(mod)
    return mod


q11 = _load_q11()


def _panel(o, h, l, c, day=None, v=None):
    o = np.asarray(o, float); h = np.asarray(h, float)
    l = np.asarray(l, float); c = np.asarray(c, float)
    n = len(c)
    return q11.Panel(symbol="TEST", t=np.arange(n, dtype=np.int64) * 300_000,
                     o=o, h=h, l=l, c=c, v=(np.full(n, 1.0) if v is None else v),
                     qv=c, day=(np.zeros(n, dtype=np.int64) if day is None else day),
                     atr=np.ones(n), fast=np.full(n, 1e9), slow=np.full(n, 1e9))


# --------------------------------------------------------------- detection
def test_power_run_requires_red_move_and_a_low_close():
    """A 1-bar red bar that moves >= 1.5*ATR closes in the bottom quarter of its
    own range -> detected; the same bar with a close at the TOP of its range is
    not (the close-location quality test)."""
    pad = np.full(6, 100.0)
    tail = np.full(4, 100.0)
    o = np.concatenate([pad, [99.0], tail])
    h = np.concatenate([pad, [99.0], tail])
    l = np.concatenate([pad, [95.0], tail])
    c = np.concatenate([pad, [95.0], tail])      # red, 4.0 move, close at the low
    p = _panel(o, h, l, c)
    assert q11.candidate_power(p, q11.LOCKED)[6] == 1

    c2 = np.concatenate([pad, [99.0], tail])     # red but close at the HIGH
    h2 = np.concatenate([pad, [99.0], tail])
    l2 = np.concatenate([pad, [95.0], tail])
    p2 = _panel(o, h2, l2, c2)
    assert q11.candidate_power(p2, q11.LOCKED)[6] == -1


def test_power_run_rejects_a_green_run():
    pad = np.full(6, 100.0)
    tail = np.full(4, 100.0)
    o = np.concatenate([pad, [96.0], tail])
    h = np.concatenate([pad, [100.0], tail])
    l = np.concatenate([pad, [96.0], tail])
    c = np.concatenate([pad, [100.0], tail])     # green
    p = _panel(o, h, l, c)
    assert q11.candidate_power(p, q11.LOCKED)[6] == -1


def test_rolling_red_median_matches_the_scalar_reference():
    """The vectorised prior-day red-range median must equal the scalar
    `_prior_red_median` at EVERY finite index (the funnel depends on it), not
    just a handful -- including even red counts, where the median averages two
    central values."""
    rng = np.random.default_rng(11)
    n = 1200
    c = 100 + np.cumsum(rng.normal(0, 0.5, n))
    o = c - rng.normal(0, 0.4, n)
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.3, n))
    l = np.minimum(o, c) - np.abs(rng.normal(0, 0.3, n))
    p = _panel(o, h, l, c)
    fast = q11.rolling_red_median(p, q11.LOCKED)
    checked = 0
    for i in (300, 500, 777, 1199):
        slow = q11._prior_red_median(p, i, q11.LOCKED)
        if np.isfinite(slow):
            assert abs(float(fast[i]) - slow) < 1e-9, (i, fast[i], slow)
            checked += 1
        else:
            assert not np.isfinite(fast[i]), i
    assert checked >= 3


def test_enough_red_reference_is_the_prior_day_only():
    """The reference is the median range of the RED bars strictly BEFORE the run
    (a look-ahead-free window), and it uses red bars only."""
    n = 400
    o = np.full(n, 100.0)
    c = np.full(n, 99.5)                         # every bar is red
    h = np.full(n, 100.0)
    l = np.full(n, 99.0)                         # range 1.0
    p = _panel(o, h, l, c)
    med = q11._prior_red_median(p, 300, q11.LOCKED)
    assert abs(med - 1.0) < 1e-9
    # too early: not a full reference window yet
    assert not np.isfinite(q11._prior_red_median(p, 5, q11.LOCKED))


# --------------------------------------------------------------- zone rule
def test_zone_one_is_at_or_below_the_fast_ma():
    p = _panel([100.0], [100.0], [100.0], [100.0])
    p.fast[:] = 100.0
    assert q11.zone_class(p, 0, q11.LOCKED) == 1          # close == fast -> Zone 1
    c2 = _panel([100.0], [100.0], [100.0], [101.0])
    c2.fast[:] = 100.0
    assert q11.zone_class(c2, 0, q11.LOCKED) == 2          # above  -> Zone 2


def test_zone_slope_variant_exempts_a_rising_ma():
    """`pos_slope` honours his 'plus the slope' clause: at/below a RISING MA is
    not counted as the zone he refuses."""
    prm = q11.replace_locked(zone_mode="pos_slope", slope_lb=1)
    p = _panel([100.0, 100.0], [100.0, 100.0], [100.0, 100.0], [100.0, 100.0])
    p.fast[:] = [99.0, 100.0]                              # rising
    assert q11.zone_class(p, 1, prm) == 2
    p2 = _panel([100.0, 100.0], [100.0, 100.0], [100.0, 100.0], [100.0, 100.0])
    p2.fast[:] = [101.0, 100.0]                            # falling
    assert q11.zone_class(p2, 1, prm) == 1


# --------------------------------------------------------- stop and target
def test_stop_is_high_of_move_plus_buffer_and_target_is_the_grid():
    # the entry sits 0.6 below a 0.5-ATR stop, so the cap is not what is tested
    ev = q11.Event(symbol="TEST", day=0, run_start=1, run_end=2, run_high=100.0,
                   run_low=95.0, atr_at=1.0, zone=2, z3=False)
    o = np.array([100.0, 99.0, 96.0, 96.0, 96.0])
    h = np.array([101.0, 100.0, 96.0, 96.0, 96.0])
    l = np.array([99.0, 95.0, 95.0, 95.0, 95.0])
    c = np.array([100.0, 96.0, 96.0, 96.0, 96.0])
    p = _panel(o, h, l, c)
    st = q11._stop_and_target(p, ev, entry_bar=2, entry=99.0, prm=q11.LOCKED)
    assert st is not None
    stop, target = st
    # the move includes the bar BEFORE the run (Q9's base-candle convention),
    # whose high is 101 -> stop 101 + 0.5*ATR
    assert abs(stop - 101.5) < 1e-9
    assert abs(target - 98.0) < 1e-9           # 99.0 is on a level -> next one down


def test_target_grid_rolls_to_the_next_step_on_an_exact_level():
    """An entry exactly on a grid level must target the NEXT level down, not
    itself -- 100.0 -> 99.0, never 100.0."""
    ev = q11.Event(symbol="TEST", day=0, run_start=1, run_end=2, run_high=100.0,
                   run_low=95.0, atr_at=1.0, zone=2, z3=False)
    o = np.array([100.0, 99.0, 96.0, 96.0, 96.0])
    h = np.array([101.0, 100.0, 96.0, 96.0, 96.0])
    l = np.array([99.0, 95.0, 95.0, 95.0, 95.0])
    c = np.array([100.0, 96.0, 96.0, 96.0, 96.0])
    p = _panel(o, h, l, c)
    # entry 99.0 sits exactly on the 1.0 grid
    st = q11._stop_and_target(p, ev, entry_bar=2, entry=99.0, prm=q11.LOCKED)
    assert st is not None and abs(st[1] - 98.0) < 1e-9


def test_max_stop_cap_drops_the_wide_stop():
    ev = q11.Event(symbol="TEST", day=0, run_start=1, run_end=2, run_high=100.0,
                   run_low=80.0, atr_at=1.0, zone=2, z3=False)
    o = np.array([100.0, 99.0, 81.0, 81.0])
    h = np.array([101.0, 100.0, 100.0, 81.0])
    l = np.array([99.0, 80.0, 80.0, 80.0])
    c = np.array([100.0, 81.0, 81.0, 81.0])
    p = _panel(o, h, l, c)
    # stop ~19.5 away on ATR 1 -> far over the 3xATR cap
    assert q11._stop_and_target(p, ev, 3, 81.0, q11.LOCKED) is None
    wide = q11.replace_locked(max_stop_atr=0.0)
    assert q11._stop_and_target(p, ev, 3, 81.0, wide) is not None


# --------------------------------------------------------------- pessimistic
def test_stop_wins_when_one_bar_hits_both_stop_and_target():
    """The ifvg convention: an ambiguous bar resolves to the STOP. On a SHORT,
    the stop is above and the target below."""
    o = np.array([100.0, 100.0])
    h = np.array([100.0, 103.0])
    l = np.array([100.0, 97.0])
    c = np.array([100.0, 100.0])
    p = _panel(o, h, l, c)
    arm = q11.simulate(p, 0, 100.0, stop=102.0, target=98.0, max_hold=5,
                       cost_bp=0.0, h_bp=2)
    assert arm.reason == "stop"
    assert arm.gross_R < 0


def test_intrabar_entry_bar_gets_no_target_credit():
    """On the BOUNCE arm the position did not exist for the whole entry bar, so
    a target touch on that same bar must NOT be credited -- the ambiguous bar
    resolves the worse way. The control arm, which enters at the open, is not
    intrabar and does get the target."""
    o = np.array([100.0, 100.0])
    h = np.array([100.0, 100.0])
    l = np.array([90.0, 90.0])                   # touches the target on the ENTRY bar
    c = np.array([95.0, 95.0])
    p = _panel(o, h, l, c)
    bounce = q11.simulate(p, 0, 100.0, stop=200.0, target=95.0, max_hold=1,
                          cost_bp=0.0, h_bp=2, intrabar=True)
    assert bounce.reason == "time"               # target NOT taken on the entry bar
    control = q11.simulate(p, 0, 100.0, stop=200.0, target=95.0, max_hold=1,
                           cost_bp=0.0, h_bp=2, intrabar=False)
    assert control.reason == "target"


def test_short_net_R_subtracts_cost_and_target_credits_correctly():
    o = np.array([100.0, 100.0])
    h = np.array([100.0, 100.0])
    l = np.array([100.0, 94.0])
    c = np.array([100.0, 95.0])
    p = _panel(o, h, l, c)
    # entry 100, stop 101 (R=1), target 95 -> +5 gross, minus 15bp -> +4.85
    arm = q11.simulate(p, 0, 100.0, stop=101.0, target=95.0, max_hold=5,
                       cost_bp=15.0, h_bp=2)
    assert arm.reason == "target"
    assert abs(arm.gross_R - 5.0) < 1e-9
    assert abs(arm.net_R - 4.85) < 1e-9


def test_bp_common_uses_one_shared_terminal_bar():
    """Both arms' `bp_common` must read the SAME bar, so only the entry price
    differs."""
    o = np.array([100.0, 100.0, 100.0, 100.0])
    h = np.array([100.0, 100.0, 100.0, 100.0])
    l = np.array([100.0, 100.0, 100.0, 100.0])
    c = np.array([100.0, 100.0, 90.0, 90.0])
    p = _panel(o, h, l, c)
    a = q11.simulate(p, 0, 100.0, stop=200.0, target=1.0, max_hold=2, cost_bp=0.0,
                     h_bp=10, common_bar=2, run_end=0)
    assert abs(a.bp_common - 1000.0) < 1e-6     # 100 -> 90 = +1000 bp for a short


# --------------------------------------------------------------- clustered t
def test_day_clustered_t_matches_a_hand_computation():
    day = np.repeat(np.arange(8), 4)
    g = np.tile([1, 1, 0, 0], 8)                 # both arms present every day
    treated = np.array([2.0, 2.5, 1.5, 3.0, 2.2, 1.8, 2.7, 2.1])
    x = np.empty(32)
    for d in range(8):
        x[d * 4:d * 4 + 4] = [treated[d], treated[d], 1.0, 1.0]
    m, t, G = q11.cluster_t(x, g, day)
    assert G == 8
    assert abs(m - (treated.mean() - 1.0)) < 1e-9
    assert np.isfinite(t) and t > 0


def test_cluster_mean_t_recovers_the_mean_and_needs_two_clusters():
    x = np.array([1.0, 2.0, 3.0, 4.0])
    day = np.array([0, 0, 1, 1])
    m, t, G = q11.cluster_mean_t(x, day)
    assert abs(m - 2.5) < 1e-9 and G == 2 and np.isfinite(t)
    m1, t1, G1 = q11.cluster_mean_t(x, np.zeros(4, dtype=int))
    assert abs(m1 - 2.5) < 1e-9 and np.isnan(t1) and G1 == 1


# --------------------------------------------------------------- recovery
def test_recovery_uses_the_run_high_and_the_prior_high():
    ev = q11.Event(symbol="TEST", day=0, run_start=2, run_end=3, run_high=100.0,
                   run_low=90.0, atr_at=1.0, zone=2, z3=False, bounce_bar=4)
    # bars 0-1 sit at 110 (the prior high), the run drops 100 -> 90, then a
    # bounce to 95 and finally a NEW LOW at 89.
    o = np.array([110.0, 110.0, 100.0, 92.0, 91.0, 89.0, 89.0])
    h = np.array([110.0, 110.0, 100.0, 92.0, 95.0, 89.0, 89.0])
    l = np.array([110.0, 110.0, 96.0, 90.0, 90.0, 89.0, 89.0])
    c = np.array([110.0, 110.0, 96.0, 90.0, 95.0, 89.0, 89.0])
    p = _panel(o, h, l, c)
    q11.measure_recovery(p, ev, q11.LOCKED)
    assert abs(ev.rec_frac - 0.5) < 1e-9        # max high 95, drop 100->90
    assert abs(ev.rec_pre - (95.0 - 90.0) / (110.0 - 90.0)) < 1e-9
    assert ev.made_new_low == 1
    assert ev.reached_new_low == 0              # the new low lands AFTER the bounce


# --------------------------------------------------------------- bounce entry
def test_bounce_entry_fills_at_the_trigger_not_the_bar_open():
    """The conservative entry is a sell-stop at bounce_low - tol*ATR. When the
    next bar gaps down through the trigger, the fill is `min(trigger, open)` --
    never better than the trigger for a short -- and the realized entry price
    must be exactly that, not the bar's open."""
    prm = q11.LOCKED
    # ATR 10 -> trigger tolerance 0.5. The green bounce bar (index 3) has low
    # 95.5 -> trigger 95.0. Bar 4 opens at 92, below the trigger, so the
    # pessimistic fill is the OPEN 92.0 (worse for a short), not the trigger.
    o = np.array([100.0, 99.0, 96.0, 96.0, 92.0, 92.0])
    h = np.array([101.0, 100.0, 97.0, 97.0, 93.0, 92.0])
    l = np.array([99.0, 95.0, 94.0, 95.5, 90.0, 90.0])
    c = np.array([100.0, 96.0, 95.0, 96.5, 91.0, 91.0])
    p = _panel(o, h, l, c)
    ev = q11.Event(symbol="TEST", day=0, run_start=1, run_end=3, run_high=101.0,
                   run_low=94.0, atr_at=10.0, zone=2, z3=False)
    q11.attach_bounce(p, ev, prm)
    assert ev.bo is not None
    assert abs(ev.bo.entry - 92.0) < 1e-9       # the worse of trigger/open
    assert ev.bo.entry_idx == 4
