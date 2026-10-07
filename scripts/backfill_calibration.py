#!/usr/bin/env python3
"""
Migrate trades.csv / signals.csv headers and backfill funding_usd.

Historical rows predate exp_return_at_entry. That field cannot be recovered
from p_revert alone (different units), so it stays blank until new closes
write it. Funding can be reconstructed from user_funding_history.

  python scripts/backfill_calibration.py            # dry-run
  python scripts/backfill_calibration.py --execute  # rewrite reports/trades.csv
"""
from __future__ import annotations

import argparse
import csv
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

from src.hyperliquid_bot.phase3 import funding, journal
from src.hyperliquid_bot.phase3.journal import SIGNALS_COLUMNS, TRADES_COLUMNS
from src.hyperliquid_bot.phase3.paths import REPORTS_DIR


def _pair_coins(pair: str) -> tuple[str, str]:
    a, b = pair.split("/", 1)
    return a.strip(), b.strip()


def _info_and_wallet(network: str):
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    base = (constants.MAINNET_API_URL if network == "mainnet"
            else constants.TESTNET_API_URL)
    wallet = os.getenv("HL_ACCOUNT_ADDRESS_MAINNET" if network == "mainnet"
                       else "HL_ACCOUNT_ADDRESS", "")
    if not wallet:
        raise SystemExit("Missing account address env for funding backfill")
    return Info(base, skip_ws=True, perp_dexs=["", "xyz"]), wallet


def migrate_headers(path: Path, columns: list[str]) -> int:
    """Ensure CSV has the current column set; return number of data rows."""
    journal._ensure_file(path, columns)  # noqa: SLF001 — intentional migrate
    with open(path, newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


def backfill_trades(path: Path, network: str, execute: bool) -> None:
    n = migrate_headers(path, TRADES_COLUMNS)
    print(f"trades header ok ({n} rows) → {path}")
    if n == 0:
        return

    info, wallet = _info_and_wallet(network)
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))

    updated = 0
    er_present = 0
    for row in rows:
        if (row.get("exp_return_at_entry") or "").strip():
            er_present += 1
        pair = row.get("pair") or ""
        if "/" not in pair:
            continue
        coin_a, coin_b = _pair_coins(pair)
        start, end = row.get("entry_time_iso"), row.get("exit_time_iso")
        if not start or not end:
            continue
        fund = funding.position_funding_usd(
            info, wallet,
            {"coin_a": coin_a, "coin_b": coin_b,
             "entry_time_iso": start, "exit_time_iso": end},
        )
        old = (row.get("funding_usd") or "").strip()
        new = f"{fund:.6f}"
        if old != new:
            # Adjust stored PnL by the funding delta vs whatever was implied.
            try:
                old_f = float(old) if old else 0.0
            except ValueError:
                old_f = 0.0
            delta = fund - old_f
            try:
                pnl_usd = float(row.get("pnl_usd") or 0.0) + delta
                notional = float(row.get("notional_usd") or 0.0)
                row["pnl_usd"] = f"{pnl_usd:.4f}"
                if notional > 0:
                    row["pnl_pct"] = f"{pnl_usd / notional:.6f}"
            except ValueError:
                pass
            row["funding_usd"] = new
            updated += 1
            print(f"  {pair} {start[:19]}  funding {old or '∅'} → {new}")

    print(f"funding updates: {updated}; rows with exp_return_at_entry: {er_present}/{len(rows)}")
    print("note: exp_return_at_entry left blank for pre-model trades "
          "(p_revert is not in return units)")

    if not execute:
        print("dry-run; pass --execute to rewrite")
        return

    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TRADES_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)
    print(f"wrote {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--network", default="mainnet",
                    choices=["mainnet", "testnet"])
    ap.add_argument("--trades", type=Path,
                    default=REPORTS_DIR / "trades.csv")
    ap.add_argument("--signals", type=Path,
                    default=REPORTS_DIR / "signals.csv")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    if args.signals.exists() or args.execute:
        n = migrate_headers(args.signals, SIGNALS_COLUMNS)
        print(f"signals header ok ({n} rows) → {args.signals}")

    if args.trades.exists():
        backfill_trades(args.trades, args.network, args.execute)
    else:
        print(f"no trades file at {args.trades}")


if __name__ == "__main__":
    main()
