#!/usr/bin/env python
"""
One-off, idempotent reconciliation of reports/trades.csv against state/positions.json.

Why
---
The closed-trade journal is missing episodes the bot actually closed. Current
state of the live book:

    positions.json closed episodes        37
    reports/trades.csv rows               18
    matched on entry timestamp            14
    recoverable (have pnl_usd, no row)    12
    unrecoverable (no pnl_usd)            11
    only in trades.csv (no state record)   3

The 3 journal-only rows matter: a naive "rebuild the journal from
positions.json" would DESTROY them. So this is a MERGE, never a rebuild.

It also never fabricates. The 11 unrecoverable episodes were closed by
reconciliation / manual flatten, so the bot never computed a PnL and there are
no fill prices to recover one from. They are reported, not written.

Method
------
Transcribe, never recompute. The stored pnl_usd IS the record; the job is to
get it into the journal with its provenance flagged, not to re-derive it.

positions.json stores the exit SPREAD and the entry coordinate frame but almost
never the exit PRICE (8 of 37). Since

    exit_spread = log(pa) - entry_alpha - beta_entry*log(pb) - entry_mean_raw

the pair is recoverable up to the beta axis; holding leg B at its entry price
and solving for A reproduces the stored exit_spread exactly. That is the only
reconstruction used, and it is asserted before writing. pnl_usd and pnl_pct are
copied verbatim.

Cost basis is NOT uniform across the source and the script says so. On the
unwritten rows the stored pnl_usd matches GROSS PnL (before the 15 bps
round-trip), while the journaled rows carry net PnL. This was measured, not
assumed: recomputing the unwritten rows through journal.compute_pnl lands ~15
bps per trade away, while the journaled rows match to the cent. Every merged
row is therefore tagged pending_reason="reconstructed_from_state" plus a
cost_basis flag, so a reader can exclude them instead of inheriting a silent
gross/net mix.

Rows are written through journal._ensure_file so the schema stays canonical and
the header migration is the bot's own code path.

Usage:
    .venv/bin/python scripts/rebuild_trade_journal.py            # dry run
    .venv/bin/python scripts/rebuild_trade_journal.py --apply    # write
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.hyperliquid_bot.phase3 import journal  # noqa: E402

LIVE = Path("/home/hlbot/hlbot-runtime/live")
POSITIONS = LIVE / "state" / "positions.json"
TRADES = LIVE / "reports" / "trades.csv"
GAPS = LIVE / "reports" / "trade_journal_gaps.csv"

# Two episodes on the same pair can open seconds apart, so identity is the entry
# timestamp at minute resolution.
KEY_FMT = "%Y-%m-%dT%H:%M"


def _key(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime(KEY_FMT)


def _f(d: dict, k: str, default: float = 0.0) -> float:
    v = d.get(k)
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def recover_exit_prices(pos: dict) -> tuple[float, float] | None:
    """Exit (price_a, price_b) implied by the stored exit spread.

    Holds leg B at its entry price. The pair reproduces the journal's pnl_usd
    exactly because that PnL was derived from the same spread.
    """
    pb = _f(pos, "entry_price_b")
    pa_entry = _f(pos, "entry_price_a")
    if not pb or not pa_entry:
        return None
    log_pb = math.log(pb)
    log_pa = (
        _f(pos, "exit_spread")
        + _f(pos, "entry_alpha")
        + _f(pos, "beta_entry") * log_pb
        + _f(pos, "entry_mean_raw")
    )
    return math.exp(log_pa), pb


def recoverable(pos: dict) -> tuple[bool, str, tuple[float, float] | None]:
    """(is_recoverable, reason, exit_prices)."""
    if pos.get("status") != "closed":
        return False, "not closed", None
    if pos.get("pnl_usd") is None:
        return False, "no pnl_usd recorded (reconciled/manual close)", None
    if pos.get("exit_spread") is None:
        return False, "no exit_spread", None
    px = recover_exit_prices(pos)
    if px is None:
        return False, "no entry prices", None
    return True, "", px


def verify(pos: dict, pa: float, pb: float) -> tuple[bool, float]:
    """Check the derived prices reproduce the stored exit SPREAD exactly.

    The spread, not the PnL, is what the reconstruction must match: PnL depends
    on cost basis, and the unwritten rows were recorded gross while the journal
    carries net. Both bases are reported so the difference is never silent.
    """
    raw = (math.log(pa) - _f(pos, "entry_alpha")
           - _f(pos, "beta_entry") * math.log(pb))
    return abs(raw - _f(pos, "entry_mean_raw") - _f(pos, "exit_spread")) < 1e-9, raw


def pnl_basis(pos: dict, pa: float, pb: float) -> tuple[float, float]:
    """(gross_pnl, net_pnl) for this episode under the bot's own cost model."""
    stub = {
        "direction": pos["direction"],
        "entry_price_a": _f(pos, "entry_price_a"),
        "entry_price_b": _f(pos, "entry_price_b"),
        "leg_a_usd": _f(pos, "leg_a_usd"),
        "leg_b_usd": _f(pos, "leg_b_usd"),
        "notional_usd": _f(pos, "notional_usd"),
    }
    for k in ("entry_size_a", "entry_size_b"):
        if k in pos:
            stub[k] = pos[k]
    gross, _ = journal.compute_pnl(stub, pa, pb, fee_pct=0.0, slippage_bps=0.0)
    net, _ = journal.compute_pnl(
        stub, pa, pb, funding_usd=_f(pos, "funding_usd"))
    return gross, net


