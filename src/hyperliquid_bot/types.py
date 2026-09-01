from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Network(str, Enum):
    MAINNET = "mainnet"
    TESTNET = "testnet"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class OrderIntent:
    coin: str
    side: Side
    size: float
    price: float | None = None
    reduce_only: bool = False
    post_only: bool = False
    tag: str = ""


@dataclass
class PositionSnapshot:
    coin: str
    size: float
    entry_price: float
    unrealized_pnl: float
    leverage: float


@dataclass
class RiskLimits:
    max_position_usd: float = 10_000.0
    max_daily_loss_usd: float = 500.0
    max_leverage: float = 5.0
    kill_switch: bool = False


@dataclass
class BotConfig:
    bot_id: str
    coin: str = "ETH"
    network: Network = Network.TESTNET
    size: float = 0.01
    risk: RiskLimits = field(default_factory=RiskLimits)
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Signal:
    action: str  # "long", "short", "flat", "hold"
    confidence: float = 1.0
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
