"""The Kalshi score is settlement minus the ask minus the one-contract fee."""
from src.hyperliquid_bot.kalshi_hypothesis import (
    contract_pnl,
    score_joined,
    taker_fee,
    walk_forward,
    Scored,
)


def test_fee_matches_published_one_contract_table():
    assert taker_fee(0.50) == 0.02
    assert taker_fee(0.10) == 0.01
    assert taker_fee(0.50, contracts=100) == 1.75


def test_pnl_is_settlement_minus_ask_minus_fee():
    # Buy yes at 40 cents, fee 2 cents, settles yes: 1 - 0.40 - 0.02.
    assert contract_pnl("yes", 0.40, "yes", 0.02) == 0.58
    # Same buy, settles no: the stake and the fee are both lost.
    assert abs(contract_pnl("yes", 0.40, "no", 0.02) - (-0.42)) < 1e-9
    # Buy no. Winning no pays 1, not the yes price.
    assert contract_pnl("no", 0.60, "no", 0.02) == 0.38


def test_join_skips_other_series_flats_and_unsettled():
    decisions = [
        {"type": "decision", "series": "KXBTC15M", "ticker": "A", "side": "yes",
         "price": 0.40, "close_time": "2026-10-08T17:00:00Z", "contracts": 1},
        {"type": "decision", "series": "KXBTC15M", "ticker": "B", "side": "flat",
         "close_time": "2026-10-08T17:15:00Z"},
        {"type": "decision", "series": "KXETH15M", "ticker": "C", "side": "yes",
         "price": 0.40, "close_time": "2026-10-08T17:00:00Z"},
        {"type": "decision", "series": "KXBTC15M", "ticker": "D", "side": "no",
         "price": 0.55, "close_time": "2026-10-08T17:30:00Z"},
    ]
    settlements = {
        "A": {"result": "yes"},
        "C": {"result": "yes"},
    }
    scored = score_joined(decisions, settlements, "KXBTC15M")
    assert [row.close_time for row in scored] == ["2026-10-08T17:00:00Z"]
    assert scored[0].pnl == contract_pnl("yes", 0.40, "yes", taker_fee(0.40))


def test_verdict_waits_for_the_minimum():
    rows = [Scored(pnl=0.1, close_time=f"t{i:04d}") for i in range(10)]
    early = walk_forward(rows, minimum=1920)
    assert early["ready"] is False
    assert early["remaining"] == 1910
    done = walk_forward(rows, minimum=10, scatter_frac=0.70)
    assert done["ready"] is True
    assert done["scatter_n"] == 7
    assert done["holdout_n"] == 3
    assert done["kill"] is False
