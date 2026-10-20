#!/usr/bin/env python
"""Test: MACD + 200 EMA cross strategy ("86% win rate").

Source: TradingLab, "BEST MACD Trading Strategy [86% Win Rate]",
https://www.youtube.com/watch?v=rf_EQvubKlk (2021, ~4.6M views). The
title's 86% has no trade count, market, timeframe or test method
behind it anywhere in the video or description.

Rules as stated in the video
----------------------------
  long   MACD line crosses ABOVE its signal line while BOTH lines are
         BELOW zero, and price is ABOVE the 200-period moving average
  short  mirror: MACD crosses BELOW signal with both lines ABOVE zero,
         and price is BELOW the 200 MA
  stop   beyond the 200 MA, which "acts as a wall"
  target 1.5 x the risk

Choices the video leaves loose, frozen here BEFORE any test (the same
way two independent public replications had to freeze them):
  * MACD 12/26/9 (the standard the video's chart shows); the video
    mis-describes the two lines as "12 day / 26 day" averages.
  * the moving average is a 200-period EMA.
  * entry is the NEXT bar's open after the cross, not the cross close.
  * the stop sits 0.1 x ATR beyond the 200 EMA as it stood at entry.
  * ambiguous bars (one bar touches stop and target) resolve to STOP.
  * one position at a time; a fresh cross is required after an exit.
  * unresolved positions flatten at the last close.
  * the video's "wait for a support/resistance bounce" step is a
    judgement call and is NOT tested here.

Data: frozen Binance Vision panels (api.binance.com is HTTP 451 from
this host; data-api.binance.vision is not). Costs: 15 bp round trip
(4.5 bp taker fee + 3 bp slippage, each side) on notional, the same
model used in the day-trade edge matrix.

Reported gross AND net, and against a buy-and-hold benchmark.
"""
from __future__ import annotations

import json
import statistics as st
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from hyperliquid_bot.indicators import atr, ema, macd  # noqa: E402

CACHE = ROOT / "reports" / ".macd_cache"
CACHE.mkdir(parents=True, exist_ok=True)
KLINE = "https://data-api.binance.vision/api/v3/klines"

# ---- frozen model ---------------------------------------------------------
MA_LEN = 200            # 200-period EMA
MACD_PARAMS = (12, 26, 9)
ATR_LEN = 14
STOP_ATR_BUF = 0.1      # stop = 200EMA +/- 0.1*ATR
TARGET_R = 1.5
FEE_PER_SIDE = 0.00045
SLIP_PER_SIDE = 0.00030
COST_ROUND_TRIP = 2 * (FEE_PER_SIDE + SLIP_PER_SIDE)   # 15 bp
SCATTER_FRAC = 0.70
MIN_TRADES = 100

MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


@dataclass
class Panel:
    symbol: str
    interval: str
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray


@dataclass
class Trade:
    i: int
    direction: int          # +1 long, -1 short
    entry: float
    stop: float
    target: float
    exit: float
    reason: str
    R: float
    gross_R: float
    net_R: float


# ------------------------------------------------------------------ data
def fetch_panel(symbol: str, interval: str, days: int) -> Panel:
    f = CACHE / f"{symbol}_{interval}_{days}d.json"
    if f.exists():
        raw = json.loads(f.read_text())
    else:
        end = int(time.time() * 1000)
        start = end - days * 86_400_000
        rows: list[list] = []
        cursor = end
        while cursor > start:
            url = (f"{KLINE}?symbol={symbol}&interval={interval}"
                   f"&endTime={cursor}&limit=1000")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            page = json.load(urllib.request.urlopen(req, timeout=45))
            if not page:
                break
            rows = page + rows
            cursor = page[0][0] - 1
            if page[0][0] <= start:
                break
            time.sleep(0.1)
        raw = rows
        f.write_text(json.dumps(raw))

    seen: dict[int, list] = {int(r[0]): r for r in raw}
    ordered = [r for _, r in sorted(seen.items(), key=lambda kv: kv[0])]
    return Panel(
        symbol=symbol, interval=interval,
        t=np.array([int(r[0]) for r in ordered], dtype=np.int64),
        o=np.array([float(r[1]) for r in ordered]),
        h=np.array([float(r[2]) for r in ordered]),
        l=np.array([float(r[3]) for r in ordered]),
        c=np.array([float(r[4]) for r in ordered]),
    )