def build_row(pos: dict, pa: float, pb: float) -> dict:
    """A canonical journal row, transcribed from the state record."""
    gross, net = pnl_basis(pos, pa, pb)
    stored = _f(pos, "pnl_usd")
    if abs(stored - gross) < 0.01:
        basis = "gross"
    elif abs(stored - net) < 0.01:
        basis = "net"
    else:
        basis = "other"

    entry_dt = datetime.fromisoformat(pos["entry_time_iso"])
    exit_dt = datetime.fromisoformat(pos["exit_time_iso"])
    row = {c: "" for c in journal.TRADES_COLUMNS}
    row.update({
        "pair": pos["pair"],
        "direction": pos["direction"],
        "entry_time_iso": pos["entry_time_iso"],
        "exit_time_iso": pos["exit_time_iso"],
        "elapsed_hours": f"{(exit_dt - entry_dt).total_seconds() / 3600:.3f}",
        "beta_entry": f"{_f(pos, 'beta_entry'):.6f}",
        "half_life_hours": f"{_f(pos, 'half_life_hours'):.3f}",
        "entry_price_a": f"{_f(pos, 'entry_price_a'):.8f}",
        "entry_price_b": f"{_f(pos, 'entry_price_b'):.8f}",
        "exit_price_a": f"{pa:.8f}",
        "exit_price_b": f"{pb:.8f}",
        "entry_spread": f"{_f(pos, 'entry_spread'):.6f}",
        "exit_spread": f"{_f(pos, 'exit_spread'):.6f}",
        "target_spread": f"{_f(pos, 'target_spread'):.6f}",
        "stop_spread": f"{_f(pos, 'stop_spread'):.6f}",
        "notional_usd": f"{_f(pos, 'notional_usd'):.2f}",
        "leg_a_usd": f"{_f(pos, 'leg_a_usd'):.2f}",
        "leg_b_usd": f"{_f(pos, 'leg_b_usd'):.2f}",
        # Transcribed verbatim -- this is the record, not a recomputation.
        "pnl_usd": f"{stored:.4f}",
        "pnl_pct": f"{_f(pos, 'pnl_pct'):.6f}",
        "exit_reason": str(pos.get("exit_reason") or ""),
        "p_revert_at_entry": f"{_f(pos, 'p_revert_at_entry'):.4f}",
        "funding_usd": f"{_f(pos, 'funding_usd'):.6f}",
        "fees_usd": ("" if pos.get("fees_usd") in (None, "")
                     else f"{_f(pos, 'fees_usd'):.6f}"),
        "maker_fills": str(pos.get("maker_fills", "")),
        "taker_fills": str(pos.get("taker_fills", "")),
        "pending_reason": f"reconstructed_from_state;cost_basis={basis};"
                          f"exit_prices=derived",
        "exit_detail": json.dumps(
            {"source": "positions.json", "cost_basis": basis,
             "gross_pnl": round(gross, 4), "net_pnl": round(net, 4)},
            separators=(",", ":")),
    })
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    positions = json.loads(POSITIONS.read_text())["positions"]
    if TRADES.exists():
        with TRADES.open() as fh:
            existing = list(csv.DictReader(fh))
            header = list(existing[0].keys()) if existing else []
    else:
        existing, header = [], []

    existing_keys = {_key(r["entry_time_iso"]) for r in existing
                     if r.get("entry_time_iso")}
    print(f"positions.json records     {len(positions)}"
          f"  (closed {sum(1 for p in positions if p.get('status') == 'closed')})")
    print(f"trades.csv rows            {len(existing)}")
    print(f"journal header columns     {len(header)}\n")

    closed = [p for p in positions if p.get("status") == "closed"]
    to_write, unrec, bad = [], [], []
    for pos in closed:
        k = _key(pos["entry_time_iso"])
        if k in existing_keys:
            continue
        if pos.get("pnl_usd") is None or pos.get("exit_spread") is None:
            unrec.append((pos, "no pnl_usd (reconciled/manual close)"))
            continue
        px = recover_exit_prices(pos)
        if px is None:
            unrec.append((pos, "no entry prices to derive an exit from"))
            continue
        ok, _raw = verify(pos, *px)
        if not ok:
            bad.append(pos)
            continue
        gross, net = pnl_basis(pos, *px)
        stored = _f(pos, "pnl_usd")
        basis = ("gross" if abs(stored - gross) < 0.01
                 else "net" if abs(stored - net) < 0.01 else "other")
        to_write.append((pos, px, stored, basis))

    already = len(closed) - len(to_write) - len(unrec) - len(bad)
    print(f"already journaled          {already}")
    print(f"recoverable, to merge      {len(to_write)}")
    print(f"unrecoverable              {len(unrec)}")
    print(f"failed spread verification {len(bad)}\n")

    if to_write:
        print("--- to merge (exit prices derived; pnl transcribed verbatim) ---")
        print(f"{'pair':<17}{'entry':<18}{'reason':<17}{'stored$':>9}{'basis':>7}")
        for pos, px, stored, basis in to_write:
            print(f"  {pos['pair']:<15}{_key(pos['entry_time_iso']):<18}"
                  f"{str(pos.get('exit_reason')):<17}{stored:>+9.2f}{basis:>7}")
        from collections import Counter
        print(f"\n  cost basis mix: {dict(Counter(b for _, _, _, b in to_write))}")
        print("  gross/net MIX: these rows are NOT directly comparable to the "
              f"{already} existing journal rows until reconciled.")

    if bad:
        print("\n--- SKIPPED: derived prices do not reproduce exit_spread ---")
        for pos in bad:
            print(f"  {pos['pair']:<15}{_key(pos['entry_time_iso'])}")

    if unrec:
        print("\n--- NOT WRITTEN: no PnL exists to record ---")
        for pos, why in unrec:
            print(f"  {pos['pair']:<15}{_key(pos['entry_time_iso']):<18}"
                  f"{str(pos.get('exit_reason')):<26}{why}")

    # Gaps are recorded even without --apply: this is state about missing data.
    if unrec:
        GAPS.parent.mkdir(parents=True, exist_ok=True)
        with GAPS.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["pair", "entry_time_iso",
                                               "exit_time_iso", "exit_reason",
                                               "why", "pnl_usd"])
            w.writeheader()
            for pos, why in unrec:
                w.writerow({"pair": pos["pair"],
                            "entry_time_iso": pos["entry_time_iso"],
                            "exit_time_iso": pos.get("exit_time_iso", ""),
                            "exit_reason": pos.get("exit_reason", ""),
                            "why": why,
                            "pnl_usd": ""})
        print(f"\nGap ledger written: {GAPS} ({len(unrec)} unrecoverable episodes)")

    if not args.apply:
        print(f"\nDRY RUN. Re-run with --apply to merge {len(to_write)} rows.")
        return

    if not to_write:
        print("\nNothing to merge.")
        return

    backup = TRADES.with_suffix(".csv.bak-merged")
    shutil.copy2(TRADES, backup)
    print(f"\nBackup: {backup}")

    # _ensure_file migrates the header, then append transcribed rows directly
    # (log_trade would recompute the PnL we are deliberately preserving).
    journal._ensure_file(TRADES, journal.TRADES_COLUMNS)
    for pos, px, _stored, _basis in to_write:
        journal._append_row(TRADES, journal.TRADES_COLUMNS, build_row(pos, *px))

    with TRADES.open() as fh:
        after = list(csv.DictReader(fh))
    print(f"trades.csv rows {len(existing)} -> {len(after)}")

    keys = {_key(r["entry_time_iso"]) for r in after}
    still = [p for p in closed if _key(p["entry_time_iso"]) not in keys
             and p.get("pnl_usd") is not None]
    print(f"closed episodes with a PnL but still unwritten: {len(still)}")
    for p in still:
        print(f"  {p['pair']:<16}{_key(p['entry_time_iso'])}")

    total = sum(_f(r, "pnl_usd") for r in after)
    print(f"journal net PnL now ${total:+.2f} across {len(after)} rows")


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
