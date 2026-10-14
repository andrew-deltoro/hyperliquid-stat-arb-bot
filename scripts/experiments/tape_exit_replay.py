#!/usr/bin/env python
"""
Exit counterfactual on the bot's OWN realized tape.

Why this exists
---------------
The walk-forward studies (experiment_exit_comparison.py,
experiment_full_config_validation.py) re-implement the entry gate and the exit
rules by hand and report gross spread points. That makes them unable to answer
an exit question about the live book, for three measured reasons:

  1. They enter on |z| >= 1.0 alone. Live also requires the EG stationarity
     gate, P(kappa) > 0.80 and HL <= 96h. In the live journal 1,411 of 1,794
     signals were SKIP; the top vetoes were not_stationary and z_too_small.
     So the backtest trades a population the bot refuses.
  2. The exit that fires most in live (beta_shift) is absent from the exit
     sweeps entirely.
  3. They report gross. journal.compute_pnl charges 15 bps of gross leg
     notional round-trip, and no live trade has a measured fee yet.

What this does instead
----------------------
It does NOT re-fit the model (a production NUTS fit measured 21.7 s, so a
6-month refit replay is ~5 days of compute). Every quantity the replay needs
was already published by the bot while the trade was live:

  positions.json  entry_alpha, entry_mean_raw, entry_sigma_raw, time_stop_iso,
                  beta_entry, beta_sd_entry, entry geometry, direction
  signals.csv     the per-cycle beta / beta_sd / exp_return refits
  mainnet candles the realized price path after entry

and it drives the bot's OWN code: PositionManager.evaluate_open, which routes
the beta-shift trigger through leverage.simulate_trade_returns for the
posterior-predictive economics gate. No exit rule is re-implemented here, so
the replay cannot drift from live.

Known limit that shaped the conclusions
---------------------------------------
signals.csv only carries exp_return from 2026-10-07. Cycles before that have a
row but a blank exp_return, and the replay substitutes the entry's value. So
the beta-shift economics gate cannot be evaluated on pre-Oct-7 episodes and
they replay as time_stop. Exit-reason agreement is therefore 8/23 (of the
15 misses, 7 are not exit-rule events: state_reconciled / manual_flatten /
operator_close). Conclusions that depend on the gate are drawn only from
post-Oct-7 episodes, or cross-checked against the recorded trade timestamps,
which is what refuted the initial "mislabelled time_stop" reading.

Clock fidelity
--------------
The live unit is scheduled at 00/04/08/12/16/20 UTC and acts on the latest
CLOSED bar. So the cycle consuming bar T runs at T + 1h, and only bars stamped
03/07/11/15/19/23 map onto a grid slot. Refit values are looked up by
(date, slot-within-day), not by walk-forward proximity, which is what stops
cycle-hour drift from contaminating the exit ordering.

Baked-at-entry geometry
-----------------------
`stop_spread` and `time_stop_iso` are written into the position record at open
and read back by evaluate_open. Config patches cannot reach them, so variants
that change them do so explicitly. Only `cross_exit_frac` and the beta-shift sd
threshold are read from config at evaluation time.

Safety: the replay is read-only. _close, save_state, upsert_position and the
executor are all stubbed, so it can never place an order, write positions.json,
or append to the live journal.

Spread points convert to return on position notional with the bot's own
geometry: dPnL = (N/2) * dS  =>  return = 0.5 * sign * (exit - entry).

Run: .venv/bin/python scripts/experiments/tape_exit_replay.py
     .venv/bin/python scripts/experiments/tape_exit_replay.py --sweep
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from src.hyperliquid_bot.data import ohlcv  # noqa: E402
from src.hyperliquid_bot.phase3 import positions as P  # noqa: E402
from src.hyperliquid_bot.phase3.config import load_config  # noqa: E402
from src.hyperliquid_bot.phase3.executor import BaseExecutor  # noqa: E402
from src.hyperliquid_bot.phase3.positions import PositionManager  # noqa: E402

LIVE = Path("/home/hlbot/hlbot-runtime/live")
CONFIG = REPO / "config" / "phase3-live.yaml"

CYCLE_HOURS = 4
SLOTS_PER_DAY = 24 // CYCLE_HOURS          # 6 grid slots: 00/04/08/12/16/20
ACT_HOUR = CYCLE_HOURS - 1                 # the bar a cycle acts on (03, 07, ...)
BAR_MS = 3_600_000
MAX_ENTRY_GAP_H = 6.0
PATH_LIMIT_H = 24 * 60


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------
def _f(row: dict | None, key: str, default: float = 0.0) -> float:
    v = (row or {}).get(key)
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def load_json_positions() -> list[dict]:
    return json.loads((LIVE / "state" / "positions.json").read_text())["positions"]


def load_csv(name: str) -> list[dict]:
    path = LIVE / "reports" / name
    if not path.exists():
        return []
    with path.open() as fh:
        return list(csv.DictReader(fh))


@dataclass
class Entry:
    pair: str
    coin_a: str
    coin_b: str
    direction: str
    alpha: float
    mean_raw: float
    sigma_raw: float
    entry_spread: float
    beta_entry: float
    beta_sd_entry: float
    entry_time: datetime
    time_stop: datetime
    target_spread: float
    stop_spread: float
    recorded_reason: str
    recorded_pnl_usd: float
    recorded_pnl_pct: float
    notional_usd: float
    leg_usd: float
    half_life_hours: float
    p_revert: float
    exp_return: float
    sigma_ou: float
    nu_ou: float
    kappa: float
    status: str
    original: dict = field(default_factory=dict)
    spread_path: np.ndarray = field(default_factory=lambda: np.empty(0))
    beta_path: np.ndarray = field(default_factory=lambda: np.empty(0))
    beta_sd_path: np.ndarray = field(default_factory=lambda: np.empty(0))
    exp_return_path: np.ndarray = field(default_factory=lambda: np.empty(0))
    ts: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int64))
    samples: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int64))
    t0_idx: int = 0
    date: str = ""

    @property
    def sign(self) -> float:
        return 1.0 if self.direction == "LONG_A_SHORT_B" else -1.0

    def ret(self, exit_spread: float) -> float:
        return 0.5 * self.sign * (exit_spread - self.entry_spread)


def _signal_near(signals: list[dict], pair: str, when: datetime) -> dict | None:
    best, gap = None, None
    for row in signals:
        if row["pair"] != pair or row["action"] != "OPEN":
            continue
        d = abs((datetime.fromisoformat(row["timestamp_iso"]) - when).total_seconds())
        if gap is None or d < gap:
            best, gap = row, d
    if best is None or gap > MAX_ENTRY_GAP_H * 3600:
        return None
    return best


REQUIRED = ("entry_alpha", "entry_mean_raw", "entry_sigma_raw", "entry_spread",
            "beta_entry", "time_stop_iso", "entry_time_iso")


def build_entries(positions: list[dict], signals: list[dict],
                  trades: list[dict]) -> tuple[list[Entry], list[str]]:
    trades_by_pair: dict[str, list[dict]] = {}
    for t in trades:
        if t.get("entry_time_iso"):
            trades_by_pair.setdefault(t["pair"], []).append(t)

    entries, skipped = [], []
    for pos in positions:
        pair = pos["pair"]
        if pos.get("status") not in ("open", "closed"):
            skipped.append(f"{pair}: status={pos.get('status')}")
            continue
        if any(k not in pos for k in REQUIRED):
            skipped.append(f"{pair}: missing entry coordinates "
                           f"({[k for k in REQUIRED if k not in pos]})")
            continue
        entry_time = datetime.fromisoformat(pos["entry_time_iso"])
        sig = _signal_near(signals, pair, entry_time)
        if sig is None:
            skipped.append(f"{pair}: no OPEN signal within {MAX_ENTRY_GAP_H:g}h")
            continue
        rec = min(trades_by_pair.get(pair, []),
                  key=lambda t: abs((datetime.fromisoformat(t["entry_time_iso"])
                                     - entry_time).total_seconds()),
                  default=None)
        entries.append(Entry(
            pair=pair, coin_a=pos["coin_a"], coin_b=pos["coin_b"],
            direction=pos["direction"],
            alpha=float(pos["entry_alpha"]),
            mean_raw=float(pos["entry_mean_raw"]),
            sigma_raw=float(pos["entry_sigma_raw"]),
            entry_spread=float(pos["entry_spread"]),
            beta_entry=float(pos["beta_entry"]),
            beta_sd_entry=float(pos.get("beta_sd_entry") or 0.0),
            entry_time=entry_time,
            time_stop=datetime.fromisoformat(pos["time_stop_iso"]),
            target_spread=float(pos.get("target_spread") or 0.0),
            stop_spread=float(pos.get("stop_spread") or 0.0),
            recorded_reason=pos.get("exit_reason") or "still_open",
            recorded_pnl_usd=_f(rec, "pnl_usd"),
            recorded_pnl_pct=_f(rec, "pnl_pct"),
            notional_usd=float(pos.get("notional_usd") or 0.0),
            leg_usd=abs(float(pos.get("leg_a_usd") or 0.0))
            + abs(float(pos.get("leg_b_usd") or 0.0)),
            half_life_hours=float(pos.get("half_life_hours") or 0.0),
            p_revert=_f(sig, "p_revert", 0.5),
            exp_return=_f(sig, "exp_return", 0.0),
            sigma_ou=_f(sig, "sigma_ou", 0.0),
            nu_ou=max(_f(sig, "nu_ou", 8.0), 2.01),
            kappa=_f(sig, "kappa", 0.0),
            status=pos.get("status", "?"),
            original=pos,
        ))
    return entries, skipped


# --------------------------------------------------------------------------
# realized prices + cycle sampling
# --------------------------------------------------------------------------
def _hourly(coin: str, start: datetime, end: datetime) -> dict[int, float]:
    candles = ohlcv._fetch_chunk(coin, "1h", int(start.timestamp() * 1000),
                                int(end.timestamp() * 1000))
    return {c.t: c.c for c in candles}


def _grid_key(ms: int) -> int:
    """Floor a timestamp to the live cycle grid (00/04/08/12/16/20 UTC)."""
    hour_ms = 3_600_000
    day_ms = 24 * hour_ms
    day = ms // day_ms
    hour = (ms % day_ms) // hour_ms
    return day * day_ms + (hour // CYCLE_HOURS) * CYCLE_HOURS * hour_ms


def attach_paths(entries: list[Entry], signals: list[dict],
                 verbose: bool = True) -> None:
    by_pair: dict[str, list[dict]] = {}
    for row in signals:
        by_pair.setdefault(row["pair"], []).append(row)

    # Exact grid-slot lookup: {grid_key: latest refit row at or before it}.
    grid_by_pair: dict[str, dict[int, dict]] = {}
    for pair, rows in by_pair.items():
        table: dict[int, dict] = {}
        for row in sorted(rows, key=lambda r: r["timestamp_iso"]):
            key = _grid_key(int(datetime.fromisoformat(
                row["timestamp_iso"]).timestamp() * 1000))
            table[key] = row          # later row at the same slot wins
        grid_by_pair[pair] = table

    now = datetime.now(timezone.utc)
    for e in entries:
        start = e.entry_time - timedelta(hours=2)
        ca = _hourly(e.coin_a, start, now)
        cb = _hourly(e.coin_b, start, now)
        shared = sorted(set(ca) & set(cb))[:PATH_LIMIT_H + 4]
        t0 = int(e.entry_time.timestamp() // 3600 * 3600) * 1000

        ts, sp = [], []
        for t in shared:
            raw = math.log(ca[t]) - e.alpha - e.beta_entry * math.log(cb[t])
            ts.append(t)
            sp.append(raw - e.mean_raw)
        e.spread_path = np.asarray(sp, dtype=float)
        e.ts = np.asarray(ts, dtype=np.int64)
        e.t0_idx = next((i for i, t in enumerate(ts) if t >= t0), 0)

        # The cycle acting on bar T runs at T + 1h; only bars whose hour is
        # ACT_HOUR mod CYCLE_HOURS land on the live timer grid. Look each cycle
        # up by its own timestamp, so cycle-hour drift cannot shift the series.
        table = grid_by_pair.get(e.pair, {})
        samples, beta, sd, er, keys = [], [], [], [], sorted(table)
        for i, t in enumerate(ts):
            if int(t // 3_600_000) % CYCLE_HOURS != ACT_HOUR:
                continue
            gkey = _grid_key(int(t) + BAR_MS)
            row = table.get(gkey)
            if row is None and table:
                # No refit published for this slot (bot was up but idle, or the
                # slot predates the panel): hold the last published refit.
                prev = [k for k in keys if k <= gkey]
                row = table[prev[-1]] if prev else table[keys[0]]
            samples.append(i)
            beta.append(_f(row, "beta", e.beta_entry) if row else e.beta_entry)
            sd.append(_f(row, "beta_sd", e.beta_sd_entry) if row else e.beta_sd_entry)
            er.append(_f(row, "exp_return", e.exp_return) if row else e.exp_return)
        e.samples = np.asarray(samples, dtype=np.int64)
        e.beta_path = np.asarray(beta, dtype=float)
        e.beta_sd_path = np.asarray(sd, dtype=float)
        e.exp_return_path = np.asarray(er, dtype=float)
        e.date = e.entry_time.date().isoformat()

        if verbose:
            drift = (e.spread_path[e.t0_idx] - e.entry_spread
                     if len(e.spread_path) > e.t0_idx else float("nan"))
            print(f"  {e.pair:<16}{e.status:<7}bars={len(e.spread_path):>5} "
                  f"cycles={len(e.samples):>4} rec={e.recorded_reason:<16} "
                  f"pnl=${e.recorded_pnl_usd:>+7.2f} drift@entry={drift:+.4f}")


# --------------------------------------------------------------------------
# replay (read-only)
# --------------------------------------------------------------------------
class _Clock(datetime):
    """datetime with a pinned .now(), so the time stop sees simulated time."""
    _pinned: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls._pinned if tz is None else cls._pinned.astimezone(tz)


class _NoopExecutor(BaseExecutor):
    verifies_positions = False

    def place_order(self, order, ref_price):  # pragma: no cover
        raise AssertionError("replay must never place orders")

    def lot_size(self, coin: str) -> float:
        return 1e-9


def _install_safety() -> None:
    """Make the replay structurally incapable of touching live state."""
    P.save_state = lambda *a, **k: None
    P.upsert_position = lambda *a, **k: None
    P.now_iso = lambda: "replay"


def _pm_for(cfg, pos: dict) -> PositionManager:
    from types import MethodType

    pm = PositionManager.__new__(PositionManager)
    pm.cfg = cfg
    pm.executor = _NoopExecutor(cfg)
    pm.log_fn = lambda *a, **k: None
    pm.state = type("S", (), {"positions": [pos]})()

    def _close(cls, pos_, exit_spread, exit_beta, reason, **kw):
        return {"action": "closed", "reason": reason, "position": pos_}

    pm._close = MethodType(_close, pm)
    return pm


def _cfg_with(cfg, patch: dict):
    if not patch:
        return cfg
    raw = copy.deepcopy(cfg.raw)
    pos = dict(raw.get("position", {}))
    pos.update(patch)
    raw["position"] = pos
    return type(cfg)(
        pairs=cfg.pairs, data=cfg.data, model=cfg.model, signal=cfg.signal,
        position=pos, risk=cfg.risk, ou_prior=cfg.ou_prior, raw=raw,
    )


@dataclass
class Variant:
    name: str
    cfg_patch: dict = field(default_factory=dict)
    time_stop_mult: float | None = None    # overrides baked time_stop_iso
    stop_mult: float | None = None         # overrides baked stop_spread
    disable_beta_shift: bool = False


def replay(entry: Entry, cfg, variant: Variant) -> dict:
    """Step the realized path through the bot's real evaluate_open."""
    if len(entry.samples) == 0:
        return {"reason": "no_path", "ret": float("nan"), "bars": 0,
                "exit_spread": float("nan")}

    pos = copy.deepcopy(entry.original)
    # evaluate_open finds the position via find_open(), which filters on status.
    pos["status"] = "open"

    # Baked-at-entry geometry: evaluate_open reads these off the position.
    if variant.time_stop_mult is not None:
        pos["time_stop_iso"] = (
            entry.entry_time
            + timedelta(hours=entry.half_life_hours * variant.time_stop_mult)
        ).isoformat()
    if variant.stop_mult is not None:
        pos["stop_spread"] = variant.stop_mult * entry.entry_spread
    if variant.disable_beta_shift:
        pos["beta_sd_entry"] = 0.0     # gate -> no sd -> no trigger

    use_cfg = _cfg_with(cfg, variant.cfg_patch)
    pm = _pm_for(use_cfg, pos)

    for k, i in enumerate(entry.samples):
        i = int(i)
        if i <= entry.t0_idx:
            continue
        s = float(entry.spread_path[i])
        fresh_beta = float(entry.beta_path[k])
        fresh_sd = float(entry.beta_sd_path[k])
        fresh_exp = float(entry.exp_return_path[k])
        if variant.disable_beta_shift or fresh_beta == entry.beta_entry:
            fresh_beta = None
        if variant.disable_beta_shift:
            fresh_sd = None
        if np.isnan(fresh_exp) or fresh_exp == 0.0:
            fresh_exp = None

        _Clock._pinned = datetime.fromtimestamp(
            (int(entry.ts[i]) + BAR_MS) / 1000, tz=timezone.utc)
        real_dt = P.datetime
        P.datetime = _Clock
        try:
            # Invert the entry frame with price_b = 1 so the spread the bot
            # computes equals the tape value exactly.
            price_a = math.exp(s + entry.mean_raw + entry.alpha)
            res = pm.evaluate_open(
                entry.pair, price_a, 1.0,
                fresh_beta=fresh_beta, fresh_beta_sd=fresh_sd,
                fresh_exp_return=fresh_exp,
            )
        finally:
            P.datetime = real_dt

        if res.get("action") == "closed":
            return {"reason": res["reason"], "ret": entry.ret(s),
                    "bars": i - entry.t0_idx, "exit_spread": s}

    s = float(entry.spread_path[-1])
    return {"reason": "open_at_path_end", "ret": entry.ret(s),
            "bars": len(entry.spread_path) - 1 - entry.t0_idx, "exit_spread": s}


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------
def block_bootstrap(diffs: np.ndarray, dates: list[str], n_iter: int = 4000,
                    seed: int = 7) -> dict:
    groups: dict[str, list[float]] = {}
    for d, v in zip(dates, diffs):
        if np.isfinite(v):
            groups.setdefault(d, []).append(float(v))
    if len(groups) < 3:
        return {"n_days": len(groups), "lo": float("nan"), "hi": float("nan"),
                "p_le_zero": float("nan")}
    keys = list(groups)
    vals = [np.asarray(groups[k]) for k in keys]
    rng = np.random.default_rng(seed)
    means = np.empty(n_iter)
    for i in range(n_iter):
        pick = rng.integers(0, len(keys), size=len(keys))
        means[i] = np.concatenate([vals[p] for p in pick]).mean()
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {"n_days": len(keys), "lo": float(lo), "hi": float(hi),
            "p_le_zero": float(np.mean(means <= 0))}


