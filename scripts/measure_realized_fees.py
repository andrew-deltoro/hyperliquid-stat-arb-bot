#!/usr/bin/env python
"""Measure realized trading cost from exchange fills.

The bot models cost as taker_fee (0.045%) + slippage_bps (3.0) = 0.075% one-way,
0.150% round trip. This tool replaces that guess with measurement, using the
`fee` and `crossed` fields the exchange reports per fill.

Usage:
    python scripts/measure_realized_fees.py            # all closed trades
    python scripts/measure_realized_fees.py --fills    # whole-account fill mix
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics as st
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

from hyperliquid.info import Info  # noqa: E402
from hyperliquid.utils import constants  # noqa: E402

from src.hyperliquid_bot.phase3.funding import fees_for_position  # noqa: E402

NETWORK = "mainnet"

# Live reports live in the runtime dir, which only resolves from REPORTS_DIR
# when HLBOT_RUNTIME_DIR is exported. Fall back explicitly so the tool works
# when run by hand.
_LIVE_REPORTS = Path("/home/hlbot/hlbot-runtime/live/reports")


def _default_trades() -> Path:
    from src.hyperliquid_bot.phase3.paths import REPORTS_DIR
    for cand in (REPORTS_DIR / "trades.csv", _LIVE_REPORTS / "trades.csv"):
        if cand.exists():
            return cand
    return REPORTS_DIR / "trades.csv"


def _info() -> Info:
    url = constants.TESTNET_API_URL if NETWORK == "testnet" else constants.MAINNET_API_URL
    return Info(url, skip_ws=True)


def _wallet() -> str:
    return (os.getenv("HL_PAIRS_SUBACCOUNT")
            or os.getenv("HL_ACCOUNT_ADDRESS_MAINNET")
            or os.getenv("HL_ACCOUNT_ADDRESS") or "")


def fill_mix(info: Info, wallet: str) -> None:
    """Whole-account maker/taker mix and effective fee rate."""
    fills = info.user_fills(wallet)
    if not fills:
        print("no fills returned")
        return

    def rate(f):
        ntl = abs(float(f["px"]) * float(f["sz"]))
        return (float(f.get("fee", 0) or 0) / ntl * 100) if ntl else None

    mk = [f for f in fills if not f.get("crossed")]
    tk = [f for f in fills if f.get("crossed")]
    print(f"fills: {len(fills)}  maker: {len(mk)}  taker: {len(tk)}")
    for name, s in (("maker", mk), ("taker", tk)):
        rs = [r for r in (rate(f) for f in s) if r is not None]
        if rs:
            print(f"  {name:6} n={len(rs):4d}  median={st.median(rs):.4f}%  mean={st.mean(rs):.4f}%")
    ntl = sum(abs(float(f["px"]) * float(f["sz"])) for f in fills)
    fee = sum(float(f.get("fee", 0) or 0) for f in fills)
    if ntl:
        print(f"\n  effective one-way: {100*fee/ntl:.4f}%   round trip: {2*100*fee/ntl:.4f}%")
        print(f"  bot model assumes 0.0750% one-way / 0.1500% round trip")


def per_trade(info: Info, wallet: str, trades_path: Path) -> None:
    if not trades_path.exists():
        print(f"no trades file at {trades_path}")
        return
    rows = list(csv.DictReader(open(trades_path)))
    if not rows:
        print("no closed trades")
        return

    print(f"{'pair':<18} {'fees$':>8} {'model$':>8} {'notional':>9} "
          f"{'maker':>6} {'taker':>6} {'source':<10}")
    tot_fee = tot_model = tot_ntl = 0.0
    exact = 0
    for r in rows:
        a, b = r["pair"].split("/")
        pos = {"pair": r["pair"], "coin_a": a.strip(), "coin_b": b.strip(),
               "entry_time_iso": r["entry_time_iso"],
               "exit_time_iso": r["exit_time_iso"]}
        s = fees_for_position(info, wallet, pos)
        model = (float(r["leg_a_usd"]) + float(r["leg_b_usd"])) * 0.00045 * 2
        tot_fee += s["total"]
        tot_model += model
        tot_ntl += s["notional"]
        if s["source"] == "ok":
            exact += 1
        print(f"{r['pair']:<18} {s['total']:>8.4f} {model:>8.4f} {s['notional']:>9.0f} "
              f"{s['maker_fills']:>6} {s['taker_fills']:>6} {s['source']:<10}")

    print()
    print(f"trades measured      : {exact}/{len(rows)}")
    print(f"TOTAL realized fees  : ${tot_fee:.4f}   (one-way {100*tot_fee/tot_ntl:.4f}%)"
          if tot_ntl else "")
    print(f"TOTAL modeled fees   : ${tot_model:.4f}")
    if tot_fee > 0:
        print(f"model over-charges   : {tot_model/tot_fee:.2f}x")
    print("\nNOTE: realized fees exclude slippage (entry/exit are fill prices).")
    print("      Modeled fee_pct terms are now replaced by measurement in trades.csv.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fills", action="store_true",
                    help="show whole-account maker/taker mix instead of per-trade")
    ap.add_argument("--trades", default=str(_default_trades()))
    args = ap.parse_args()

    wallet = _wallet()
    if not wallet:
        print("no wallet address in env")
        return
    info = _info()
    if args.fills:
        fill_mix(info, wallet)
    else:
        per_trade(info, wallet, Path(args.trades))
        print()
        fill_mix(info, wallet)


if __name__ == "__main__":
    main()
