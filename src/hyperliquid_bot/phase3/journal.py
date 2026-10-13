"""
Append-only CSV journal.

Two files:
  reports/trades.csv   — one row per position lifecycle (open + close together)
  reports/signals.csv  — one row per signal generated (including SKIPs)

Both are created with headers on first write. Appended atomically.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


from .paths import REPORTS_DIR

TRADES_FILE = REPORTS_DIR / "trades.csv"
SIGNALS_FILE = REPORTS_DIR / "signals.csv"


TRADES_COLUMNS = [
    "pair", "direction",
    "entry_time_iso", "exit_time_iso", "elapsed_hours",
    "beta_entry", "half_life_hours",
    "entry_price_a", "entry_price_b",
    "exit_price_a", "exit_price_b",
    "entry_spread", "exit_spread",
    "target_spread", "stop_spread",
    "notional_usd", "leg_a_usd", "leg_b_usd",
    "pnl_usd", "pnl_pct",
    "exit_reason",
    "p_revert_at_entry",
    "exp_return_at_entry",
    "funding_usd",
    # Per-leg attribution: separates the spread reversion we *intend* to
    # capture from a single-leg trend that happens to be in the pair.
    "pnl_a_usd", "pnl_b_usd", "leg_a_pct", "leg_b_pct",
]

# Columns appended in later revisions; journal strips legacy duplicates.
TRADES_DEDUP_KEY = "pair"


SIGNALS_COLUMNS = [
    "timestamp_iso", "pair",
    "beta", "beta_sd",
    "nu_coint", "converged_coint",
    "current_spread", "spread_sd", "z",
    "kappa", "half_life_hours",
    "sigma_ou", "nu_ou", "converged_ou",
    "p_revert", "edge", "notional",
    "direction", "action",
    "skip_reason",
    "exp_return", "stress_loss_frac", "eg_tstat",
    "shock_side", "shock_hours_since", "shock_magnitude",
]


def _ensure_file(path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
        return
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        if header == columns:
            return
        rows = list(reader)
    # Column set changed: rewrite with the new header, keeping old rows.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def _append_row(path: Path, columns: list[str], row: dict) -> None:
    _ensure_file(path, columns)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writerow(row)
    # Flush to disk immediately
    with open(path, "r+") as f:
        f.flush()
        os.fsync(f.fileno())


def log_signal(sig) -> None:
    if sig is None:
        return
    """Append one signal to signals.csv (including SKIP decisions)."""
    skip_reason = ""
    if sig.action == "SKIP":
        skip_reason = getattr(sig, "skip_reason", "") or "unknown"

    row = {
        "timestamp_iso": datetime.now(timezone.utc).isoformat(),
        "pair": sig.pair,
        "beta": f"{sig.beta:.6f}",
        "beta_sd": f"{sig.beta_sd:.6f}",
        "nu_coint": f"{sig.nu_coint:.3f}",
        "converged_coint": sig.converged_coint,
        "current_spread": f"{sig.current_spread:.6f}",
        "spread_sd": f"{sig.spread_sd:.6f}",
        "z": f"{sig.z:.4f}",
        "kappa": f"{sig.kappa:.6f}",
        "half_life_hours": f"{sig.half_life_hours:.3f}",
        "sigma_ou": f"{sig.sigma_ou:.6f}",
        "nu_ou": f"{sig.nu_ou:.3f}",
        "converged_ou": sig.converged_ou,
        "p_revert": f"{sig.p_revert:.4f}",
        "edge": f"{sig.edge:.4f}",
        "notional": f"{sig.notional:.2f}",
        "direction": sig.direction,
        "action": sig.action,
        "skip_reason": skip_reason,
        "exp_return": f"{getattr(sig, 'exp_return', 0.0):.6f}",
        "stress_loss_frac": f"{getattr(sig, 'stress_loss_frac', 0.0):.6f}",
        "eg_tstat": f"{getattr(sig, 'eg_tstat', 0.0):.3f}",
    }
    shock = getattr(sig, "shock", None) or {}
    hs = shock.get("hours_since")
    row.update({
        "shock_side": shock.get("side") or "",
        "shock_hours_since": "" if hs is None else str(int(hs)),
        "shock_magnitude": "" if not shock.get("is_shock") else f"{shock.get('magnitude', 0.0):.6f}",
    })
    _append_row(SIGNALS_FILE, SIGNALS_COLUMNS, row)


def _leg_pnls(pos: dict, exit_price_a: float, exit_price_b: float) -> tuple[float, float, float, float]:
    """Gross P&L per leg in USD plus the deployed notional of each leg."""
    if "entry_size_a" in pos and "entry_size_b" in pos:
        # Signed filled quantities: exact for what the exchange holds.
        qa, qb = float(pos["entry_size_a"]), float(pos["entry_size_b"])
        pnl_a = qa * (exit_price_a - pos["entry_price_a"])
        pnl_b = qb * (exit_price_b - pos["entry_price_b"])
        leg_a = abs(qa) * pos["entry_price_a"]
        leg_b = abs(qb) * pos["entry_price_b"]
    else:
        sign_a = 1.0 if pos["direction"] == "LONG_A_SHORT_B" else -1.0
        sign_b = -sign_a
        leg_a = abs(pos["leg_a_usd"])
        leg_b = abs(pos["leg_b_usd"])
        pnl_a = leg_a * sign_a * (exit_price_a / pos["entry_price_a"] - 1.0)
        pnl_b = leg_b * sign_b * (exit_price_b / pos["entry_price_b"] - 1.0)
    return float(pnl_a), float(pnl_b), float(leg_a), float(leg_b)


def compute_pnl(
    pos: dict,
    exit_price_a: float,
    exit_price_b: float,
    fee_pct: float = 0.00045,
    slippage_bps: float = 3.0,
    funding_usd: float = 0.0,
) -> tuple[float, float]:
    """
    Realized P&L in USD and pct of deployed notional, NET of fees,
    slippage, and funding.

    LONG_A_SHORT_B: long A, short B
    SHORT_A_LONG_B: short A, long B

    Cost model:
      - fee_pct applied to notional on entry AND exit
      - slippage_bps applied to notional on entry AND exit
      - funding_usd: sum of account funding payments on both coins while open
        (positive = received)
    """
    pnl_a, pnl_b, leg_a, leg_b = _leg_pnls(pos, exit_price_a, exit_price_b)
    gross_pnl = pnl_a + pnl_b

    # Round-trip cost model:
    #   fee:     notional × fee_pct × 2 sides
    #   slippage: notional × (bps/10000) × 2 sides
    notional_total = leg_a + leg_b
    fee_cost = notional_total * fee_pct * 2.0
    slippage_cost = notional_total * (slippage_bps / 10000.0) * 2.0

    net_pnl = gross_pnl - fee_cost - slippage_cost + float(funding_usd)
    pnl_pct = net_pnl / (pos["notional_usd"] + 1e-9)
    return net_pnl, pnl_pct


def calibration_pairs() -> tuple[list[float], list[float]]:
    """(predicted exp_return, realized pnl_pct) for closed trades that have both.

    Rows with blank exp_return_at_entry (pre-E[r] journal) are skipped — do not
    substitute p_revert; it is not in return units and would crush c.
    """
    if not TRADES_FILE.exists():
        return [], []
    pred, real = [], []
    with open(TRADES_FILE, newline="") as f:
        for row in csv.DictReader(f):
            raw_p = (row.get("exp_return_at_entry") or "").strip()
            if not raw_p:
                continue
            try:
                p = float(raw_p)
                r = float(row.get("pnl_pct") or "nan")
            except ValueError:
                continue
            if p == p and r == r:
                pred.append(p)
                real.append(r)
    return pred, real


def log_trade(pos: dict, exit_price_a: float, exit_price_b: float,
              funding_usd: float = 0.0) -> None:
    """Append a completed trade (entry + exit) to trades.csv."""
    entry_dt = datetime.fromisoformat(pos["entry_time_iso"])
    exit_dt = datetime.fromisoformat(pos["exit_time_iso"])
    elapsed_hours = (exit_dt - entry_dt).total_seconds() / 3600.0

    funding_usd = float(pos.get("funding_usd", funding_usd) or 0.0)
    pnl_usd, pnl_pct = compute_pnl(
        pos, exit_price_a, exit_price_b, funding_usd=funding_usd,
    )
    leg_pnl_a, leg_pnl_b, leg_ntl_a, leg_ntl_b = _leg_pnls(
        pos, exit_price_a, exit_price_b,
    )

    row = {
        "pair": pos["pair"],
        "direction": pos["direction"],
        "entry_time_iso": pos["entry_time_iso"],
        "exit_time_iso": pos["exit_time_iso"],
        "elapsed_hours": f"{elapsed_hours:.3f}",
        "beta_entry": f"{pos['beta_entry']:.6f}",
        "half_life_hours": f"{pos['half_life_hours']:.3f}",
        "entry_price_a": f"{pos['entry_price_a']:.8f}",
        "entry_price_b": f"{pos['entry_price_b']:.8f}",
        "exit_price_a": f"{exit_price_a:.8f}",
        "exit_price_b": f"{exit_price_b:.8f}",
        "entry_spread": f"{pos['entry_spread']:.6f}",
        "exit_spread": f"{pos.get('exit_spread', 0.0):.6f}",
        "target_spread": f"{pos['target_spread']:.6f}",
        "stop_spread": f"{pos['stop_spread']:.6f}",
        "notional_usd": f"{pos['notional_usd']:.2f}",
        "leg_a_usd": f"{pos['leg_a_usd']:.2f}",
        "leg_b_usd": f"{pos['leg_b_usd']:.2f}",
        "pnl_usd": f"{pnl_usd:.4f}",
        "pnl_pct": f"{pnl_pct:.6f}",
        "exit_reason": pos.get("exit_reason", ""),
        "p_revert_at_entry": f"{pos.get('p_revert_at_entry', 0.0):.4f}",
        "exp_return_at_entry": (
            f"{pos['exp_return_at_entry']:.6f}" if "exp_return_at_entry" in pos else ""
        ),
        "funding_usd": f"{funding_usd:.6f}",
        "pnl_a_usd": f"{leg_pnl_a:.4f}",
        "pnl_b_usd": f"{leg_pnl_b:.4f}",
        "leg_a_pct": f"{leg_pnl_a / (leg_ntl_a + 1e-9):.6f}",
        "leg_b_pct": f"{leg_pnl_b / (leg_ntl_b + 1e-9):.6f}",
    }
    _append_row(TRADES_FILE, TRADES_COLUMNS, row)
