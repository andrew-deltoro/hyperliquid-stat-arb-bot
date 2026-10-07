#!/usr/bin/env python3
"""
List / create / fund Hyperliquid strategy subaccounts (M8).

Dry-run by default. Creating empty subaccounts and transferring USDC both
require --execute. Transfers use HL raw units (dollars × 1e6).

Hyperliquid currently requires ~$100k lifetime traded volume on the master
before createSubAccount succeeds; below that the API returns an error and
strategies keep sharing the master (soft isolation only).

Examples:
  python scripts/manage_subaccounts.py list
  python scripts/manage_subaccounts.py create --name pairs --name directional
  python scripts/manage_subaccounts.py create --name pairs --execute
  python scripts/manage_subaccounts.py transfer --to pairs --usd 500 --execute
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    live_env = Path("/home/hlbot/hyperliquid-stat-arb-bot/.env")
    if live_env.exists():
        load_dotenv(live_env, override=False)
except ImportError:
    pass


def _sdk(network: str):
    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    base = (constants.MAINNET_API_URL if network == "mainnet"
            else constants.TESTNET_API_URL)
    pk = os.getenv("HL_PRIVATE_KEY_MAINNET" if network == "mainnet"
                   else "HL_PRIVATE_KEY")
    master = os.getenv("HL_ACCOUNT_ADDRESS_MAINNET" if network == "mainnet"
                       else "HL_ACCOUNT_ADDRESS")
    if not pk or not master:
        raise SystemExit("Missing HL private key / account address in env")
    account = Account.from_key(pk)
    exchange = Exchange(account, base, account_address=master)
    info = Info(base, skip_ws=True)
    return exchange, info, master


def _subs(info, master: str) -> list[dict]:
    raw = info.query_sub_accounts(master)
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return list(raw) if raw else []


def _by_name(subs: list[dict]) -> dict[str, dict]:
    out = {}
    for s in subs:
        name = s.get("name") or s.get("subAccountUser") or ""
        out[str(name)] = s
    return out


def cmd_list(args):
    _, info, master = _sdk(args.network)
    subs = _subs(info, master)
    print(f"master={master}  n_subaccounts={len(subs)}")
    print(json.dumps(subs, indent=2, default=str))


def cmd_create(args):
    exchange, info, master = _sdk(args.network)
    existing = _by_name(_subs(info, master))
    names = args.name or []
    if not names:
        raise SystemExit("pass one or more --name")
    for name in names:
        if name in existing:
            addr = (existing[name].get("subAccountUser")
                    or existing[name].get("address") or "?")
            print(f"exists  name={name}  address={addr}")
            continue
        if not args.execute:
            print(f"dry-run createSubAccount name={name}")
            continue
        resp = exchange.create_sub_account(name)
        print(f"create name={name} resp={resp}")
    if args.execute:
        print("--- after create ---")
        cmd_list(args)


def cmd_transfer(args):
    exchange, info, master = _sdk(args.network)
    subs = _by_name(_subs(info, master))
    target = args.to
    if target.startswith("0x"):
        sub_addr = target
    else:
        row = subs.get(target)
        if not row:
            raise SystemExit(f"unknown subaccount name {target!r}; run list")
        sub_addr = row.get("subAccountUser") or row.get("address")
        if not sub_addr:
            raise SystemExit(f"subaccount {target!r} missing address field")
    usd = float(args.usd)
    if usd <= 0:
        raise SystemExit("--usd must be positive")
    # HL subAccountTransfer `usd` is integer USDC * 1e6 (see SDK examples).
    usd_units = int(round(usd * 1_000_000))
    is_deposit = not args.withdraw
    action = "deposit" if is_deposit else "withdraw"
    if not args.execute:
        print(f"dry-run subAccountTransfer {action} ${usd:.2f} "
              f"({usd_units} units) <-> {sub_addr}")
        return
    resp = exchange.sub_account_transfer(sub_addr, is_deposit, usd_units)
    print(f"{action} ${usd:.2f} sub={sub_addr} resp={resp}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--network", default="mainnet",
                   choices=["mainnet", "testnet"])
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="show subaccounts")
    p_list.set_defaults(func=cmd_list)

    p_create = sub.add_parser("create", help="create named subaccounts")
    p_create.add_argument("--name", action="append", default=[],
                          help="subaccount name (repeatable)")
    p_create.add_argument("--execute", action="store_true")
    p_create.set_defaults(func=cmd_create)

    p_xfer = sub.add_parser("transfer", help="move USDC master <-> subaccount")
    p_xfer.add_argument("--to", required=True,
                        help="subaccount name or 0x address")
    p_xfer.add_argument("--usd", type=float, required=True)
    p_xfer.add_argument("--withdraw", action="store_true",
                        help="pull from subaccount to master (default: deposit)")
    p_xfer.add_argument("--execute", action="store_true")
    p_xfer.set_defaults(func=cmd_transfer)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
