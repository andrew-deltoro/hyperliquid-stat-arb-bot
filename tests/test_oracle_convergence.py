import pytest

from scripts.oracle_convergence_score import build_report
from src.hyperliquid_bot.oracle_convergence import (
    convergence_bps,
    favorable_edge_bps,
    impact_premium,
    init_db,
    position_pnl,
    rank_tails,
)


def test_impact_premium_uses_both_executable_impact_prices():
    assert impact_premium(100, 101, 103) == pytest.approx(0.02)


def test_rank_tails_selects_cheapest_long_and_richest_short():
    rows = [{"coin": str(i), "premium": value} for i, value in enumerate(range(-5, 5))]
    ranked = rank_tails(rows, 0.20)
    selected = [(row["premium"], row["selected_side"]) for row in ranked if row["selected_side"]]
    assert selected == [(-5, "long"), (-4, "long"), (3, "short"), (4, "short")]


def test_position_pnl_handles_long_and_short():
    assert position_pnl("long", 2, 100, 103) == 6
    assert position_pnl("short", 2, 100, 97) == 6


def test_cost_gate_measures_executable_edge_in_correct_direction():
    assert favorable_edge_bps("long", 99.8, 100) == pytest.approx(20)
    assert favorable_edge_bps("short", 100.2, 100) == pytest.approx(20)
    assert favorable_edge_bps("long", 100.2, 100) == pytest.approx(-20)


def test_positive_convergence_means_basis_moved_as_predicted():
    # Rich perp: +2% at entry, +0.5% at exit.
    assert convergence_bps("short", 102, 100, 100.5, 100) == pytest.approx(150)
    # Cheap perp: -2% at entry, -0.5% at exit.
    assert convergence_bps("long", 98, 100, 99.5, 100) == pytest.approx(150)


def test_score_refuses_early_verdict(tmp_path):
    conn = init_db(tmp_path / "oracle.db")
    cfg = {
        "hypothesis_id": "test",
        "inference": {
            "minimum_complete_cohorts": 30,
            "confirmatory_entry_hours_utc": [0, 4, 8, 12, 16, 20],
        },
    }
    report = build_report(conn, cfg)
    assert report["verdict"] == "not_ready"
    assert report["remaining_complete_cohorts"] == 30


def test_path_observations_are_restart_idempotent(tmp_path):
    conn = init_db(tmp_path / "oracle.db")
    columns = (
        "cohort_hour, coin, ts_ms, elapsed_minutes, oracle_px, executable_exit_px, "
        "exit_basis_bps, convergence_bps, gross_pnl_if_exit_usd, net_pnl_if_exit_usd"
    )
    values = ("c", "BTC", 1, 15, 100, 100, 0, 5, 1, 0.5)
    conn.execute(
        f"INSERT INTO path_observations ({columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        values,
    )
    conn.execute(
        f"INSERT OR IGNORE INTO path_observations ({columns}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        values,
    )
    assert conn.execute("SELECT COUNT(*) FROM path_observations").fetchone()[0] == 1
