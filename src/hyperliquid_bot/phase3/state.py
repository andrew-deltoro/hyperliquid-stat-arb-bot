"""Atomic JSON state persistence for open positions."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
STATE_DIR = REPO_ROOT / "state"
POSITIONS_FILE = STATE_DIR / "positions.json"


@dataclass
class Position:
    pair: str                    # e.g. "LINK/PYTH"
    coin_a: str
    coin_b: str
    direction: str               # "LONG_A_SHORT_B" or "SHORT_A_LONG_B"
    beta_entry: float
    beta_sd_entry: float = 0.0
    entry_alpha: float
    entry_mean_raw: float
    entry_sigma_raw: float
    entry_spread: float          # centered spread at entry
    entry_price_a: float
    entry_price_b: float
    entry_time_iso: str
    half_life_hours: float
    target_spread: float         # exit when |spread| < this
    stop_spread: float           # exit if |spread| > this (adverse direction)
    time_stop_iso: str           # exit at this time regardless
    notional_usd: float
    leg_a_usd: float
    leg_b_usd: float
    status: str = "open"         # "open", "closed"
    exit_reason: Optional[str] = None
    exit_spread: Optional[float] = None
    exit_time_iso: Optional[str] = None


@dataclass
class State:
    positions: list = field(default_factory=list)

    def open_positions(self) -> list:
        return [p for p in self.positions if p["status"] == "open"]


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(path)


def load_state() -> State:
    if not POSITIONS_FILE.exists():
        return State(positions=[])
    with open(POSITIONS_FILE) as f:
        raw = json.load(f)
    return State(positions=raw.get("positions", []))


def save_state(state: State) -> None:
    _atomic_write_json(POSITIONS_FILE, {"positions": state.positions})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
