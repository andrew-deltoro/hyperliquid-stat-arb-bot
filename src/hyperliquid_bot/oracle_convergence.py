"""Pure helpers for the paper-only perp/oracle convergence experiment."""
from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS cohorts (
    cohort_hour TEXT PRIMARY KEY,
    evaluation_ts_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    eligible_assets INTEGER NOT NULL,
    tail_size INTEGER NOT NULL,
    completed_ts_ms INTEGER
);
CREATE TABLE IF NOT EXISTS rankings (
    cohort_hour TEXT NOT NULL,
    coin TEXT NOT NULL,
    rank INTEGER NOT NULL,
    oracle_px REAL NOT NULL,
    impact_bid REAL NOT NULL,
    impact_ask REAL NOT NULL,
    premium REAL NOT NULL,
    day_volume REAL NOT NULL,
    selected_side TEXT,
    PRIMARY KEY (cohort_hour, coin)
);
CREATE TABLE IF NOT EXISTS positions (
    cohort_hour TEXT NOT NULL,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_ts_ms INTEGER NOT NULL,
    due_ts_ms INTEGER NOT NULL,
    exit_ts_ms INTEGER,
    quantity REAL NOT NULL,
    entry_px REAL NOT NULL,
    entry_oracle REAL NOT NULL,
    entry_notional REAL NOT NULL,
    exit_px REAL,
    exit_oracle REAL,
    exit_notional REAL,
    fees_usd REAL NOT NULL,
    gross_pnl_usd REAL,
    net_pnl_usd REAL,
    entry_basis_bps REAL NOT NULL,
    exit_basis_bps REAL,
    convergence_bps REAL,
    valid INTEGER NOT NULL DEFAULT 1,
    invalid_reason TEXT,
    PRIMARY KEY (cohort_hour, coin)
);
CREATE TABLE IF NOT EXISTS path_observations (
    cohort_hour TEXT NOT NULL,
    coin TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    elapsed_minutes REAL NOT NULL,
    oracle_px REAL NOT NULL,
    executable_exit_px REAL NOT NULL,
    exit_basis_bps REAL NOT NULL,
    convergence_bps REAL NOT NULL,
    gross_pnl_if_exit_usd REAL NOT NULL,
    net_pnl_if_exit_usd REAL NOT NULL,
    PRIMARY KEY (cohort_hour, coin, ts_ms)
);
"""


def init_db(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def impact_premium(oracle: float, impact_bid: float, impact_ask: float) -> float:
    if oracle <= 0 or impact_bid <= 0 or impact_ask <= 0:
        raise ValueError("prices must be positive")
    return ((impact_bid + impact_ask) / 2.0 - oracle) / oracle


def rank_tails(rows: list[dict], fraction: float) -> list[dict]:
    """Rank by premium and mark non-overlapping cheap/rich tails."""
    if not 0 < fraction <= 0.5:
        raise ValueError("fraction must be in (0, 0.5]")
    ranked = sorted(rows, key=lambda row: (row["premium"], row["coin"]))
    tail = max(1, int(len(ranked) * fraction))
    if tail * 2 > len(ranked):
        raise ValueError("not enough rows for non-overlapping tails")
    out = []
    for index, row in enumerate(ranked):
        value = dict(row)
        value["rank"] = index + 1
        value["selected_side"] = (
            "long" if index < tail else "short" if index >= len(ranked) - tail else None
        )
        out.append(value)
    return out


def position_pnl(side: str, quantity: float, entry_px: float, exit_px: float) -> float:
    if side == "long":
        return quantity * (exit_px - entry_px)
    if side == "short":
        return quantity * (entry_px - exit_px)
    raise ValueError("side must be long or short")


def favorable_edge_bps(side: str, fill_px: float, oracle_px: float) -> float:
    """Positive when the executable fill is cheap for a long or rich for a short."""
    basis = (fill_px / oracle_px - 1.0) * 10_000
    if side == "long":
        return -basis
    if side == "short":
        return basis
    raise ValueError("side must be long or short")


def convergence_bps(
    side: str,
    entry_px: float,
    entry_oracle: float,
    exit_px: float,
    exit_oracle: float,
) -> float:
    """Positive means the traded premium moved in the hypothesized direction."""
    entry_basis = entry_px / entry_oracle - 1.0
    exit_basis = exit_px / exit_oracle - 1.0
    if side == "short":
        return (entry_basis - exit_basis) * 10_000
    if side == "long":
        return (exit_basis - entry_basis) * 10_000
    raise ValueError("side must be long or short")