def concentration(rets: np.ndarray) -> dict:
    r = rets[np.isfinite(rets)]
    if r.size == 0:
        return {"best_share": float("nan"), "mean_ex_best": float("nan")}
    total, best = float(r.sum()), float(r.max())
    return {"best_share": best / total if total else float("nan"),
            "mean_ex_best": (total - best) / max(r.size - 1, 1)}


VARIANTS = [
    Variant("live rule"),
    Variant("cross_frac 0.50", cfg_patch={"cross_exit_frac": 0.50}),
    Variant("cross_frac 1.00", cfg_patch={"cross_exit_frac": 1.00}),
    Variant("cross_frac 1.50", cfg_patch={"cross_exit_frac": 1.50}),
    Variant("beta_shift OFF", disable_beta_shift=True),
    Variant("time stop x1.5", time_stop_mult=3.33 * 1.5),
    Variant("time stop x5.0", time_stop_mult=3.33 * 5.0),
    Variant("stop_mult 2.0", stop_mult=2.0),
    Variant("stop_mult 4.0", stop_mult=4.0),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    _install_safety()
    cfg = load_config(CONFIG)
    print(f"exit_mode={cfg.position.get('exit_mode')} "
          f"cross_frac={cfg.position.get('cross_exit_frac')} "
          f"stop_mult={cfg.position.get('stop_mult')} "
          f"horizon_mult={cfg.position.get('exit_horizon_mult')}\n")

    positions = load_json_positions()
    signals = load_csv("signals.csv")
    trades = load_csv("trades.csv")
    entries, skipped = build_entries(positions, signals, trades)

    closed = sum(1 for e in entries if e.status == "closed")
    print(f"Replayable episodes: {len(entries)} "
          f"(from {len(positions)} position records; {closed} closed)")
    for s in skipped:
        print(f"  skip {s}")
    armed = sum(1 for e in entries if e.beta_sd_entry > 0)
    print(f"  beta_shift gate armed on {armed}/{len(entries)} "
          f"(needs beta_sd_entry > 0)\n")

    attach_paths(entries, signals)

    print("\n" + "=" * 80)
    print("VALIDATION: does the replay reproduce the bot's recorded exit reason?")
    print("=" * 80)
    base = [replay(e, cfg, VARIANTS[0]) for e in entries]
    agree = 0
    print(f"{'pair':<18}{'status':<8}{'recorded':<20}{'replayed':<20}{'match':<6}")
    for e, b in zip(entries, base):
        ok = b["reason"] == e.recorded_reason
        agree += ok
        print(f"{e.pair:<18}{e.status:<8}{e.recorded_reason:<20}"
              f"{b['reason']:<20}{'yes' if ok else 'NO':<6}")
    print(f"\nexact-reason agreement {agree}/{len(entries)}")
    base_ret = np.array([b["ret"] for b in base], dtype=float)
    rec_ret = np.array([e.recorded_pnl_pct for e in entries], dtype=float)
    print(f"replayed mean {base_ret.mean()*1e4:+.1f} bp/trade (gross, spread frame)")
    print(f"recorded mean {rec_ret.mean()*1e4:+.1f} bp/trade "
          f"(realized, includes 15 bps modeled cost + funding)")

    if not args.sweep:
        print("\n(run with --sweep for the paired exit counterfactual)")
        return

    print("\n" + "=" * 80)
    print("PAIRED EXIT COUNTERFACTUAL  (identical entries; diff vs live rule)")
    print("=" * 80)
    dates = [e.date for e in entries]
    c0 = concentration(base_ret)
    print(f"{'variant':<18}{'mean bp':>9}{'diff bp':>9}{'95% CI':>19}"
          f"{'P(d<=0)':>9}{'best%':>8}{'ex-best bp':>12}")
    print(f"{'live rule':<18}{base_ret.mean()*1e4:>+9.1f}{0.0:>+9.1f}{'':>19}"
          f"{'':>9}{c0['best_share']*100:>7.0f}%{c0['mean_ex_best']*1e4:>+11.1f}")

    for v in VARIANTS[1:]:
        rets = np.array([replay(e, cfg, v)["ret"] for e in entries], dtype=float)
        d = rets - base_ret
        bs = block_bootstrap(d, dates)
        c = concentration(rets)
        ci = (f"[{bs['lo']*1e4:+.1f}, {bs['hi']*1e4:+.1f}]"
              if np.isfinite(bs["lo"]) else "n/a")
        print(f"{v.name:<18}{rets.mean()*1e4:>+9.1f}{d.mean()*1e4:>+9.1f}"
              f"{ci:>19}{bs['p_le_zero']:>9.2f}"
              f"{c['best_share']*100:>7.0f}%{c['mean_ex_best']*1e4:>+11.1f}")

    print(f"\nCI = day-block bootstrap over {len(set(dates))} distinct entry dates; "
          "P(d<=0) = resample share where the change did not help.")
    print("best% = share of summed PnL from the single best trade. A variant whose "
          "edge lives in one trade is not a rule.")


if __name__ == "__main__":
    main()
