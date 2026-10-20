#!/usr/bin/env python
"""Q6 re-test: MACD + 200 EMA cross, on US equity/ETF 5m bars.

Why this exists
---------------
Row Q6 in docs/DAY_TRADE_EDGE_MATRIX.md is Closed. The original test ran
on crypto (Binance Vision) and failed at net -0.854R while gross was
+0.034R. Reading that result carefully: almost the entire loss was cost.
The stop sits 0.1*ATR beyond the 200 EMA, which on crypto made R small
relative to the 15 bp round-trip taker cost, so each trade surrendered
roughly 0.9R to fees and slippage. That is a cost-model failure as much
as a strategy failure.

Free Webull equity data removes that confound: ETF spreads are ~1 tick
and commission is zero, so round-trip cost drops from ~15 bp to well
under 1 bp. If the edge was real and merely fee-starved, it should appear
here. If the win rate itself is below the 40% break-even of a 1.5R
target, then costs were never the real problem and the 86% claim fails
on its own terms.

The frozen rules are copied unchanged from macd_ema200_backtest.py so the
two runs are comparable. Only the venue, the cost model, and one equity
specific exit are new. The equity-specific exit (flatten at RTH close) is
run as a clearly-marked variant, not folded into the primary.

Frozen model (unchanged from Q6):
    MACD 12/26/9, 200-period EMA.
    long  : MACD line crosses above signal with BOTH lines below zero,
            and close > 200 EMA
    short : mirror, both lines above zero, close < 200 EMA
    stop  : 200 EMA +/- 0.1 * ATR(14), as it stood at the signal bar
    target: 1.5R
    entry : next bar's open
    one position at a time; ambiguous bars resolve to STOP.

Cost model (equities; the one deliberate change):
    commission       0 (Webull retail)
    half-spread      1 tick each side  -> ~0.14 bp per side on SPY
    slippage         1 tick each side on entry and on stop exits
    => ~0.6 bp round trip at SPY's price level, vs 15 bp for crypto.
    A pessimistic 5 bp scenario is also reported so the conclusion does
    not rest on a favourable cost assumption.

Data: reports/.webull_cache/{SPY,QQQ}_5m_raw.json, pulled from the Webull
free tier. RTH only, volume present, verified contiguous.
"""
from __future__ import annotations

import json
import statistics as st
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from hyperliquid_bot.indicators import atr, ema, macd  # noqa: E402

CACHE = ROOT / "reports" / ".webull_cache"

# ---- frozen model (identical to Q6) ---------------------------------------
MA_LEN = 200
MACD_PARAMS = (12, 26, 9)
ATR_LEN = 14
STOP_ATR_BUF = 0.1
TARGET_R = 1.5
SCATTER_FRAC = 0.70
MIN_TRADES = 100

# ---- equity cost model ----------------------------------------------------
TICK = 0.01
COST_BP_REALISTIC = 0.6      # ~1 tick round trip in SPY terms
COST_BP_PESSIMISTIC = 5.0    # deliberately harsh; shows robustness
RTH_BARS_PER_DAY = 78


@dataclass
class Panel:
    symbol: str
    t: np.ndarray          # epoch ms
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    day: np.ndarray        # integer session index, for RTH-close flattening


@dataclass
class Trade:
    i: int
    direction: int
    entry: float
    stop: float
    target: float
    exit: float
    reason: str
    R: float
    gross_R: float
    net_R: float
    day: int


# ------------------------------------------------------------------ data
def load_panel(symbol: str) -> Panel:
    f = CACHE / f"{symbol}_5m_raw.json"
    if not f.exists():
        raise SystemExit(f"missing {f}; pull the Webull panel first")
    rows = json.loads(f.read_text())
    rows.sort(key=lambda r: r["time"])
    t, o, h, l, c, v = [], [], [], [], [], []
    for r in rows:
        t.append(int(datetime.strptime(r["time"], "%Y-%m-%dT%H:%M:%S.%f%z").timestamp() * 1000))
        o.append(float(r["open"])); h.append(float(r["high"]))
        l.append(float(r["low"])); c.append(float(r["close"]))
        v.append(float(r["volume"]))
    t = np.array(t, dtype=np.int64)
    # session index = trading day, so we can detect the RTH close
    days = np.array([datetime.fromtimestamp(x / 1000, tz=timezone.utc).toordinal()
                     for x in t])
    return Panel(symbol, t, np.array(o), np.array(h), np.array(l), np.array(c),
                 np.array(v), days)


