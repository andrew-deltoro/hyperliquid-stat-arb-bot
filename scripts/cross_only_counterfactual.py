#!/usr/bin/env python3
"""
Cross-only counterfactual for the live pairs bot.

Question (audit follow-up): for every trade the bot actually took, what would
it have made if the ONLY spread exit was `cross` (the current live rule) and
no target/trail/beta_shift force-close existed?

Method
------
Replay each *real* closed trade (pnl != 0) on the *live* signal tape by
reconstructing the same centered spread the bot uses at each decision point:

    raw(t)             = log(pa(t)) - entry_alpha - beta_entry * log(pb(t))
    current_centered(t)= raw(t) - entry_mean_raw

Prices come from Hyperliquid 1h candles (public API).  The decision grid is the
4h cycle grid (00,04,08,12,16,20 UTC) that hlbot.timer actually fires on, so we
evaluate the spread exactly like the scheduler does.

For each trade we find the FIRST decision timestamp where any of these fire, in
the SAME precedence order as positions.evaluate_open:

    1. cross      -> current_centered crosses -cross_frac * entry_spread
    2. divergence -> stop_spread breached
    3. time_stop  -> now >= time_stop_iso

(deliberately NO beta_shift and NO target/trail: this is the cross-only world)

P&L is computed with the production cost model (journal.compute_pnl), funding
excluded because it is not in the tape, so numbers are slightly conservative.

Run:  .venv/bin/python scripts/cross_only_counterfactual.py
"""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.hyperliquid_bot.phase3.journal import compute_pnl  # noqa: E402

RUNTIME = Path("/home/hlbot/hlbot-runtime/live")
POSITIONS = RUNTIME / "state" / "positions.json"
CFG = yaml.safe_load((REPO / "config" / "phase3-live.yaml").read_text())
API = CFG.get("api_url", "https://api.hyperliquid.xyz/info")
CROSS_FRAC = float(CFG["position"].get("cross_exit_frac", 0.75))

# Real eval grid: hlbot.timer fires 00,04,08,12,16,20 UTC.
CYCLE_HOURS = [0, 4, 8, 12, 16, 20]


def iso(s):
    return datetime.fromisoformat(s) if s else None


def candles(coin, start_ms, end_ms):
    try:
        r = requests.post(
            API,
            json={
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": "1h", "startTime": start_ms, "endTime": end_ms},
            },
            timeout=25,
        ).json()
    except Exception as exc:  # noqa: BLE001
        print(f"    ! candle fetch failed for {coin}: {exc}", file=sys.stderr)
        return None
    if not isinstance(r, list):
        return None
    return r


