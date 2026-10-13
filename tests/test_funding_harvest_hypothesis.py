from __future__ import annotations

import sqlite3

import pytest

from scripts.funding_harvest_cycle import validate_assets
from scripts.funding_harvest_score import build_report
from src.hyperliquid_bot.funding.hypothesis import (
    closest_oracle,
    four_leg_pnl,
    funding_cashflow_short,
    init_db,
    persistently_positive,
    walk_book,
)


def test_walk_book_uses_multiple_levels_and_rejects_insufficient_depth():
    levels = [{"px": "100", "sz": "1"}, {"px": "102", "sz": "2"}]
    fill = walk_book(levels, 2)
    assert fill.average_price == pytest.approx(101)
    assert fill.notional == pytest.approx(202)
    assert fill.worst_price == 102
    assert walk_book(levels, 4) is None


def test_positive_funding_pays_short_and_four_leg_math_includes_all_costs():
    funding = funding_cashflow_short(2, 100, 0.001)
    assert funding == pytest.approx(0.20)
    pnl = four_leg_pnl(
        quantity=2,
        entry_spot_px=100,
        entry_perp_px=101,
        exit_spot_px=105,
        exit_perp_px=105.5,
        funding_usd=funding,
        fees_usd=0.30,
    )
    assert pnl == pytest.approx(0.90)


def test_signal_requires_enough_prints_and_every_print_positive():
    rows = [(hour, 0.0001) for hour in range(24)]
    assert persistently_positive(rows, 0, 23, 20)[0]
    rows[8] = (8, -0.00001)
    ok, reason = persistently_positive(rows, 0, 23, 20)
    assert not ok and "not positive" in reason
    assert not persistently_positive(rows[:10], 0, 23, 20)[0]


def test_oracle_must_be_near_the_funding_print():
    rows = [(1_000, 100.0), (10_000, 101.0)]
    assert closest_oracle(rows, 9_000, 2_000) == 101.0
    assert closest_oracle(rows, 20_000, 2_000) is None


def test_spot_context_is_resolved_by_coin_key_and_token_identity():
    cfg = {
        "assets": {
            "BTC": {
                "spot_pair": "@142",
                "spot_token": "UBTC",
                "token_id": "0xabc",
            }
        }
    }
    perps = {"BTC": ({"name": "BTC", "szDecimals": 5}, {"midPx": "100"})}
    spot_meta = {
        "tokens": [
            {"index": 0, "name": "USDC", "tokenId": "quote", "szDecimals": 8},
            {"index": 197, "name": "UBTC", "tokenId": "0xabc", "szDecimals": 5},
        ],
        "universe": [{"name": "@142", "tokens": [197, 0], "index": 142}],
    }
    # Include a stale unrelated context first. Validation must not zip by position.
    spots = {"@141": {"midPx": "1"}, "@142": {"midPx": "100"}}
    out = validate_assets(cfg, perps, spot_meta, spots)
    assert out["BTC"]["spot_ctx"]["midPx"] == "100"
    bad = {"@142": {"midPx": "100"}}
    spot_meta["tokens"][1]["tokenId"] = "lookalike"
    with pytest.raises(RuntimeError, match="identity changed"):
        validate_assets(cfg, perps, spot_meta, bad)


def test_decision_key_is_restart_idempotent(tmp_path):
    conn = init_db(tmp_path / "test.db")
    row = ("2026-10-09", "BTC", 1, 0, "x", "{}")
    conn.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)", row)
    conn.execute("INSERT OR IGNORE INTO decisions VALUES (?, ?, ?, ?, ?, ?)", row)
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1


def test_scorer_refuses_verdict_before_independent_date_minimum(tmp_path):
    conn = init_db(tmp_path / "test.db")
    cfg = {
        "hypothesis_id": "test",
        "inference": {"minimum_independent_entry_dates": 30},
    }
    report = build_report(conn, cfg)
    assert report["ready"] is False
    assert report["verdict"] == "not_ready"
    assert report["remaining_entry_dates"] == 30