# --------------------------------------------------------------- strategy
def signals(p: Panel) -> np.ndarray:
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


def simulate(p: Panel, sig: np.ndarray, flatten_at_close: bool,
             cost_bp: float | None = None) -> list[Trade]:
    a = atr(p.h, p.l, p.c, ATR_LEN)
    ma = ema(p.c, MA_LEN)
    n = len(p.c)
    cost_frac = (COST_BP_REALISTIC if cost_bp is None else cost_bp) / 1e4
    trades: list[Trade] = []
    i = 1
    while i < n - 1:
        s = int(sig[i])
        if s == 0 or not np.isfinite(ma[i]) or not np.isfinite(a[i]):
            i += 1
            continue
        j = i + 1
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

        entry_day = p.day[j]
        exit_px, reason, k = None, "eod", j
        while k < n:
            if flatten_at_close and p.day[k] != entry_day:
                # session ended; flatten on the last bar of the entry day
                exit_px, reason, k = p.c[k - 1], "close", k - 1
                break
            if s == 1:
                hit_stop = p.l[k] <= stop
                hit_tgt = p.h[k] >= target
            else:
                hit_stop = p.h[k] >= stop
                hit_tgt = p.l[k] <= target
            if hit_stop:              # adverse first on ambiguous bars
                exit_px, reason = stop, "stop"
                break
            if hit_tgt:
                exit_px, reason = target, "target"
                break
            k += 1
        if exit_px is None:
            exit_px, reason, k = p.c[n - 1], "eod", n - 1

        gross_px = (exit_px - entry) * s
        cost_px = entry * cost_frac
        trades.append(Trade(
            i=j, direction=s, entry=entry, stop=stop, target=target,
            exit=exit_px, reason=reason, R=R,
            gross_R=gross_px / R, net_R=(gross_px - cost_px) / R,
            day=entry_day,
        ))
        i = k + 1
    return trades


# ------------------------------------------------------------- statistics
def day_clustered_t(trades: list[Trade]) -> float:
    """t-stat clustered by session, per the matrix protocol."""
    by_day: dict[int, list[float]] = {}
    for t in trades:
        by_day.setdefault(t.day, []).append(t.net_R)
    if len(by_day) < 2:
        return float("nan")
    means = [st.mean(v) for v in by_day.values()]
    sd = st.stdev(means)
    return st.mean(means) / (sd / len(means) ** 0.5) if sd > 0 else float("nan")


def describe(label: str, trades: list[Trade], cost_bp: float,
             quiet: bool = False) -> dict:
    if not trades:
        print(f"  {label:30s} no trades")
        return {}
    wins = [t for t in trades if t.net_R > 0]
    losses = [t for t in trades if t.net_R <= 0]
    gross = st.mean(t.gross_R for t in trades)
    net = st.mean(t.net_R for t in trades)
    tcl = day_clustered_t(trades)
    cost_R = st.mean((t.entry * cost_bp / 1e4) / t.R for t in trades)
    be_net = (1.0 + cost_R) / (TARGET_R + 1.0)
    avg_w = st.mean(t.net_R for t in wins) if wins else 0.0
    avg_l = st.mean(t.net_R for t in losses) if losses else 0.0
    pf = (sum(t.net_R for t in wins) / abs(sum(t.net_R for t in losses))
          if losses and sum(t.net_R for t in losses) != 0 else float("inf"))
    if not quiet:
        print(f"  {label:30s} n={len(trades):5d}  win={len(wins)/len(trades):6.1%}  "
              f"grossR={gross:+.3f}  netR={net:+.3f}  t_cl={tcl:+.2f}")
        print(f"  {'':30s} avg win {avg_w:+.2f}R  avg loss {avg_l:+.2f}R  "
              f"PF {pf:.2f}  modelled break-even win {be_net:.1%}")
    return {"n": len(trades), "win": len(wins) / len(trades), "gross": gross,
            "net": net, "t": tcl, "be": be_net, "avg_w": avg_w, "avg_l": avg_l,
            "pf": pf, "cost_R": cost_R}


