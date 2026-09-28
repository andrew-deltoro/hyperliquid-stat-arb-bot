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
]


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
]


def _ensure_file(path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(columns)


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
    """Append one signal to signals.csv (including SKIP decisions)."""
    skip_reason = ""
    if sig.action == "SKIP":
        if not sig.converged_coint:
            skip_reason = "coint_not_converged"
        elif not sig.converged_ou:
            skip_reason = "ou_not_converged"
        elif abs(sig.z) < 1.5:
            skip_reason = "z_too_small"
        elif sig.edge < 0.05:
            skip_reason = "edge_too_small"
        elif sig.notional <= 0:
            skip_reason = "notional_zero"
        else:
            skip_reason = "unknown"

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
    }
    _append_row(SIGNALS_FILE, SIGNALS_COLUMNS, row)


def compute_pnl(pos: dict, exit_price_a: float, exit_price_b: float) -> tuple[float, float]:
    """
    Compute realized P&L in USD and as a percentage of deployed notional.

    Direction:
      LONG_A_SHORT_B: long A, short B
      SHORT_A_LONG_B: short A, long B

    Returns (pnl_usd, pnl_pct).
    """
    sign_a = 1.0 if pos["direction"] == "LONG_A_SHORT_B" else -1.0
    sign_b = -sign_a

    entry_a = pos["entry_price_a"]
    entry_b = pos["entry_price_b"]

    # Notional per leg — use stored values
    leg_a = pos["leg_a_usd"]
    leg_b = pos["leg_b_usd"]

    # Simple P&L on each leg (no fees, no funding for now)
    pnl_a = leg_a * sign_a * (exit_price_a / entry_a - 1.0)
    pnl_b = leg_b * sign_b * (exit_price_b / entry_b - 1.0)

    pnl_usd = pnl_a + pnl_b
    pnl_pct = pnl_usd / (pos["notional_usd"] + 1e-9)
    return pnl_usd, pnl_pct


def log_trade(pos: dict, exit_price_a: float, exit_price_b: float) -> None:
    """Append a completed trade (entry + exit) to trades.csv."""
    entry_dt = datetime.fromisoformat(pos["entry_time_iso"])
    exit_dt = datetime.fromisoformat(pos["exit_time_iso"])
    elapsed_hours = (exit_dt - entry_dt).total_seconds() / 3600.0

    pnl_usd, pnl_pct = compute_pnl(pos, exit_price_a, exit_price_b)

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
    }
    _append_row(TRADES_FILE, TRADES_COLUMNS, row)
