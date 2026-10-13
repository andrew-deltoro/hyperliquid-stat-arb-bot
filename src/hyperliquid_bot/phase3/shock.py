"""Single-leg shock detector: an *observable*, not a signal.

Empirical basis (``scripts/experiments/momentum_event_study.py``, 180d, 41
coins): after a 1h move >= 5% with volume-z >= 3, the next 1-4h of that same
leg is unpredictable and negative-EV net of round-trip cost in BOTH
directions (continuation t+1h -0.07%, fade t+1h -0.23%). The change-z
*reversion* rule was separately walk-forwarded and FAILED out of sample
(``scripts/experiments/walk_forward_change_z.py``: scatter -0.24%, test
+0.65%, 2/4 rolling 60d windows positive). So a shock is not an entry
signal.

What a shock *is* useful for is a state label: it tells us the pair's
cointegrating mean may be stale, and it tags every signal so the
post-shock -> forward-return relationship can be measured on our own book
before anyone gates live entries on it. ``block`` is therefore OFF by
default; with it off this module only annotates and journals.

The bot's own two RSR/TAO trades are the motivating example: the winner
(+$95.36) was 93% a single RSR leg repricing (+$98.32 on RSR, -$6.50 on
TAO), not spread reversion. The pair machinery happened to hold the
trending leg. We want that fact recorded on every trade, not inferred
after the fact from the blotter.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..data.ohlcv import fetch_candles


DEFAULTS = {
    "block": False,
    "move_min": 0.05,      # |1h log-ish return| that counts as a shock
    "vol_z_min": 3.0,      # volume z vs trailing window
    "vol_window": 168,     # 7d of hourly bars
    "block_hours": 6,      # cooldown after the most recent shock
}


@dataclass
class ShockState:
    coin: str
    last_ret: float
    vol_z: float
    hours_since: int | None   # bars since most recent qualifying shock (0 = this bar)
    is_shock: bool            # a qualifying shock printed on the latest bar
    magnitude: float          # |return| of the most recent qualifying shock


def _params(cfg) -> dict:
    raw = (cfg.signal.get("shock_guard", {}) or {}) if hasattr(cfg, "signal") else {}
    p = dict(DEFAULTS)
    p.update({k: v for k, v in raw.items() if k in DEFAULTS})
    return p


def compute_shock(candles, coin: str, params: dict | None = None) -> ShockState | None:
    """Shock features for one coin from its (closed) hourly candles."""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    win = int(p["vol_window"])
    if candles is None or len(candles) < win + 2:
        return None

    c = np.array([x.c for x in candles], dtype=float)
    v = np.array([x.v for x in candles], dtype=float)
    ret = np.zeros_like(c)
    ret[1:] = c[1:] / c[:-1] - 1.0

    # volume z per bar against the trailing window (no lookahead)
    vol_z = np.full_like(v, np.nan)
    for i in range(win, len(v)):
        w = v[i - win:i]
        sd = w.std()
        if sd > 0:
            vol_z[i] = (v[i] - w.mean()) / sd

    qualifying = (np.abs(ret) >= p["move_min"]) & (vol_z >= p["vol_z_min"])
    hours_since: int | None = None
    magnitude = 0.0
    q_idx = np.nonzero(qualifying)[0]
    if len(q_idx):
        last = int(q_idx[-1])
        hours_since = len(c) - 1 - last
        magnitude = float(abs(ret[last]))

    return ShockState(
        coin=coin,
        last_ret=float(ret[-1]),
        vol_z=float(vol_z[-1]) if np.isfinite(vol_z[-1]) else 0.0,
        hours_since=hours_since,
        is_shock=bool(qualifying[-1]),
        magnitude=magnitude,
    )


def pair_shock_state(
    coin_a: str,
    coin_b: str,
    cfg,
    interval: str = "1h",
    lookback_hours: int | None = None,
) -> tuple[ShockState | None, ShockState | None, dict]:
    """Shock state for both legs of a pair plus a small summary dict.

    The summary names the shocked side (``side`` = "A"/"B"/"AB"/None) and
    whether the cooldown gate would fire. Fetches only the two coins' candles;
    the caller has usually already fetched them for the cointegration fit, so
    this is two extra cheap HTTP calls per pair per cycle.
    """
    p = _params(cfg)
    lb = lookback_hours or int(cfg.data.get("lookback_hours", 720))
    sa = sb = None
    try:
        sa = compute_shock(fetch_candles(coin_a, interval, lb), coin_a, p)
        sb = compute_shock(fetch_candles(coin_b, interval, lb), coin_b, p)
    except Exception as e:  # noqa: BLE001 - never let annotation break entries
        print(f"[SHOCK] {coin_a}/{coin_b}: features unavailable ({e})")

    sides = [s for s, c in ((sa, coin_a), (sb, coin_b)) if s is not None and s.is_shock]
    names = []
    if sa is not None and sa.is_shock:
        names.append("A")
    if sb is not None and sb.is_shock:
        names.append("B")
    side = "".join(names) or None

    # True recency of the most recent qualifying shock on EITHER leg
    # (independent of the cooldown window), plus the cooldown decision.
    recent_all = [
        s.hours_since for s in (sa, sb)
        if s is not None and s.hours_since is not None
    ]
    hours_since = min(recent_all) if recent_all else None
    in_cooldown = hours_since is not None and hours_since <= int(p["block_hours"])
    summary = {
        "side": side,
        "is_shock": bool(sides),
        "hours_since": hours_since,
        "magnitude": max(
            [s.magnitude for s in (sa, sb) if s is not None] or [0.0]
        ),
        "in_cooldown": in_cooldown,
        "block": bool(p["block"]),
        "block_hours": int(p["block_hours"]),
    }
    return sa, sb, summary