def buy_hold(p: Panel) -> float:
    return p.c[-1] / p.c[0] - 1.0


def run(symbol: str, flatten_at_close: bool, cost_bp: float,
        verbose: bool = False, quiet: bool = False) -> dict:
    p = load_panel(symbol)
    sig = signals(p)
    trades = simulate(p, sig, flatten_at_close, cost_bp)
    hold = buy_hold(p)

    cut = int(len(p.c) * SCATTER_FRAC)
    sc = [t for t in trades if t.i <= cut]
    ho = [t for t in trades if t.i > cut]

    tag = "flatten@close" if flatten_at_close else "hold-until-exit"
    if verbose:
        print(f"\n{p.symbol}  {len(p.c)} bars  {len(np.unique(p.day))} sessions  "
              f"buy&hold {hold:+.1%}")
        print(f"  variant: {tag}   cost {cost_bp} bp round trip")
    a = describe(f"{symbol} [{tag}]", trades, cost_bp, quiet=quiet)
    if a:
        a.update({"symbol": symbol, "variant": tag, "cost_bp": cost_bp,
                  "hold": hold, "bars": len(p.c)})
        if verbose:
            describe("  └ scatter 70%", sc, cost_bp)
            describe("  └ holdout 30%", ho, cost_bp)
        a["sc"] = {"n": len(sc), "net": st.mean([t.net_R for t in sc]) if sc else 0}
        a["ho"] = {"n": len(ho), "net": st.mean([t.net_R for t in ho]) if ho else 0}
    return a


