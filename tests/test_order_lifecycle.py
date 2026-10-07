"""Order-lifecycle tests against an in-memory fake exchange (no network)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.hyperliquid_bot.phase3 import executor as executor_mod
from src.hyperliquid_bot.phase3.config import Phase3Config as Config
from src.hyperliquid_bot.phase3.executor import LiveExecutor, Order
from src.hyperliquid_bot.phase3.positions import PositionManager
from src.hyperliquid_bot.phase3.signals import Signal
from src.hyperliquid_bot.phase3.state import load_state
from src.hyperliquid_bot.phase3.testnet_safeguards import Safeguards

from .fake_hl import FakeHL


def make_cfg(prefer_maker=False, max_daily=100):
    raw = {
        "executor": {"mode": "live", "network": "testnet",
                     "prefer_maker": prefer_maker, "maker_timeout_sec": 5},
        "safeguards": {"max_notional_per_order": 1000,
                       "max_total_exposure_usd": 10000,
                       "max_daily_orders": max_daily},
    }
    return Config(
        pairs=[], data={}, model={}, signal={},
        position={"target_frac": 0.0, "stop_mult": 2.0,
                  "exit_horizon_mult": 3.0, "exit_mode": "cross",
                  "cross_exit_frac": 0.75},
        risk={"max_concurrent_positions": 5, "max_total_notional": 10000},
        ou_prior={}, raw=raw,
    )


def make_executor(hl, cfg=None, kill_path=None, tmp_path=None):
    ex = LiveExecutor(cfg or make_cfg(), log_fn=lambda m: None,
                      exchange=hl, info=hl, wallet_address="0xtest")
    ex.safeguards.kill_switch_path = kill_path or (tmp_path / "KILL")
    return ex


def make_pm(hl, tmp_path, cfg=None):
    cfg = cfg or make_cfg()
    ex = make_executor(hl, cfg, tmp_path=tmp_path)
    return PositionManager(cfg, executor=ex, log_fn=lambda m: None), ex


def make_signal(a="AAA", b="BBB", notional=200.0, beta=1.0,
                direction="LONG_A_SHORT_B"):
    return Signal(
        pair=f"{a}/{b}", coin_a=a, coin_b=b, beta=beta, beta_sd=0.05,
        nu_coint=5.0, converged_coint=True, alpha=0.0, mean_raw=0.0,
        sigma_raw=0.01, current_spread=-0.02, spread_sd=0.01, z=-2.0,
        kappa=0.1, half_life_hours=10.0, sigma_ou=0.01, nu_ou=5.0,
        converged_ou=True, p_revert=0.8, edge=0.1, notional=notional,
        direction=direction, action="OPEN",
    )


def open_position(pm, a="AAA", b="BBB", **extra):
    """Seed state with an open position (as if opened earlier)."""
    pos = {
        "pair": f"{a}/{b}", "coin_a": a, "coin_b": b,
        "direction": "LONG_A_SHORT_B", "beta_entry": 1.0, "entry_alpha": 0.0,
        "entry_mean_raw": 0.0, "entry_sigma_raw": 0.01, "entry_spread": -0.02,
        "entry_price_a": 10.0, "entry_price_b": 20.0,
        "entry_time_iso": datetime.now(timezone.utc).isoformat(),
        "half_life_hours": 10.0, "target_spread": 0.0, "stop_spread": -0.04,
        "time_stop_iso": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "notional_usd": 200.0, "leg_a_usd": 100.0, "leg_b_usd": 100.0,
        "status": "open",
    }
    pos.update(extra)
    pm.state.positions.append(pos)
    return pos


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(executor_mod.time, "sleep", lambda s: None)


# ---------------- C1: close sizes come from entry quantities ----------------
def test_close_uses_entry_quantity_not_exit_price(tmp_path):
    hl = FakeHL({"AAA": 20.0, "BBB": 40.0})   # both prices doubled since entry
    hl.positions = {"AAA": 10.0, "BBB": -5.0}
    pm, _ = make_pm(hl, tmp_path)
    pos = open_position(pm, size_a=10.0, size_b=-5.0)

    res = pm._close(pos, 0.0, None, "cross", exit_price_a=20.0, exit_price_b=40.0)

    assert res["action"] == "closed"
    assert hl.positions == {}
    assert all(o["reduce_only"] for o in hl.orders)


def test_close_legacy_position_without_stored_sizes(tmp_path):
    hl = FakeHL({"AAA": 5.0, "BBB": 30.0}, sz_decimals={"AAA": 1, "BBB": 1})
    hl.positions = {"AAA": 10.0, "BBB": -5.0}   # 100/10, 100/20
    pm, _ = make_pm(hl, tmp_path)
    pos = open_position(pm)

    res = pm._close(pos, 0.0, None, "time_stop", exit_price_a=5.0, exit_price_b=30.0)

    assert res["action"] == "closed"
    assert hl.positions == {}


def test_close_never_exceeds_live_position(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.positions = {"AAA": 9.0, "BBB": -5.0}    # exchange holds less than recorded
    pm, _ = make_pm(hl, tmp_path)
    pos = open_position(pm, size_a=10.0, size_b=-5.0)

    pm._close(pos, 0.0, None, "cross", exit_price_a=10.0, exit_price_b=20.0)

    assert hl.ioc_orders("AAA")[0]["sz"] == pytest.approx(9.0)
    assert "AAA" not in hl.positions


# ---------------- C2: unverified closes keep the position open ----------------
def test_failed_close_leg_keeps_position_open_and_retries(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.positions = {"AAA": 10.0, "BBB": -5.0}
    hl.ioc_fill_frac["BBB"] = 0.0
    pm, _ = make_pm(hl, tmp_path)
    pos = open_position(pm, size_a=10.0, size_b=-5.0)

    res = pm._close(pos, 0.0, None, "cross", exit_price_a=10.0, exit_price_b=20.0)

    assert res["action"] == "close_failed"
    assert pos["status"] == "open"
    assert pos["close_pending"] == "cross"
    assert pos["size_a"] == 0.0 and pos["size_b"] == pytest.approx(-5.0)
    assert load_state().positions[0]["close_pending"] == "cross"

    hl.ioc_fill_frac["BBB"] = 1.0
    res = pm.evaluate_open("AAA/BBB", 10.0, 20.0)
    assert res["action"] == "closed"
    assert res["reason"] == "cross"
    assert hl.positions == {}
    assert len(hl.ioc_orders("AAA")) == 1   # leg A not re-sent


def test_close_unknown_position_does_not_mark_closed(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.positions = {"AAA": 10.0, "BBB": -5.0}
    pm, _ = make_pm(hl, tmp_path)
    pos = open_position(pm, size_a=10.0, size_b=-5.0)
    hl.user_state_error = True

    res = pm._close(pos, 0.0, None, "cross", exit_price_a=10.0, exit_price_b=20.0)

    assert res["action"] == "close_failed"
    assert pos["status"] == "open"
    assert hl.orders == []


def test_close_with_opposite_live_position_flags_review(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.positions = {"AAA": -3.0, "BBB": -5.0}
    pm, _ = make_pm(hl, tmp_path)
    pos = open_position(pm, size_a=10.0, size_b=-5.0)

    res = pm._close(pos, 0.0, None, "cross", exit_price_a=10.0, exit_price_b=20.0)

    assert res["action"] == "close_failed"
    assert pos["status"] == "needs_review"
    assert hl.positions["AAA"] == -3.0


# ---------------- H1: safeguards never block reduce-only ----------------
def test_kill_switch_and_daily_limit_allow_reduce_only(tmp_path):
    kill = tmp_path / "KILL"
    kill.touch()
    sg = Safeguards(max_daily_orders=0, kill_switch_path=kill)
    assert sg.check_order(1e9, 1e9, reduce_only=True).allowed
    assert not sg.check_order(1.0, 0.0).allowed


def test_unknown_exposure_rejects_opening_orders(tmp_path):
    sg = Safeguards(kill_switch_path=tmp_path / "KILL")
    assert not sg.check_order(1.0, None).allowed


def test_close_succeeds_with_kill_switch_active(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.positions = {"AAA": 10.0, "BBB": -5.0}
    pm, ex = make_pm(hl, tmp_path, cfg=make_cfg(max_daily=0))
    (tmp_path / "KILL").touch()
    pos = open_position(pm, size_a=10.0, size_b=-5.0)

    res = pm._close(pos, 0.0, None, "cross", exit_price_a=10.0, exit_price_b=20.0)
    assert res["action"] == "closed"
    assert hl.positions == {}


# ---------------- C3: maker fallback only sends the remainder ----------------
def test_partial_maker_fill_sends_only_remainder(tmp_path):
    hl = FakeHL({"AAA": 10.0})
    hl.maker_fill_frac = 0.4
    ex = make_executor(hl, make_cfg(prefer_maker=True), tmp_path=tmp_path)

    fill = ex.place_order(Order("AAA", "buy", 10.0, 100.0, "arb_open"), ref_price=10.0)

    assert hl.positions["AAA"] == pytest.approx(10.0)
    assert hl.ioc_orders("AAA")[0]["sz"] == pytest.approx(6.0)
    assert fill.status == "filled"
    assert fill.size == pytest.approx(10.0)


def test_unknown_maker_outcome_sends_no_taker(tmp_path):
    hl = FakeHL({"AAA": 10.0})
    hl.maker_fill_frac = 0.4
    hl.user_fills_error = True
    ex = make_executor(hl, make_cfg(prefer_maker=True), tmp_path=tmp_path)

    fill = ex.place_order(Order("AAA", "buy", 10.0, 100.0, "arb_open"), ref_price=10.0)

    assert fill.status == "unknown"
    assert hl.ioc_orders() == []


# ---------------- C4: opens use measured quantities ----------------
def test_open_records_measured_sizes_and_fill_prices(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    pm, _ = make_pm(hl, tmp_path)

    res = pm.try_open(make_signal(), 10.0, 20.0)

    assert res["opened"]
    pos = res["position"]
    assert pos["size_a"] == pytest.approx(10.0)
    assert pos["size_b"] == pytest.approx(-5.0)
    assert hl.positions == {"AAA": 10.0, "BBB": -5.0}


def test_partial_leg_a_is_unwound_exactly(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.ioc_fill_frac["AAA"] = 0.5
    hl.reduce_fill_frac["AAA"] = 1.0
    pm, _ = make_pm(hl, tmp_path)

    res = pm.try_open(make_signal(), 10.0, 20.0)

    assert not res["opened"]
    assert hl.positions == {}
    unwind = hl.ioc_orders("AAA")[1]
    assert unwind["reduce_only"] and unwind["sz"] == pytest.approx(5.0)
    assert pm.state.positions == []


def test_unwind_that_cannot_complete_flags_review(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.ioc_fill_frac["AAA"] = 0.5
    hl.reduce_fill_frac["AAA"] = 0.0
    pm, _ = make_pm(hl, tmp_path)

    res = pm.try_open(make_signal(), 10.0, 20.0)

    assert not res["opened"]
    assert pm.state.positions[0]["status"] == "needs_review"
    assert pm.open_count() == 1
    assert not pm.try_open(make_signal(), 10.0, 20.0)["opened"]


def test_leg_b_failure_rolls_back_leg_a(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.ioc_fill_frac["BBB"] = 0.3
    hl.reduce_fill_frac["BBB"] = 1.0
    pm, _ = make_pm(hl, tmp_path)

    res = pm.try_open(make_signal(), 10.0, 20.0)

    assert not res["opened"]
    assert hl.positions == {}
    assert pm.state.positions == []


def test_open_blocked_when_position_query_fails(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.user_state_error = True
    pm, _ = make_pm(hl, tmp_path)

    res = pm.try_open(make_signal(), 10.0, 20.0)

    assert not res["opened"]
    assert hl.orders == []


def test_needs_review_blocks_entries_on_same_coin(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0, "CCC": 5.0})
    pm, _ = make_pm(hl, tmp_path)
    open_position(pm, a="AAA", b="CCC", status="needs_review")

    res = pm.try_open(make_signal(), 10.0, 20.0)

    assert not res["opened"]
    assert "needs_review" in res["reason"]
    assert hl.orders == []


# ---------------- C5: reconciliation fails closed ----------------
def test_reconcile_fails_closed_on_query_error(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    pm, _ = make_pm(hl, tmp_path)
    open_position(pm, size_a=10.0, size_b=-5.0)
    hl.user_state_error = True

    assert pm.reconcile() is False
    assert pm.state.positions[0]["status"] == "open"


def test_reconcile_marks_closed_or_review(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0, "CCC": 1.0, "DDD": 2.0})
    hl.positions = {"CCC": 50.0}
    pm, _ = make_pm(hl, tmp_path)
    gone = open_position(pm, size_a=10.0, size_b=-5.0)
    half = open_position(pm, a="CCC", b="DDD", size_a=50.0, size_b=-25.0)

    assert pm.reconcile() is True
    assert gone["status"] == "closed"
    assert half["status"] == "needs_review"


# ---------------- H3: sizing precision ----------------
def test_size_rounds_down_with_xyz_precision(tmp_path):
    hl = FakeHL({"xyz:GOLD": 2000.0}, sz_decimals={"xyz:GOLD": 2})
    ex = make_executor(hl, tmp_path=tmp_path)

    fill = ex.place_order(Order("xyz:GOLD", "buy", 0.0279, 55.8, "t"), ref_price=2000.0)

    assert hl.ioc_orders()[0]["sz"] == pytest.approx(0.02)
    assert fill.size == pytest.approx(0.02)
    assert ex.get_position("xyz:GOLD") == pytest.approx(0.02)


def test_unknown_asset_is_rejected_before_order(tmp_path):
    hl = FakeHL({"AAA": 10.0})
    ex = make_executor(hl, tmp_path=tmp_path)

    fill = ex.place_order(Order("ZZZ", "buy", 1.0, 10.0, "t"), ref_price=10.0)

    assert fill.status == "error"
    assert hl.orders == []
