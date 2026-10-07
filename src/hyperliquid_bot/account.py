"""
Resolve master vs strategy subaccount addresses for Hyperliquid SDK wiring.

HL treats subaccounts like vaults for signing: Exchange gets
`vault_address=<subaccount>` while Info queries use the subaccount address
as the user. The master address still owns the API wallet / agent.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AccountTargets:
    """Where to sign as (master) and where positions live (trading)."""
    master_address: str
    trading_address: str
    vault_address: str | None  # set when trading through a subaccount

    @property
    def using_subaccount(self) -> bool:
        return self.vault_address is not None


def resolve_account_targets(ex_cfg: dict) -> AccountTargets:
    """
    Read account + optional subaccount from executor config / env.

    Config keys (all optional except account):
      account_address_env   — env var for the master wallet (default HL_ACCOUNT_ADDRESS)
      account_address       — literal master address (overrides env if set)
      subaccount_address_env — env var for this strategy's subaccount
      subaccount_address     — literal subaccount (overrides env if set)
    """
    master = str(ex_cfg.get("account_address") or "").strip()
    if not master:
        acct_env = ex_cfg.get("account_address_env", "HL_ACCOUNT_ADDRESS")
        master = os.getenv(acct_env, "").strip()
    if not master:
        acct_env = ex_cfg.get("account_address_env", "HL_ACCOUNT_ADDRESS")
        raise RuntimeError(
            f"Missing master account address "
            f"(set {acct_env} or executor.account_address)"
        )

    sub = str(ex_cfg.get("subaccount_address") or "").strip()
    if not sub:
        sub_env = ex_cfg.get("subaccount_address_env") or ""
        if sub_env:
            sub = os.getenv(sub_env, "").strip()

    if sub:
        if sub.lower() == master.lower():
            raise RuntimeError(
                "subaccount_address equals master; omit subaccount to trade on master"
            )
        return AccountTargets(
            master_address=master, trading_address=sub, vault_address=sub,
        )
    return AccountTargets(
        master_address=master, trading_address=master, vault_address=None,
    )
