"""M8 account / subaccount resolution (no network)."""
from __future__ import annotations

import pytest

from src.hyperliquid_bot.account import resolve_account_targets


def test_master_only(monkeypatch):
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS_MAINNET", "0xMaster")
    monkeypatch.delenv("HL_PAIRS_SUBACCOUNT", raising=False)
    t = resolve_account_targets({
        "account_address_env": "HL_ACCOUNT_ADDRESS_MAINNET",
        "subaccount_address_env": "HL_PAIRS_SUBACCOUNT",
    })
    assert t.master_address == "0xMaster"
    assert t.trading_address == "0xMaster"
    assert t.vault_address is None
    assert not t.using_subaccount


def test_subaccount_from_env(monkeypatch):
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS_MAINNET", "0xMaster")
    monkeypatch.setenv("HL_PAIRS_SUBACCOUNT", "0xPairsSub")
    t = resolve_account_targets({
        "account_address_env": "HL_ACCOUNT_ADDRESS_MAINNET",
        "subaccount_address_env": "HL_PAIRS_SUBACCOUNT",
    })
    assert t.vault_address == "0xPairsSub"
    assert t.trading_address == "0xPairsSub"
    assert t.master_address == "0xMaster"
    assert t.using_subaccount


def test_literal_subaccount_overrides_env(monkeypatch):
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS_MAINNET", "0xMaster")
    monkeypatch.setenv("HL_PAIRS_SUBACCOUNT", "0xEnvSub")
    t = resolve_account_targets({
        "account_address_env": "HL_ACCOUNT_ADDRESS_MAINNET",
        "subaccount_address_env": "HL_PAIRS_SUBACCOUNT",
        "subaccount_address": "0xLiteralSub",
    })
    assert t.vault_address == "0xLiteralSub"


def test_rejects_sub_equal_master(monkeypatch):
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS_MAINNET", "0xSame")
    with pytest.raises(RuntimeError, match="equals master"):
        resolve_account_targets({
            "account_address_env": "HL_ACCOUNT_ADDRESS_MAINNET",
            "subaccount_address": "0xSame",
        })


def test_missing_master_raises(monkeypatch):
    monkeypatch.delenv("HL_ACCOUNT_ADDRESS_MAINNET", raising=False)
    with pytest.raises(RuntimeError, match="Missing master"):
        resolve_account_targets({
            "account_address_env": "HL_ACCOUNT_ADDRESS_MAINNET",
        })
