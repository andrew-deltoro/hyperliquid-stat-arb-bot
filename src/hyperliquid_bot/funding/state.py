"""
Funding position state.

Separate from stat-arb state — funding positions have a different lifecycle:
single-coin delta-neutral (long spot + short perp on the same asset).
"""
from __future__ import annotations
import os, json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Resolve state dir — prefer the runtime one if it has our siblings
_CANDIDATES = [
    Path("/home/hlbot/hlbot-runtime/live/state"),
    Path("/home/hlbot/hyperliquid-stat-arb-bot/state"),
]


def _state_dir() -> Path:
    for p in _CANDIDATES:
        if p.exists() and (p / "positions.json").exists():
            return p
    return _CANDIDATES[0]


STATE_DIR = _state_dir()
POSITIONS_FILE = STATE_DIR / "funding_positions.json"


@dataclass
class FundingPosition:
    coin: str
    spot_notional_usd: float
    perp_notional_usd: float
    entry_funding_pct: float
    entry_spot_px: float
    entry_perp_px: float
    entry_time_iso: str
    max_hold_hours: int
    target_funding_pct: float
    exit_reason: Optional[str] = None
    exit_time_iso: Optional[str] = None
    accumulated_funding_usd: float = 0.0
    status: str = "open"


@dataclass
class FundingState:
    positions: list = field(default_factory=list)

    def open_positions(self) -> list:
        return [p for p in self.positions if p.get("status") == "open"]


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(path)


def load_funding_state() -> FundingState:
    if not POSITIONS_FILE.exists():
        return FundingState(positions=[])
    with open(POSITIONS_FILE) as f:
        raw = json.load(f)
    return FundingState(positions=raw.get("positions", []))


def save_funding_state(state: FundingState) -> None:
    _atomic_write(POSITIONS_FILE, {"positions": state.positions})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
