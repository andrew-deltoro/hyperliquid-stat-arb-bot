"""Exchange leverage (H4), gross exposure (H2), cluster/coin caps (M5),
negative beta (M9), quantity-based PnL (M4), dry-run sizing path."""
from __future__ import annotations

import pytest

from src.hyperliquid_bot.phase3 import journal
from src.hyperliquid_bot.phase3.correlation_guard import CorrelationGuard
from src.hyperliquid_bot.phase3.executor import DryRunExecutor
from src.hyperliquid_bot.phase3.positions import PositionManager
from src.hyperliquid_bot.phase3.scheduler import _size_for_book

from .fake_hl import FakeHL
from .test_order_lifecycle import make_cfg, make_executor, make_pm, make_signal, open_position


def test_leverage_set_before_entry_and_capped_at_asset_max(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.max_leverage["AAA"] = 2
    cfg = make_cfg()
    cfg.raw["leverage"] = {"exchange_leverage": 3}
    pm, _ = make_pm(hl, tmp_path, cfg=cfg)

    assert pm.try_open(make_signal(), 10.0, 20.0)["opened"]
    assert ("AAA", 2, True) in hl.leverage_calls
    assert ("BBB", 3, True) in hl.leverage_calls


def test_isolated_only_asset_uses_isolated_leverage(tmp_path):
    hl = FakeHL({"xyz:GOLD": 2000.0, "xyz:SILVER": 30.0})
    hl.only_isolated = {"xyz:GOLD", "xyz:SILVER"}
    cfg = make_cfg()
    cfg.raw["leverage"] = {"isolated_leverage": 2}
    ex = make_executor(hl, cfg, tmp_path=tmp_path)
    assert ex.ensure_leverage("xyz:GOLD")
    assert hl.leverage_calls == [("xyz:GOLD", 2, False)]


def test_leverage_failure_blocks_entry(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.leverage_error = True
    pm, _ = make_pm(hl, tmp_path)

    res = pm.try_open(make_signal(), 10.0, 20.0)
    assert not res["opened"] and "leverage" in res["reason"]
    assert hl.orders == []


def test_maintenance_frac_from_max_leverage(tmp_path):
    hl = FakeHL({"AAA": 10.0})
    hl.max_leverage["AAA"] = 5
    ex = make_executor(hl, tmp_path=tmp_path)
    assert ex.maintenance_frac("AAA") == pytest.approx(0.1)


def test_xyz_positions_count_toward_exposure(tmp_path):
    hl = FakeHL({"AAA": 10.0, "xyz:TSM": 200.0})
    hl.positions = {"AAA": 10.0, "xyz:TSM": -2.0}
    ex = make_executor(hl, tmp_path=tmp_path)
    assert ex.account_gross() == pytest.approx(100.0 + 400.0)
    assert ex.account_equity() == {"": 1000.0, "xyz": 500.0}


def test_total_open_notional_is_gross_legs(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    pm, _ = make_pm(hl, tmp_path)
    open_position(pm, leg_a_usd=100.0, leg_b_usd=150.0)
    assert pm.total_open_notional() == pytest.approx(250.0)


def test_shared_coin_is_capped_across_pairs():
    guard = CorrelationGuard(cluster_caps={"defi": 10_000}, max_coin_gross=300.0)
    book = [
        {"status": "open", "coin_a": "SNX", "coin_b": "DYDX", "leg_a_usd": 150, "leg_b_usd": 150},
        {"status": "open", "coin_a": "SNX", "coin_b": "SUI", "leg_a_usd": 100, "leg_b_usd": 100},
    ]
    res = guard.check("SNX", "TIA", 200.0, book)
    assert not res.allowed and "SNX" in res.reason


def test_cluster_counts_each_leg_and_covers_xyz():
    guard = CorrelationGuard(cluster_caps={"xyz_semis": 500})
    book = [{"status": "open", "coin_a": "xyz:MU", "coin_b": "xyz:TSM",
             "leg_a_usd": 150, "leg_b_usd": 150}]
    assert not guard.check("xyz:AMD", "xyz:TSM", 300.0, book).allowed
    assert guard.check("xyz:AMD", "xyz:TSM", 100.0, book).allowed


def test_negative_beta_is_rejected(tmp_path):
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    pm, _ = make_pm(hl, tmp_path)
    res = pm.try_open(make_signal(beta=-0.3), 10.0, 20.0)
    assert not res["opened"] and "beta" in res["reason"]
    assert hl.orders == []


def test_beta_drift_with_no_economic_damage_holds(tmp_path):
    # Finding 3.2: a large posterior beta drift alone is no longer an exit.
    # Here |Δβ|/sd(β) = 5σ, but the spread still has a positive expected
    # return under the real exit policy, so the position is held.
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.positions = {"AAA": 10.0, "BBB": -5.0}
    pm, _ = make_pm(hl, tmp_path)
    open_position(pm, size_a=10.0, size_b=-5.0, beta_sd_entry=0.01,
                  entry_alpha=0.0, entry_mean_raw=0.0)
    import math
    pa, pb = 10.0, 20.0
    pm.state.positions[0]["entry_mean_raw"] = math.log(pa) - math.log(pb)
    pm.state.positions[0]["entry_spread"] = -0.02

    held = pm.evaluate_open("AAA/BBB", pa, pb, fresh_beta=1.25,
                            fresh_beta_sd=0.05, fresh_exp_return=+0.02)
    assert held["action"] == "hold"

    # Same 5σ drift, but the spread no longer pays -> exit.
    shifted = pm.evaluate_open("AAA/BBB", pa, pb, fresh_beta=1.25,
                               fresh_beta_sd=0.05, fresh_exp_return=-0.01)
    assert shifted["action"] == "closed" and shifted["reason"] == "beta_shift"


def test_beta_drift_disabled_without_stored_sd(tmp_path):
    # Finding 3.1: with no posterior sd there is no statistical scale, so the
    # drift gate must not fire even when the expected return is negative.
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.positions = {"AAA": 10.0, "BBB": -5.0}
    pm, _ = make_pm(hl, tmp_path)
    open_position(pm, size_a=10.0, size_b=-5.0, beta_sd_entry=0.0,
                  entry_alpha=0.0, entry_mean_raw=0.0)
    import math
    pa, pb = 10.0, 20.0
    pm.state.positions[0]["entry_mean_raw"] = math.log(pa) - math.log(pb)
    pm.state.positions[0]["entry_spread"] = -0.02

    res = pm.evaluate_open("AAA/BBB", pa, pb, fresh_beta=1.60,
                           fresh_beta_sd=0.0, fresh_exp_return=-0.01)
    assert res["action"] == "hold"


def test_beta_sd_backfilled_on_first_sight(tmp_path):
    # Finding 3.1: a pre-correction position (no stored sd) is made safe by
    # persisting the fresh posterior sd on first sight.
    hl = FakeHL({"AAA": 10.0, "BBB": 20.0})
    hl.positions = {"AAA": 10.0, "BBB": -5.0}
    pm, _ = make_pm(hl, tmp_path)
    open_position(pm, size_a=10.0, size_b=-5.0, beta_sd_entry=0.0,
                  entry_alpha=0.0, entry_mean_raw=0.0)
    import math
    pa, pb = 10.0, 20.0
    pm.state.positions[0]["entry_mean_raw"] = math.log(pa) - math.log(pb)
    pm.state.positions[0]["entry_spread"] = -0.02

    pm.evaluate_open("AAA/BBB", pa, pb, fresh_beta=1.05,
                     fresh_beta_sd=0.03, fresh_exp_return=+0.01)
    assert pm.state.positions[0]["beta_sd_entry"] == pytest.approx(0.03)


def test_pnl_uses_filled_quantities():
    pos = {"direction": "LONG_A_SHORT_B", "entry_price_a": 10.0, "entry_price_b": 20.0,
           "entry_size_a": 10.0, "entry_size_b": -5.0,
           "leg_a_usd": 999.0, "leg_b_usd": 999.0, "notional_usd": 200.0}
    pnl, _ = journal.compute_pnl(pos, 11.0, 20.0, fee_pct=0.0, slippage_bps=0.0)
    assert pnl == pytest.approx(10.0)


def test_journal_migrates_header_when_columns_change(isolated_files):
    path = journal.TRADES_FILE
    path.write_text("pair,pnl_pct\nAAA/BBB,0.01\n")
    journal._ensure_file(path, journal.TRADES_COLUMNS)
    header = path.read_text().splitlines()[0].split(",")
    assert header == journal.TRADES_COLUMNS
    assert "AAA/BBB" in path.read_text()


def test_dry_run_kelly_sizing_respects_safeguard_cap(tmp_path):
    cfg = make_cfg()
    cfg.raw["executor"]["mode"] = "dry_run"
    cfg.position["dry_run_balance"] = 10_000.0
    cfg.raw["safeguards"]["max_notional_per_order"] = 100.0
    cfg.raw["leverage"] = {"mode": "kelly", "max_trade_notional_frac": 0.5,
                           "kelly_fraction": 1.0, "assumed_pair_correlation": 0.0}
    ex = DryRunExecutor(cfg, log_fn=lambda m: None)
    pm = PositionManager(cfg, executor=ex, log_fn=lambda m: None)

    import numpy as np
    sig = make_signal()
    sig.trade_returns = np.random.default_rng(0).normal(0.01, 0.02, 5000)
    sig.exp_return = float(sig.trade_returns.mean())
    sig.stress_loss_frac = 0.04

    notional, why = _size_for_book(sig, pm, ex, ex.account_equity(), 0.0, 1.0, cfg)
    assert notional == pytest.approx(0.98 * 200.0)
    assert "safeguard_per_order" in why