# --------------------------------------------------------------- strategy
def signals(p: Panel) -> np.ndarray:
    """+1 long, -1 short, 0 none. Uses only completed bars (index i)."""
    line, sig, _ = macd(p.c, *MACD_PARAMS)
    ma = ema(p.c, MA_LEN)
    n = len(p.c)
    out = np.zeros(n, dtype=np.int8)
    for i in range(1, n):
        if not np.isfinite(ma[i]):
            continue
        up = line[i] > sig[i] and line[i - 1] <= sig[i - 1]
        dn = line[i] < sig[i] and line[i - 1] >= sig[i - 1]
        below0 = line[i] < 0 and sig[i] < 0
        above0 = line[i] > 0 and sig[i] > 0
        if up and below0 and p.c[i] > ma[i]:
            out[i] = 1
        elif dn and above0 and p.c[i] < ma[i]:
            out[i] = -1
    return out


def simulate(p: Panel, sig: np.ndarray) -> list[Trade]:
    a = atr(p.h, p.l, p.c, ATR_LEN)
    ma = ema(p.c, MA_LEN)
    n = len(p.c)
    trades: list[Trade] = []
    i = 1
    while i < n - 1:
        s = sig[i]
        if s == 0 or not np.isfinite(ma[i]) or not np.isfinite(a[i]):
            i += 1
            continue
        j = i + 1                      # entry at next bar's open
        entry = p.o[j]
        if s == 1:
            stop = ma[i] - STOP_ATR_BUF * a[i]
            R = entry - stop
            if R <= 0:
                i += 1
                continue
            target = entry + TARGET_R * R
        else:
            stop = ma[i] + STOP_ATR_BUF * a[i]
            R = stop - entry
            if R <= 0:
                i += 1
                continue
            target = entry - TARGET_R * R

        exit_px, reason, k = None, "eod", j
        while k < n:
            if s == 1:                 # long: stop below, target above
                hit_stop = p.l[k] <= stop
                hit_tgt = p.h[k] >= target
            else:                      # short: stop above, target below
                hit_stop = p.h[k] >= stop
                hit_tgt = p.l[k] <= target
            if hit_stop:               # adverse first on an ambiguous bar
                exit_px, reason = stop, "stop"
                break
            if hit_tgt:
                exit_px, reason = target, "target"
                break
            k += 1
        if exit_px is None:
            exit_px, reason, k = p.c[n - 1], "eod", n - 1

        gross_px = (exit_px - entry) * s
        cost_px = entry * COST_ROUND_TRIP
        trades.append(Trade(
            i=j, direction=int(s), entry=entry, stop=stop, target=target,
            exit=exit_px, reason=reason, R=R,
            gross_R=gross_px / R, net_R=(gross_px - cost_px) / R,
        ))
        i = k + 1                      # one position at a time
    return trades


# ------------------------------------------------------------- statistics
def buy_hold(p: Panel) -> float:
    return p.c[-1] / p.c[0] - 1.0


def describe(label: str, trades: list[Trade], hold: float | None = None) -> dict:
    if not trades:
        print(f"  {label}: no trades")
        return {}
    wins = sum(1 for t in trades if t.net_R > 0)
    gross = st.mean(t.gross_R for t in trades)
    net = st.mean(t.net_R for t in trades)
    sd = st.stdev([x.net_R for x in trades]) if len(trades) > 1 else 0.0
    tstat = net / (sd / len(trades) ** 0.5) if sd > 0 else float("nan")
    be = 1.0 / (1.0 + TARGET_R)        # break-even win rate before costs
    line = (f"  {label:26s} n={len(trades):5d}  win={wins/len(trades):5.1%}  "
            f"grossR={gross:+.3f}  netR={net:+.3f}  t={tstat:+.2f}")
    print(line)
    return {"n": len(trades), "win": wins / len(trades), "gross": gross,
            "net": net, "t": tstat, "be": be}


