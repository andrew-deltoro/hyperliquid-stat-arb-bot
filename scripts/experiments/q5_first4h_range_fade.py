#!/usr/bin/env python
"""Q5: First 4H New York range, failed-breakout fade.

The locked rules live in docs/DAY_TRADE_EDGE_MATRIX.md, row Q5. This
script implements exactly those rules and reports the precommitted
scatter on the FIRST 70% of New York days. The last 30% stays sealed
unless the scatter clears its kill rule; see the tail of main().

Why this exists
---------------
A public YouTube video ("The BEST 5 Minute Scalping Strategy Ever",
Data Trader) presents a rule-based intraday fade. Two independent
public replications already lost money on it. The row gets one look
because the ingredients (a pre-session anchor many participants watch,
plus a failed sweep) are real, and the kill test is cheap.

Venue / data choice
-------------------
Signal data: 5-minute BTCUSDT bars from data-api.binance.vision, frozen
to disk before the first scatter. api.binance.com returns HTTP 451 from
this host; the .vision mirror does not. Hyperliquid cannot supply the
sample: its candleSnapshot keeps only the most recent ~5,000 bars per
interval, so 5m reaches back ~17.4 days. We would execute on Hyperliquid,
so the row carries basis risk between the signal venue and the execution
venue. That is stated in the matrix and is not adjusted away here.

Costs
-----
Per the matrix cost table: 4.5 bp taker fee each side, plus 3 bp
slippage each side where the tape has no book (candles have no book).
That is 7.5 bp per side, 15 bp round trip on notional. Reported gross
AND net, as the house style requires.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics as st
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "reports" / ".q5_cache"
CACHE.mkdir(parents=True, exist_ok=True)

NY = ZoneInfo("America/New_York")
KLINE = "https://data-api.binance.vision/api/v3/klines"

# ---- frozen cost model (matrix cost table) --------------------------------
FEE_PER_SIDE = 0.00045      # 4.5 bp taker fee
SLIP_PER_SIDE = 0.00030     # 3 bp slippage, no book on candles
COST_ROUND_TRIP = 2 * (FEE_PER_SIDE + SLIP_PER_SIDE)   # 15 bp on notional

# ---- frozen strategy parameters (matrix row Q5) ---------------------------
RANGE_START_H, RANGE_END_H = 0, 4        # 00:00-04:00 America/New_York
TARGET_R = 2.0                           # target = 2R from actual entry/stop
MIN_NY_DAYS = 120                        # rule 3 minimum before the split
SCATTER_FRAC = 0.70                      # first 70% is the scatter


@dataclass
class Bar:
    t: int          # open time, epoch ms
    o: float
    h: float
    l: float
    c: float

    @property
    def ny(self) -> dt.datetime:
        return dt.datetime.fromtimestamp(self.t / 1000, NY)


@dataclass
class Trade:
    day: str
    direction: str          # "long" | "short"
    entry_i: int
    entry: float
    stop: float
    target: float
    exit: float
    reason: str             # "stop" | "target" | "eod"
    R_px: float
    gross_R: float
    net_R: float


# ------------------------------------------------------------------ data
def fetch_panel(symbol: str = "BTCUSDT", days: int = 730) -> list[Bar]:
    """Frozen 5-minute panel; cached to disk so the scatter is repeatable."""
    f = CACHE / f"{symbol}_5m_{days}d.json"
    if f.exists():
        raw = json.loads(f.read_text())
    else:
        end = int(time.time() * 1000)
        start = end - days * 86_400_000
        rows: list[list] = []
        cursor = end
        while cursor > start:
            url = (f"{KLINE}?symbol={symbol}&interval=5m"
                   f"&endTime={cursor}&limit=1000")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            page = json.load(urllib.request.urlopen(req, timeout=40))
            if not page:
                break
            rows = page + rows
            cursor = page[0][0] - 1
            if page[0][0] <= start:
                break
            time.sleep(0.12)
        raw = rows
        f.write_text(json.dumps(raw))

    seen: dict[int, list] = {}
    for r in raw:
        seen[int(r[0])] = r
    out = [Bar(t=int(r[0]), o=float(r[1]), h=float(r[2]),
               l=float(r[3]), c=float(r[4]))
           for _, r in sorted(seen.items(), key=lambda kv: kv[0])]
    return out


# --------------------------------------------------------------- strategy
def run_day(bars: list[Bar]) -> Trade | None:
    """One NY day's bars, chronological. Returns at most one trade.

    Frozen rules, in order:
      range  = high/low of 5m bars whose NY hour is in [00:00, 04:00)
      break  = a 5m bar CLOSES strictly outside the range (wick alone no)
      reentry= a LATER 5m bar CLOSES strictly back inside, same NY date
      entry  = the NEXT 5m open after the re-entry close
      stop   = breakout-move extreme from first outside close to re-entry
      target = exactly 2R from actual entry and stop
      long fades a failed low-side break; short fades a high-side break
      one position at a time; a fresh setup needs a new close outside
    Ambiguous bars are resolved against the trade (stop taken first).
    Nothing carries into the next NY date; open positions flatten at the
    last 5m close of the day.
    """
    rng = [b for b in bars if RANGE_START_H <= b.ny.hour < RANGE_END_H]
    if len(rng) < 40:            # a real 4h window is ~48 bars
        return None
    hi = max(b.h for b in rng)
    lo = min(b.l for b in rng)

    session = [i for i, b in enumerate(bars) if b.ny.hour >= RANGE_END_H] \
        or [i for i, b in enumerate(bars) if b.ny.hour >= RANGE_END_H]
    if not session:
        return None

    state = "IDLE"
    ext = 0.0
    for i in session:
        b = bars[i]

        if state == "IDLE":
            if b.c > hi:
                state, ext = "OUT_UP", b.h
            elif b.c < lo:
                state, ext = "OUT_DN", b.l
            continue

        if state == "OUT_UP":
            ext = max(ext, b.h)
            if b.c <= hi:                       # re-entry close -> short fade
                return _enter(bars, i, frm=ext, direction="short", day=b.ny)
            if b.c < lo:                        # crossed the whole range: void
                state = "IDLE"
            continue

        if state == "OUT_DN":
            ext = min(ext, b.l)
            if b.c >= lo:                       # re-entry close -> long fade
                return _enter(bars, i, frm=ext, direction="long", day=b.ny)
            if b.c > hi:
                state = "IDLE"
            continue

    return None


def _enter(bars: list[Bar], reentry_i: int, frm: float, direction: str,
           day: dt.datetime) -> Trade | None:
    """Enter at the next bar's open; walk to stop, target, or the day's end."""
    j = reentry_i + 1
    if j >= len(bars) or bars[j].ny.date() != day.date():
        return None
    entry = bars[j].o
    if direction == "short":
        stop, R = frm, frm - entry
        if R <= 0:
            return None
        target = entry - TARGET_R * R
    else:
        stop, R = frm, entry - frm
        if R <= 0:
            return None
        target = entry + TARGET_R * R

    exit_px, reason = None, "eod"
    for k in range(j, len(bars)):
        if bars[k].ny.date() != day.date():
            break
        b = bars[k]
        hit_stop = b.h >= stop if direction == "short" else b.l <= stop
        hit_tgt = b.l <= target if direction == "short" else b.h >= target
        if hit_stop:                 # adverse first on an ambiguous bar
            exit_px, reason = stop, "stop"
            break
        if hit_tgt:
            exit_px, reason = target, "target"
            break
    if exit_px is None:              # nothing resolved: flat on the day's last close
        last = None
        for k in range(len(bars) - 1, j - 1, -1):
            if bars[k].ny.date() == day.date():
                last = bars[k]
                break
        if last is None:
            return None
        exit_px, reason = last.c, "eod"

    gross_px = (entry - exit_px) if direction == "short" else (exit_px - entry)
    cost_px = entry * COST_ROUND_TRIP
    return Trade(
        day=str(day.date()), direction=direction, entry_i=j,
        entry=entry, stop=stop, target=target, exit=exit_px, reason=reason,
        R_px=R, gross_R=gross_px / R, net_R=(gross_px - cost_px) / R,
    )


def by_ny_day(bars: list[Bar]) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {}
    for b in bars:
        out.setdefault(str(b.ny.date()), []).append(b)
    return out


# ------------------------------------------------------------- statistics
def day_clustered_t(net_by_day: list[float]) -> float:
    """t-statistic with one number per day. Setups inside a day are not
    independent samples, so the unit is the day."""
    n = len(net_by_day)
    if n < 2:
        return float("nan")
    m = st.mean(net_by_day)
    sd = st.stdev(net_by_day)
    return m / (sd / (n ** 0.5)) if sd > 0 else float("nan")


def describe(label: str, trades: list[Trade]) -> dict:
    if not trades:
        print(f"{label}: no trades")
        return {}
    per_day: dict[str, list[float]] = {}
    for t in trades:
        per_day.setdefault(t.day, []).append(t.net_R)
    day_means = [st.mean(v) for v in per_day.values()]
    wins = sum(1 for t in trades if t.net_R > 0)
    gross = st.mean(t.gross_R for t in trades)
    net = st.mean(t.net_R for t in trades)
    tstat = day_clustered_t(day_means)
    print(f"\n{label}")
    print(f"  days active        : {len(per_day)}")
    print(f"  trades             : {len(trades)}")
    print(f"  win rate (net R>0) : {wins / len(trades):.1%}")
    print(f"  mean gross R       : {gross:+.4f}")
    print(f"  mean net R         : {net:+.4f}")
    print(f"  day-clustered t    : {tstat:+.3f}")
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    print(f"  exits              : {reasons}")
    return {"days": len(per_day), "n": len(trades),
            "win": wins / len(trades), "gross": gross, "net": net, "t": tstat}


# ------------------------------------------------------------------- main
def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 730
    print(f"Q5  first 4H New York range, failed-breakout fade")
    print(f"signal venue data-api.binance.vision  symbol BTCUSDT  5m  {days}d")
    print(f"cost model  {COST_ROUND_TRIP*1e4:.1f} bp round trip "
          f"({FEE_PER_SIDE*1e4:.1f} fee + {SLIP_PER_SIDE*1e4:.1f} slip, each side)")
    print(f"range window  {RANGE_START_H:02d}:00-{RANGE_END_H:02d}:00 America/New_York   "
          f"target {TARGET_R:.0f}R   sealed fraction {1-SCATTER_FRAC:.0%}")

    bars = fetch_panel("BTCUSDT", days)
    print(f"\npanel: {len(bars)} bars  "
          f"{bars[0].ny:%Y-%m-%d %H:%M %Z} -> {bars[-1].ny:%Y-%m-%d %H:%M %Z} (NY)")

    days_map = by_ny_day(bars)
    ordered = sorted(days_map)

    # split by NY day, first 70% is the scatter
    cut = int(len(ordered) * SCATTER_FRAC)
    scatter_days = set(ordered[:cut])

    if cut < MIN_NY_DAYS:
        print(f"\nSTOP: only {cut} NY days before the split; rule 3 wants "
              f"{MIN_NY_DAYS}. Holdout not opened.")
        return

    trades = []
    for d in ordered:
        t = run_day(days_map[d])
        if t:
            trades.append(t)

    sc = [t for t in trades if t.day in scatter_days]
    print(f"\ncalendar split: {cut} scatter days / {len(ordered)-cut} sealed days")
    stats = describe("SCATTER (first 70% of NY days)", sc)

    if not stats:
        print("\nKILL: no setups in the scatter window. Holdout stays sealed.")
        return

    # ---- kill rule, from the matrix row --------------------------------
    fails = []
    if stats["net"] <= 0:
        fails.append(f"mean net R {stats['net']:+.4f} <= 0")
    if not (stats["t"] >= 2):
        fails.append(f"day-clustered t {stats['t']:+.3f} < 2")
    if stats["net"] <= COST_ROUND_TRIP:
        fails.append("edge inside the round trip")

    print("\nkill rule")
    if fails:
        for f in fails:
            print(f"  FAIL  {f}")
        print("\nVERDICT: kill met. The last 30% of NY days is NOT opened.")
        print("Do not add a filter, a second coin, or a new horizon to rescue this.")
    else:
        print("  scatter clears its cost and the t-gate")
        print("\nwalk-forward (sealed 30%, now opened once):")
        ho = [t for t in trades if t.day not in scatter_days]
        h = describe("HOLDOUT (last 30%)", ho)
        if h and h["net"] > 0 and h["t"] >= 2:
            print("\nVERDICT: pass. Same sign as the scatter, clears cost.")
            print("This is not a reason to trade it. Sizing is a separate decision.")
        else:
            print("\nVERDICT: fail. Scatter sign did not survive the holdout.")


if __name__ == "__main__":
    main()
