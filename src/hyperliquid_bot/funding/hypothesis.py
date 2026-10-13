"""Pure research helpers for the locked spot/perp funding-harvest test.

Nothing in this module signs actions, reads a wallet, or places an order.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics
from dataclasses import dataclass
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS oracle_snapshots (
    ts_ms INTEGER NOT NULL,
    coin TEXT NOT NULL,
    oracle_px REAL NOT NULL,
    funding_rate REAL NOT NULL,
    mark_px REAL NOT NULL,
    perp_day_volume REAL NOT NULL,
    PRIMARY KEY (ts_ms, coin)
);
CREATE TABLE IF NOT EXISTS funding_observations (
    coin TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    funding_rate REAL NOT NULL,
    PRIMARY KEY (coin, ts_ms)
);
CREATE TABLE IF NOT EXISTS decisions (
    entry_date TEXT NOT NULL,
    coin TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    eligible INTEGER NOT NULL,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL,
    PRIMARY KEY (entry_date, coin)
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date TEXT NOT NULL,
    coin TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_ts_ms INTEGER NOT NULL,
    planned_exit_ts_ms INTEGER NOT NULL,
    exit_ts_ms INTEGER,
    exit_reason TEXT,
    quantity REAL NOT NULL,
    entry_spot_px REAL NOT NULL,
    entry_perp_px REAL NOT NULL,
    exit_spot_px REAL,
    exit_perp_px REAL,
    entry_spot_notional REAL NOT NULL,
    entry_perp_notional REAL NOT NULL,
    exit_spot_notional REAL,
    exit_perp_notional REAL,
    fees_usd REAL NOT NULL,
    funding_usd REAL,
    net_pnl_usd REAL,
    valid INTEGER NOT NULL DEFAULT 1,
    invalid_reason TEXT,
    UNIQUE (entry_date, coin)
);
"""


@dataclass(frozen=True)
class Fill:
    quantity: float
    notional: float
    average_price: float
    worst_price: float


def init_db(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def walk_book(levels: list[dict], quantity: float) -> Fill | None:
    """Walk one side of an L2 book for an exact base-asset quantity."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    left = quantity
    notional = 0.0
    worst = 0.0
    for level in levels:
        px, available = float(level["px"]), float(level["sz"])
        if px <= 0 or available <= 0:
            continue
        take = min(left, available)
        notional += take * px
        worst = px
        left -= take
        if left <= quantity * 1e-12:
            return Fill(quantity, notional, notional / quantity, worst)
    return None


def quantize_quantity(quantity: float, decimals: int) -> float:
    scale = 10**decimals
    return math.floor(quantity * scale + 1e-12) / scale


def book_mid(book: dict) -> float:
    bids, asks = book["levels"]
    return (float(bids[0]["px"]) + float(asks[0]["px"])) / 2.0


def funding_cashflow_short(quantity: float, oracle_px: float, funding_rate: float) -> float:
    """Positive Hyperliquid funding is received by the short."""
    return quantity * oracle_px * funding_rate


def four_leg_pnl(
    quantity: float,
    entry_spot_px: float,
    entry_perp_px: float,
    exit_spot_px: float,
    exit_perp_px: float,
    funding_usd: float,
    fees_usd: float,
) -> float:
    spot = quantity * (exit_spot_px - entry_spot_px)
    short_perp = quantity * (entry_perp_px - exit_perp_px)
    return spot + short_perp + funding_usd - fees_usd


def fee_for_fill(notional: float, fee_rate: float) -> float:
    return abs(notional) * fee_rate


def persistently_positive(
    observations: list[tuple[int, float]],
    start_ms: int,
    end_ms: int,
    minimum: int,
) -> tuple[bool, str]:
    rates = [rate for ts, rate in observations if start_ms <= ts <= end_ms]
    if len(rates) < minimum:
        return False, f"only {len(rates)} funding prints; need {minimum}"
    if any(rate <= 0 for rate in rates):
        return False, "funding was not positive at every observed hour"
    return True, "preceding funding prints were all positive"


def closest_oracle(
    snapshots: list[tuple[int, float]], target_ms: int, tolerance_ms: int
) -> float | None:
    if not snapshots:
        return None
    ts, px = min(snapshots, key=lambda row: abs(row[0] - target_ms))
    return px if abs(ts - target_ms) <= tolerance_ms else None


def cluster_by_entry_date(rows: list[dict]) -> list[float]:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(str(row["entry_date"]), []).append(float(row["net_pnl_usd"]))
    return [sum(values) / len(values) for _, values in sorted(grouped.items())]


def posterior_summary(
    cluster_means: list[float],
    draws: int = 50_000,
    seed: int = 20261009,
    prior: dict | None = None,
) -> dict:
    """Conjugate Normal-Inverse-Gamma model over independent entry dates.

    The prior is centered at zero net edge. The defaults imply a broad
    approximately $4 prior scale for the unknown mean of a $500 paper leg.
    """
    prior = prior or {"mu": 0.0, "kappa": 0.25, "alpha": 2.0, "beta": 4.0}
    x = [float(value) for value in cluster_means]
    n = len(x)
    k0, m0 = float(prior["kappa"]), float(prior["mu"])
    a0, b0 = float(prior["alpha"]), float(prior["beta"])
    mean = statistics.fmean(x) if n else 0.0
    ss = sum((value - mean) ** 2 for value in x)
    kn = k0 + n
    mn = (k0 * m0 + n * mean) / kn
    an = a0 + n / 2.0
    bn = b0 + 0.5 * ss + (k0 * n * (mean - m0) ** 2) / (2.0 * kn)

    rng = random.Random(seed)
    mu = []
    predictive = []
    for _ in range(draws):
        variance = 1.0 / rng.gammavariate(an, 1.0 / bn)
        mean_draw = rng.gauss(mn, math.sqrt(variance / kn))
        mu.append(mean_draw)
        predictive.append(rng.gauss(mean_draw, math.sqrt(variance)))

    def quantiles(values: list[float]) -> list[float]:
        values = sorted(values)
        return [
            values[round((len(values) - 1) * probability)]
            for probability in (0.05, 0.95)
        ]

    return {
        "model": "normal_inverse_gamma_on_entry_date_means",
        "independent_entry_dates": n,
        "posterior_mean_usd_per_trade": statistics.fmean(mu),
        "posterior_mean_90pct": quantiles(mu),
        "probability_mean_gt_zero": sum(value > 0 for value in mu) / draws,
        "predictive_new_date_90pct": quantiles(predictive),
        "probability_new_date_gt_zero": sum(value > 0 for value in predictive) / draws,
        "prior": prior,
    }


def json_dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
