"""Per-leg P&L attribution in the trade journal."""
from __future__ import annotations

import csv

from src.hyperliquid_bot.phase3 import journal


def _pos(**over):
    pos = {
        "pair": "RSR/TAO",
        "direction": "LONG_A_SHORT_B",
        "entry_time_iso": "2026-10-12T04:00:00+00:00",
        "exit_time_iso": "2026-10-12T14:00:00+00:00",
        "beta_entry": 0.5,
        "half_life_hours": 12.0,
        "entry_price_a": 0.001597,
        "entry_price_b": 284.68,
        "entry_spread": -0.02,
        "target_spread": 0.0,
        "stop_spread": -0.05,
        "notional_usd": 379.35,
        "leg_a_usd": 231.92,
        "leg_b_usd": 147.43,
        "p_revert_at_entry": 0.6,
        "exp_return_at_entry": 0.02,
    }
    pos.update(over)
    return pos


def test_compute_pnl_matches_leg_sum():
    pos = _pos()
    pnl_a, pnl_b, leg_a, leg_b = journal._leg_pnls(pos, 0.002274, 297.23)
    gross = pnl_a + pnl_b
    net, _ = journal.compute_pnl(pos, 0.002274, 297.23)
    cost = (leg_a + leg_b) * (0.00045 * 2 + 3.0 / 10000.0 * 2)
    assert abs((gross - cost) - net) < 1e-9


def test_rsr_trade_was_mostly_one_leg():
    """The motivating example: the +$95 win was ~93% the RSR leg."""
    pos = _pos()
    pnl_a, pnl_b, _, _ = journal._leg_pnls(pos, 0.002274, 297.23)
    assert pnl_a > 90.0          # long RSR leg
    assert abs(pnl_b) < 10.0     # short TAO leg barely moved
    assert pnl_a / (abs(pnl_a) + abs(pnl_b)) > 0.85


def test_short_direction_flips_signs():
    pos = _pos(direction="SHORT_A_LONG_B")
    pnl_a, pnl_b, _, _ = journal._leg_pnls(pos, 0.002274, 297.23)
    assert pnl_a < 0 and pnl_b > 0


def test_log_trade_writes_per_leg_columns(tmp_path, monkeypatch):
    f = tmp_path / "trades.csv"
    monkeypatch.setattr(journal, "TRADES_FILE", f)
    pos = _pos()
    pos["exit_spread"] = 0.0
    journal.log_trade(pos, 0.002274, 297.23)
    rows = list(csv.DictReader(open(f)))
    assert len(rows) == 1
    r = rows[0]
    assert r["pnl_a_usd"] and r["pnl_b_usd"]
    assert abs(float(r["pnl_a_usd"]) - 98.32) < 0.5
    assert float(r["leg_a_pct"]) > 0.3