def price_table(coin, start_ms, end_ms):
    """Return sorted list of (T_ms, close) with T = bar close time."""
    r = candles(coin, start_ms, end_ms)
    if not r:
        return []
    out = []
    for bar in r:
        try:
            out.append((int(bar["T"]), float(bar["c"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort()
    return out


def price_at(table, ts_ms):
    """Close of the latest bar whose close time <= ts. None if none."""
    price = None
    for t, c in table:
        if t <= ts_ms:
            price = c
        else:
            break
    return price


def next_cycles(entry, until):
    """Decision timestamps on the 4h grid strictly after entry, up to `until`."""
    out = []
    cur = entry
    while cur <= until:
        # next grid point strictly greater than cur
        nxt = cur.replace(minute=0, second=0, microsecond=0)
        h = nxt.hour
        for ch in CYCLE_HOURS:
            if ch > h:
                nxt = nxt.replace(hour=ch)
                break
        else:
            nxt = (nxt + timedelta(days=1)).replace(hour=CYCLE_HOURS[0])
        if nxt <= cur:
            nxt = cur + timedelta(hours=4)
        if nxt > until:
            break
        out.append(nxt)
        cur = nxt
    return out


def replay(pos, tables):
    """Return (exit_reason, exit_a, exit_b, exit_ts) for the cross-only world."""
    long_a = pos["direction"] == "LONG_A_SHORT_B"
    entry_spread = float(pos["entry_spread"])
    cross_level = -CROSS_FRAC * entry_spread
    stop_spread = float(pos["stop_spread"])
    alpha = float(pos["entry_alpha"])
    mean_raw = float(pos["entry_mean_raw"])
    beta = float(pos["beta_entry"])

    entry = iso(pos["entry_time_iso"])
    tstop = iso(pos.get("time_stop_iso")) or entry
    now = datetime.now(timezone.utc)
    until = min(tstop, now)

    ta = tables.get(pos["coin_a"], [])
    tb = tables.get(pos["coin_b"], [])
    if not ta or not tb:
        return ("no_price_data", None, None, None)

    for ts in next_cycles(entry, until):
        tsms = int(ts.timestamp() * 1000)
        pa = price_at(ta, tsms)
        pb = price_at(tb, tsms)
        if pa is None or pb is None:
            continue
        try:
            raw = math.log(pa) - alpha - beta * math.log(pb)
        except ValueError:
            continue
        cc = raw - mean_raw

        # 1. cross
        if long_a and cc >= cross_level:
            return ("cross", pa, pb, ts)
        if (not long_a) and cc <= cross_level:
            return ("cross", pa, pb, ts)
        # 2. divergence stop
        if long_a and cc <= stop_spread:
            return ("divergence_stop", pa, pb, ts)
        if (not long_a) and cc >= stop_spread:
            return ("divergence_stop", pa, pb, ts)
        # 3. time stop
        if ts >= tstop:
            return ("time_stop", pa, pb, ts)

    if until >= tstop and tstop < now:
        tsms = int(tstop.timestamp() * 1000)
        return ("time_stop", price_at(ta, tsms), price_at(tb, tsms), tstop)
    return ("still_open", None, None, None)


def main():
    state = json.loads(POSITIONS.read_text())
    trades = [
        p
        for p in state["positions"]
        if p.get("status") == "closed" and (p.get("pnl_usd") or 0) != 0
    ]
    trades.sort(key=lambda p: p.get("entry_time_iso") or "")
    print(f"Real closed trades to replay: {len(trades)}")

    # Pre-fetch price tables per coin over the whole span.
    start = min(iso(p["entry_time_iso"]) for p in trades) - timedelta(days=1)
    end = datetime.now(timezone.utc) + timedelta(hours=1)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    coins = sorted({p["coin_a"] for p in trades} | {p["coin_b"] for p in trades})
    tables = {}
    for c in coins:
        tables[c] = price_table(c, start_ms, end_ms)
        time.sleep(0.15)
    print(f"Fetched candles for {len(coins)} coins\n")

    rows = []
    for p in trades:
        reason, pa, pb, ts = replay(p, tables)
        cf_pnl = None
        if pa is not None and pb is not None:
            cf_pnl, _ = compute_pnl(p, pa, pb)
        rows.append(
            {
                "pair": p["pair"],
                "dir": p["direction"][:5],
                "entry": (p.get("entry_time_iso") or "")[:16],
                "actual_reason": p.get("exit_reason"),
                "actual_pnl": round(p.get("pnl_usd") or 0.0, 2),
                "cf_reason": reason,
                "cf_pnl": None if cf_pnl is None else round(cf_pnl, 2),
                "delta": None if cf_pnl is None else round(cf_pnl - (p.get("pnl_usd") or 0.0), 2),
            }
        )

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 100)
    print(df.to_string(index=False))
    print()

    both = df.dropna(subset=["cf_pnl"])
    print("=" * 68)
    print(f"Actual total (all {len(df)}):          ${df['actual_pnl'].sum():+8.2f}")
    print(f"Cross-only total (replayable {len(both)}): ${both['cf_pnl'].sum():+8.2f}")
    print(f"Delta:                              ${(both['cf_pnl'] - both['actual_pnl']).sum():+8.2f}")
    print()
    # The specific question: which trades would cross-only have won?
    wins = both[both["cf_pnl"] > 0]
    print(f"Cross-only winners: {len(wins)}/{len(both)}  (${wins['cf_pnl'].sum():+.2f})")
    print(f"Cross-only losers:  {len(both) - len(wins)}/{len(both)}  (${both[both['cf_pnl'] <= 0]['cf_pnl'].sum():+.2f})")
    print("=" * 68)

    out = REPO / "reports" / "cross_only_counterfactual.csv"
    df.to_csv(out, index=False)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
