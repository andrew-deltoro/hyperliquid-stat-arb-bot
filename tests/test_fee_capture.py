"""Realized fee capture: measure cost from the exchange instead of assuming it."""
from __future__ import annotations

import csv

from src.hyperliquid_bot.phase3 import funding, journal


def _ms(iso: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


POS = {
    "pair": "RSR/TAO",
    "direction": "LONG_A_SHORT_B",
    "coin_a": "RSR",
    "coin_b": "TAO",
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
}

# Field names verified against a live mainnet userFills response.
def _fill(coin, px, sz, fee, crossed, iso):
    return {"coin": coin, "px": str(px), "sz": str(sz), "fee": str(fee),
            "crossed": crossed, "time": _ms(iso), "oid": 1}


class _Info:
    def __init__(self, fills):
        self._fills = fills

    def user_fills(self, wallet):
        return self._fills


def test_fees_are_summed_per_leg_with_maker_taker_split():
    fills = [
        _fill("RSR", 0.001597, 145000, 0.0347, False, "2026-10-12T04:05:00+00:00"),
        _fill("RSR", 0.002274, 145000, 0.0989, True, "2026-10-12T13:05:00+00:00"),
        _fill("TAO", 284.68, 0.52, 0.0222, False, "2026-10-12T04:06:00+00:00"),
        _fill("TAO", 297.23, 0.52, 0.0693, True, "2026-10-12T13:06:00+00:00"),
    ]
    snap = funding.fees_for_position(_Info(fills), "0xw", POS)
    assert snap["source"] == "ok"
    assert abs(snap["by_coin"]["RSR"] - 0.1336) < 1e-9
    assert abs(snap["by_coin"]["TAO"] - 0.0915) < 1e-9
    assert abs(snap["total"] - 0.2251) < 1e-9
    assert snap["maker_fills"] == 2
    assert snap["taker_fills"] == 2


def test_fills_outside_the_holding_window_are_excluded():
    fills = [
        _fill("RSR", 0.001597, 1000, 1.0, True, "2026-10-01T00:00:00+00:00"),
        _fill("RSR", 0.001597, 1000, 0.01, True, "2026-10-12T05:00:00+00:00"),
    ]
    snap = funding.fees_for_position(_Info(fills), "0xw", POS)
    assert abs(snap["total"] - 0.01) < 1e-9


def test_unrelated_coin_is_ignored():
    fills = [_fill("BTC", 60000, 1, 27.0, True, "2026-10-12T05:00:00+00:00")]
    snap = funding.fees_for_position(_Info(fills), "0xw", POS)
    assert snap["total"] == 0.0
    assert snap["source"] == "no_fills_in_window"


def test_truncated_window_is_flagged_not_silently_understated(monkeypatch):
    """If fills scrolled out of the API window, say so instead of reporting low."""
    # A full page whose oldest fill is still INSIDE the window means the
    # entry-side fills fell off the page.
    monkeypatch.setattr(funding, "_FILLS_CAP", 1)
    fills = [_fill("RSR", 0.001597, 1000, 0.01, True, "2026-10-12T09:00:00+00:00")]
    snap = funding.fees_for_position(_Info(fills), "0xw", POS)
    assert snap["source"] == "partial_window"


def test_short_page_is_not_treated_as_truncated():
    fills = [_fill("RSR", 0.001597, 1000, 0.01, True, "2026-10-12T09:00:00+00:00")]
    snap = funding.fees_for_position(_Info(fills), "0xw", POS)
    assert snap["source"] == "ok"


def test_query_failure_is_reported_not_raised():
    class Boom:
        def user_fills(self, wallet):
            raise RuntimeError("network")
    snap = funding.fees_for_position(Boom(), "0xw", POS)
    assert snap["source"] == "query_failed"
    assert snap["total"] == 0.0


def test_compute_pnl_uses_measured_fees_when_supplied():
    """fees_usd replaces the fee_pct model; slippage allowance is retained."""
    a, b = 0.002274, 297.23
    modeled, _ = journal.compute_pnl(POS, a, b)
    measured, _ = journal.compute_pnl(POS, a, b, fees_usd=0.2251)
    _, _, leg_a, leg_b = journal._leg_pnls(POS, a, b)
    assumed_fee = (leg_a + leg_b) * 0.00045 * 2
    # With the real fee (smaller than the assumed one) net PnL must improve.
    assert measured > modeled
    assert abs((measured - modeled) - (assumed_fee - 0.2251)) < 1e-6


def test_log_trade_records_fee_columns(tmp_path, monkeypatch):
    f = tmp_path / "trades.csv"
    monkeypatch.setattr(journal, "TRADES_FILE", f)
    pos = dict(POS)
    pos["exit_spread"] = 0.0
    pos["fees_usd"] = 0.2251
    pos["maker_fills"] = 2
    pos["taker_fills"] = 2
    journal.log_trade(pos, 0.002274, 297.23)
    r = next(iter(csv.DictReader(open(f))))
    assert float(r["fees_usd"]) == 0.2251
    assert r["maker_fills"] == "2" and r["taker_fills"] == "2"
    assert r["modeled_cost_usd"] and r["realized_fee_pct"]


def test_compute_pnl_defaults_are_the_module_constants():
    """A no-arg call must reproduce the historical constants exactly.

    The close path now passes the configured fee and slippage. If the defaults
    ever drift away from DEFAULT_FEE_PCT / DEFAULT_SLIPPAGE_BPS, every caller
    that omits them silently changes cost, so pin them here.
    """
    a, b = 0.002274, 297.23
    _, _, leg_a, leg_b = journal._leg_pnls(POS, a, b)
    ntl = leg_a + leg_b
    implicit, _ = journal.compute_pnl(POS, a, b)
    explicit, _ = journal.compute_pnl(
        POS, a, b,
        fee_pct=journal.DEFAULT_FEE_PCT,
        slippage_bps=journal.DEFAULT_SLIPPAGE_BPS,
    )
    assert abs(implicit - explicit) < 1e-12
    legacy, _ = journal.compute_pnl(POS, a, b, fee_pct=0.00045,
                                    slippage_bps=3.0)
    assert abs(implicit - legacy) < 1e-12
    # and the two constants are actually the ones the costs are built from
    zero_cost, _ = journal.compute_pnl(POS, a, b, fee_pct=0.0, slippage_bps=0.0)
    assert abs((zero_cost - legacy) -
               (ntl * 0.00045 * 2 + ntl * 3.0 / 1e4 * 2)) < 1e-9


def test_compute_pnl_honours_configured_cost_model():
    """A configured fee/slippage must move the result; a lower fee must help."""
    a, b = 0.002274, 297.23
    _, _, leg_a, leg_b = journal._leg_pnls(POS, a, b)
    ntl = leg_a + leg_b
    cheap, _ = journal.compute_pnl(POS, a, b, fee_pct=0.00015, slippage_bps=1.0)
    dear, _ = journal.compute_pnl(POS, a, b, fee_pct=0.00045, slippage_bps=3.0)
    assert cheap > dear
    expected_gap = ntl * (0.00045 - 0.00015) * 2 + ntl * (3.0 - 1.0) / 1e4 * 2
    assert abs((cheap - dear) - expected_gap) < 1e-9
    # measured fees still win over the configured fee_pct, but slippage applies
    measured, _ = journal.compute_pnl(POS, a, b, fee_pct=0.00015,
                                      slippage_bps=1.0, fees_usd=0.0)
    assert abs((measured - cheap) - ntl * 0.00015 * 2) < 1e-9


def test_log_fees_writes_per_leg_rows(tmp_path):
    f = tmp_path / "fees.csv"
    fills = [
        _fill("RSR", 0.001597, 145000, 0.0347, False, "2026-10-12T04:05:00+00:00"),
        _fill("TAO", 297.23, 0.52, 0.0693, True, "2026-10-12T13:06:00+00:00"),
    ]
    funding.log_fees(_Info(fills), "0xw", POS, f)
    rows = list(csv.DictReader(open(f)))
    assert len(rows) == 2
    assert {r["leg"] for r in rows} == {"a", "b"}
    assert all(r["pair"] == "RSR/TAO" for r in rows)
