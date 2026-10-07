"""Directional bot (H8, M8) and funding executor (M7)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.hyperliquid_bot.directional import positions as dpos
from src.hyperliquid_bot.directional.executor import DirectionalExecutor
from src.hyperliquid_bot.directional.positions import DirPositionManager
from src.hyperliquid_bot.directional.signals import Signal as DirSignal

from .fake_hl import FakeHL


class DirFakeHL(FakeHL):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.open_orders: list[dict] = []
        self.canceled: list[int] = []

    def order(self, coin, is_buy, sz, limit_px, order_type, reduce_only=False):
        if "trigger" in order_type:
            oid = self._oid()
            self.orders.append({"coin": coin, "is_buy": is_buy, "sz": sz, "tif": "trigger",
                                "reduce_only": reduce_only, "oid": oid,
                                "trigger": order_type["trigger"]})
            self.open_orders.append({"coin": coin, "oid": oid, "isTrigger": True,
                                     "reduceOnly": reduce_only})
            return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}
        return super().order(coin, is_buy, sz, limit_px, order_type, reduce_only)

    def frontend_open_orders(self, address, dex=""):
        return list(self.open_orders)

    def cancel(self, coin, oid):
        self.canceled.append(oid)
        self.open_orders = [o for o in self.open_orders if o["oid"] != oid]
        return {"status": "ok"}

    def all_mids(self, dex=""):
        return {c: str(p) for c, p in self.prices.items()}


@pytest.fixture
def dir_env(tmp_path, monkeypatch):
    monkeypatch.setattr(dpos, "POS_FILE", tmp_path / "dir.json")
    hl = DirFakeHL({"SOL": 100.0})
    cfg = SimpleNamespace(raw={"executor": {"slippage_tolerance": 0.01}})
    ex = DirectionalExecutor(cfg, log_fn=lambda m: None, exchange=hl, info=hl, wallet="0x")
    return hl, DirPositionManager(cfg, ex, log_fn=lambda m: None)


def sol_signal(base=100.0):
    return DirSignal(coin="SOL", ts=0, close=100.0, ma=90.0, momentum=0.05,
                     vol_ratio=2.0, atr=2.0, trend="up", action="OPEN_LONG",
                     direction="LONG", stop_price=96.0, target_price=108.0,
                     suggested_notional=base)


def test_leverage_applied_once_and_stop_rests_on_exchange(dir_env):
    hl, pm = dir_env
    res = pm.try_open(sol_signal(100.0), equity=1000, leverage=3.0,
                      max_concurrent=2, max_notional_per_pos=1000)
    assert res["opened"]
    assert hl.positions["SOL"] == pytest.approx(3.0)       # $300 / $100
    stop = [o for o in hl.orders if o["tif"] == "trigger"][0]
    assert stop["reduce_only"] and not stop["is_buy"]
    assert stop["trigger"]["tpsl"] == "sl"


def test_close_reduces_only_own_quantity_and_cancels_stop(dir_env):
    hl, pm = dir_env
    pm.try_open(sol_signal(100.0), equity=1000, leverage=1.0,
                max_concurrent=2, max_notional_per_pos=1000)
    hl.positions["SOL"] += 4.0   # another strategy adds to the same coin
    res = pm.evaluate("SOL", 110.0, None)             # target hit
    assert res["action"] == "closed"
    assert hl.positions["SOL"] == pytest.approx(4.0)
    assert hl.open_orders == []


def test_entry_skipped_when_coin_held_elsewhere(dir_env):
    hl, pm = dir_env
    hl.positions["SOL"] = -2.0
    res = pm.try_open(sol_signal(), equity=1000, leverage=1.0,
                      max_concurrent=2, max_notional_per_pos=1000)
    assert not res["opened"] and "another strategy" in res["reason"]


def test_exchange_stop_fill_is_recognized(dir_env):
    hl, pm = dir_env
    pm.try_open(sol_signal(), equity=1000, leverage=1.0,
                max_concurrent=2, max_notional_per_pos=1000)
    hl.positions.pop("SOL")
    res = pm.evaluate("SOL", 95.0, None)
    assert res["action"] == "closed" and res["reason"] == "exchange_stop"


def test_funding_perp_error_unwinds_spot(monkeypatch):
    fexec = pytest.importorskip("src.hyperliquid_bot.funding.executor")
    fe = object.__new__(fexec.FundingExecutor)
    fe.dry_run, fe.allow_transfers, fe.wallet = False, False, "0x"
    calls = []

    def place(name, is_buy, size, px, slippage=0.02, reduce_only=False):
        calls.append((name, is_buy, size))
        if name == "PURR":
            return {"status": "error", "error": "Insufficient margin"}
        return {"status": "filled", "total_sz": size, "avg_px": px}

    fe._place = place
    fe._spot_pair = lambda coin: {"name": "PURR/USDC", "index": 0}
    fe._mid = lambda name, idx=None: 0.2
    fe.ensure_spot_usdc = lambda n: {"action": "noop"}
    fe.ensure_perp_usdc = lambda n: {"action": "noop"}

    res = fe.open_funding_pair("PURR", 100.0)
    assert res["status"] == "unwound"
    assert calls[-1] == ("PURR/USDC", False, pytest.approx(500.0))


def test_funding_executor_parses_error_status():
    fexec = pytest.importorskip("src.hyperliquid_bot.funding.executor")
    r = {"status": "ok", "response": {"data": {"statuses": [{"error": "x"}]}}}
    assert fexec.parse_order_result(r)["status"] == "error"