def main() -> None:
    print("=" * 78)
    print("Q6 re-test on US equity 5m: MACD 12/26/9 + 200 EMA")
    print("=" * 78)
    print(f"MACD {MACD_PARAMS}  EMA {MA_LEN}  stop 200EMA±{STOP_ATR_BUF}ATR  "
          f"target {TARGET_R}R")
    print(f"break-even win rate before costs : {1/(1+TARGET_R):.1%}")
    print(f"crypto Q6 result (for reference) : n=2109  win=37.5%  net=-0.854R")

    rows = []
    for sym in ("SPY", "QQQ"):
        # primary: frozen rules, equity costs
        rows.append(run(sym, False, COST_BP_REALISTIC, verbose=(sym == "SPY")))
        # variant A: day-trader's RTH-close flatten
        rows.append(run(sym, True, COST_BP_REALISTIC))
        # variant B: pessimistic costs, frozen rules
        rows.append(run(sym, False, COST_BP_PESSIMISTIC, quiet=True))

    print("\n" + "=" * 78)
    print("HOLD-UNTIL-EXIT vs FLATTEN-AT-CLOSE (realistic costs)")
    print("=" * 78)
    for sym in ("SPY", "QQQ"):
        for flat in (False, True):
            r = [x for x in rows if x and x["symbol"] == sym and x["cost_bp"] == COST_BP_REALISTIC
                 and x["variant"] == ("flatten@close" if flat else "hold-until-exit")]
            if r:
                r = r[0]
                print(f"  {sym} {'flatten' if flat else 'hold   '}: "
                      f"n={r['n']:5d} win={r['win']:6.1%} netR={r['net']:+.3f} "
                      f"t={r['t']:+.2f}  (break-even {r['be']:.1%})")

    print("\n" + "=" * 78)
    print("COST SENSITIVITY (frozen rules) — is the result cost-driven?")
    print("=" * 78)
    print(f"  {'symbol':6s} {'cost bp':>8s} {'win':>7s} {'netR':>8s} {'break-even':>11s}  verdict")
    for sym in ("SPY", "QQQ"):
        for cb in (0.0, COST_BP_REALISTIC, 5.0, 15.0):
            r = run(sym, False, cb, quiet=True)
            if r:
                v = "PASS" if r["net"] > 0 else "fail"
                print(f"  {sym:6s} {cb:8.1f} {r['win']:7.1%} {r['net']:+8.3f} "
                      f"{r['be']:11.1%}  {v}")

    print("\n" + "=" * 78)
    print("EXIT REASONS — why does win rate clear break-even yet net is ~0?")
    print("=" * 78)
    for sym in ("SPY", "QQQ"):
        p = load_panel(sym)
        tr = simulate(p, signals(p), False, COST_BP_REALISTIC)
        print(f"\n  {sym} (frozen rules, {COST_BP_REALISTIC} bp):")
        for reason in ("target", "stop", "eod"):
            sub = [t for t in tr if t.reason == reason]
            if sub:
                print(f"    {reason:7s}: n={len(sub):4d} ({len(sub)/len(tr):5.1%})  "
                      f"avg net {st.mean(t.net_R for t in sub):+.3f}R  "
                      f"avg R size {st.mean(t.R for t in sub):+.3f}")
        # the key asymmetry: how big is R vs how far price actually travels
        print(f"    mean R (stop distance) : {st.mean(t.R for t in tr):.3f} "
              f"({st.mean(t.R/t.entry for t in tr)*1e4:.1f} bp of price)")
        print(f"    raw price move per win : {st.mean(t.net_R*t.R for t in tr if t.net_R>0):+.3f}")
        print(f"    raw price move per loss: {st.mean(t.net_R*t.R for t in tr if t.net_R<=0):+.3f}")
        print(f"    cost as fraction of R  : {st.mean((t.entry*COST_BP_REALISTIC/1e4)/t.R for t in tr):.3f}R")

    print("\n" + "=" * 78)
    print("SCATTER / HOLDOUT (SPY, frozen rules, realistic costs)")
    print("=" * 78)
    p = load_panel("SPY")
    tr = simulate(p, signals(p), False, COST_BP_REALISTIC)
    cut = int(len(p.c) * SCATTER_FRAC)
    sc = [t for t in tr if t.i <= cut]
    ho = [t for t in tr if t.i > cut]
    a = describe("scatter 70%", sc, COST_BP_REALISTIC)
    b = describe("holdout 30%", ho, COST_BP_REALISTIC)
    if a and b:
        same = (a["net"] > 0) == (b["net"] > 0)
        print(f"\n  same sign both halves: {same}")
        print(f"  verdict: {'holds up' if (a['net']>0 and b['net']>0) else 'does NOT hold up out of sample'}")

    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)
    prim = [r for r in rows if r and r["variant"] == "hold-until-exit"
            and r["cost_bp"] == COST_BP_REALISTIC]
    if prim:
        win = st.mean(r["win"] for r in prim)
        net = st.mean(r["net"] for r in prim)
        be = st.mean(r["be"] for r in prim)
        print(f"  mean win rate (SPY+QQQ, frozen rules) : {win:.1%}")
        print(f"  break-even win rate after real costs   : {be:.1%}")
        print(f"  mean net R                             : {net:+.3f}")
        print(f"  crypto Q6 comparison                   : 37.5% win, -0.854R")
        print()
        if win < be:
            print(f"  The win rate itself ({win:.1%}) is BELOW the after-cost break-even")
            print(f"  ({be:.1%}). Equity costs are ~25x smaller than crypto's, so unlike")
            print("  the original test this is NOT a fee problem. The entry rule does")
            print("  not clear its own break-even, which is the deeper finding.")
        else:
            print("  Win rate clears break-even. Check t-stat and holdout above.")


if __name__ == "__main__":
    main()