def run_market(symbol: str, interval: str, days: int, verbose: bool = False) -> dict:
    p = fetch_panel(symbol, interval, days)
    sig = signals(p)
    trades = simulate(p, sig)
    hold = buy_hold(p)

    cut_t = p.t[0] + int((p.t[-1] - p.t[0]) * SCATTER_FRAC)
    sc = [t for t in trades if p.t[t.i] <= cut_t]
    ho = [t for t in trades if p.t[t.i] > cut_t]

    if verbose:
        print(f"\n{p.symbol} {p.interval}  {len(p.c)} bars  "
              f"buy&hold {hold:+.1%}  (window {days}d)")
    a = describe(f"{symbol} {interval}", trades, hold)
    if verbose and a:
        cost_R = st.mean((x.entry * COST_ROUND_TRIP) / x.R for x in trades)
        be_gross = 1.0 / (1.0 + TARGET_R)
        # after costs, a win pays (1.5R - c) and a loss pays -(1R + c)
        be_net = (1.0 + cost_R) / (TARGET_R + 1.0)
        print(f"  {'':26s} mean cost = {cost_R:.3f}R   "
              f"break-even win rate: gross {be_gross:.1%} -> net {be_net:.1%}")
    a.update({"symbol": symbol, "interval": interval, "hold": hold,
              "sc": describe(f"  └ scatter 70%", sc) if verbose else {},
              "ho": describe(f"  └ holdout 30%", ho) if verbose else {}})
    return a


def main() -> None:
    print("MACD + 200 EMA cross strategy  (TradingLab '86% win rate')")
    print(f"MACD {MACD_PARAMS}  EMA {MA_LEN}  stop 200EMA±{STOP_ATR_BUF}ATR  "
          f"target {TARGET_R}R  cost {COST_ROUND_TRIP*1e4:.0f} bp round trip")
    print(f"break-even win rate before costs = {1/(1+TARGET_R):.1%}")
    print("after-cost break-even depends on cost/R; computed per market below")

    matrix = [
        ("BTCUSDT", "1h", 730),
        ("BTCUSDT", "15m", 365),
        ("BTCUSDT", "4h", 1825),
        ("ETHUSDT", "1h", 730),
        ("ETHUSDT", "15m", 365),
        ("SOLUSDT", "1h", 730),
        ("SOLUSDT", "15m", 365),
        ("BNBUSDT", "1h", 730),
        ("XRPUSDT", "1h", 730),
        ("ADAUSDT", "1h", 730),
    ]
    primary = run_market("BTCUSDT", "1h", 730, verbose=True)
    print("\n--- matrix ---")
    rows = [primary]
    for sym, iv, d in matrix[1:]:
        try:
            rows.append(run_market(sym, iv, d, verbose=False))
        except Exception as e:
            print(f"  {sym} {iv}: ERR {e}")

    print("\n--- summary ---")
    tot_n = sum(r.get("n", 0) for r in rows)
    net_mean = st.mean(r["net"] for r in rows if r)
    wins = st.mean(r["win"] for r in rows if r)
    print(f"  markets tested            : {len([r for r in rows if r])}")
    print(f"  total trades              : {tot_n}")
    print(f"  mean win rate across runs : {wins:.1%}")
    print(f"  mean net R across runs    : {net_mean:+.3f}")
    pos = [r for r in rows if r and r["net"] > 0]
    print(f"  runs with net R > 0       : {len(pos)} / {len([r for r in rows if r])}")
    print(f"  highest single win rate   : {max((r['win'] for r in rows if r), default=0):.1%}")

    print("\n--- primary market walk-forward (BTC 1h) ---")
    sc, ho = primary.get("sc", {}), primary.get("ho", {})
    if sc and ho:
        print(f"  scatter: n={sc['n']} net={sc['net']:+.3f} t={sc['t']:+.2f}")
        print(f"  holdout: n={ho['n']} net={ho['net']:+.3f} t={ho['t']:+.2f}")
        if sc["net"] > 0 and ho["net"] > 0:
            print("  VERDICT: same sign both halves.")
        else:
            print("  VERDICT: does not hold up out of sample.")
    print("\n  Note: the title claims 86%. The frozen rules here are the ones")
    print("  the video states. The gap between 86% and these numbers is the finding.")


if __name__ == "__main__":
    main()
