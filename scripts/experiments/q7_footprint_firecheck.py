#!/usr/bin/env python
"""Q7 fire-check: does the frozen orderflow-absorption setup fire AT ALL on
MNQ 5-minute footprint?

This is a FIRE-CHECK, not a scored backtest. It answers one question: can the
frozen Q7 funnel (four layers: context -> location -> confirmation ->
execution) even produce a setup on the instrument the source actually trades?
An earlier attempt on three years of BTCUSDT produced zero setups, and that
zero is the whole open question.

What is new since the harness was frozen
----------------------------------------
`scripts/experiments/orderflow_absorption_backtest.py` (the frozen rules)
computes `delta = 2*taker_buy - volume` from Binance BTCUSDT bars -- an
*estimate* of aggressor flow. Webull now serves a true per-price footprint
ladder (`buy_detail` / `sell_detail`) on MNQ, so this script feeds the frozen
funnel a true ladder-derived delta instead.

This module does NOT re-implement the rules. It imports the frozen harness and
calls its definitions directly (`build_days`, `context`, `prior_va`,
`swing_leg`, `fib_zone`, `simulate`, ...). The freeze stays single-sourced.
The import uses `importlib` from the file path only so the script does not
depend on `scripts/experiments` being importable as a package; the harness'
`main()` is guarded by `if __name__ == "__main__"`, so importing it has no
side effect beyond creating its (empty) `reports/.orderflow_cache/` directory.

The price-bar problem (read this before trusting any number)
------------------------------------------------------------
The frozen rules need per-bar OHLC. The footprint record carries NO OHLC.
Every MNQ price endpoint on this credential is still LOCKED (re-tested fresh
2026-10-15):

    get_futures_bars     -> MARKET_DATA_NOT_SUBSCRIBED:...US_FUTURES.
    get_futures_snapshot -> MARKET_DATA_NOT_SUBSCRIBED:...US_FUTURES.
    get_futures_tick     -> MARKET_DATA_NOT_SUBSCRIBED:...US_FUTURES.
    get_futures_depth    -> MARKET_DATA_NOT_SUBSCRIBED:...FUTURES LV2.
    get_stock_bars / _single with MNQZ6 -> INVALID_SYMBOL.

So this script derives what the ladder CAN give exactly, and refuses to fake
what it cannot:

  * `high` / `low` : EXACT. The traded price range = max/min over the union of
    `buy_detail` keys and `sell_detail` keys. Every price that traded is in the
    record, so this is the true bar range, not an estimate.
  * `volume`       : EXACT. `total`.
  * `delta`        : EXACT. `delta` (a true ladder delta, unlike the harness's
    BTC estimate). `tb` is back-solved as `(total + delta)/2` purely so the
    frozen `B.delta` property reproduces the wire delta; it is NOT used as a
    buy-volume estimate anywhere.
  * `open`/`close` : NOT AVAILABLE. Approximated by the bar VWAP (size-weighted
    mean traded price), which the ladder gives exactly too. A VWAP is a level,
    not a close, so it is flagged everywhere it is used.

Which frozen rules this weakens (honesty ledger):

  * context: prior-3-session structure   -> bar high/low only. FAITHFUL.
  * context: prior-day value area        -> bar high/low + volume. FAITHFUL.
  * participation floor                  -> bar volume. FAITHFUL.
  * location: Fib discount zone          -> swing low/high, i.e. bar low/high.
                                            FAITHFUL.
  * invalidation: 5m close below 0.886   -> needs CLOSE. APPROXIMATED.
  * absorption: closes off its low,
    closes bullish                       -> needs CLOSE. APPROXIMATED.
  * dominance shift: closes bullish,
    above prior close                    -> needs CLOSE. APPROXIMATED.
  * entry = next 5m open                 -> needs OPEN. APPROXIMATED.
  * execution: entry/stop/target/R       -> depends on the entry open. APPROXIMATED.

Crucially, the first five stages are the ones the funnel dies on here, and
they are all FAITHFUL. The approximated stages are downstream of
`candidates`, which is already 0, so the zero does not rest on a fabricated
close.

The frozen `context()` direction quirk
--------------------------------------
`context()` labels "up" when the three prior sessions have *descending* highs
and lows (it compares `d1 > d2 > d3`, with `d1` the oldest). The written rule
says "prior three sessions making higher highs and higher lows", which is
`d1 < d2 < d3`. This script uses the frozen function for the headline (so the
BTC zero and this zero are computed the same way) and ALSO reports the
doc-faithful direction as a labelled variant. It does not silently "fix" it.

Usage
-----
    .venv/bin/python scripts/experiments/q7_footprint_firecheck.py
    .venv/bin/python scripts/experiments/q7_footprint_firecheck.py --panel PATH

The panel is a cached `get_futures_footprint` payload (a JSON list of
per-symbol groups). Pull one with the `user-webull` MCP tool and drop it under
`reports/.webull_cache/` (gitignored). This script makes NO network calls.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import statistics as st
import sys
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "reports" / ".webull_cache"
HARNESS = Path(__file__).resolve().parent / "orderflow_absorption_backtest.py"
DEFAULT_PANEL = CACHE / "MNQZ6_footprint_M5_fresh.json"
NY = ZoneInfo("America/New_York")


def load_harness():
    """Import the frozen harness by path so the rules stay single-sourced."""
    spec = importlib.util.spec_from_file_location("q7_frozen_harness", HARNESS)
    if spec is None or spec.loader is None:      # pragma: no cover
        raise RuntimeError(f"cannot load frozen harness at {HARNESS}")
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: `@dataclass` resolves annotations through
    # sys.modules[cls.__module__], which is None until the module is added.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _row_ms(s: str) -> int:
    return int(dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp() * 1000)


def load_panel(path: Path) -> tuple[str, list[dict]]:
    """Return (symbol, bar_list) from a raw get_futures_footprint payload."""
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):
        raw = [raw]
    grp = raw[0]
    return grp.get("symbol", "?"), grp.get("result", [])


def ladder_extent(bar: dict) -> tuple[float, float, float, float, float]:
    """(low, high, vwap, volume, delta) -- all EXACT from the ladder."""
    prices: dict[float, float] = {}
    for src in ("buy_detail", "sell_detail"):
        for p, sz in (bar.get(src) or {}).items():
            prices[float(p)] = prices.get(float(p), 0.0) + float(sz)
    if not prices:
        return (float("nan"),) * 5
    lo, hi = min(prices), max(prices)
    vol = sum(prices.values())
    num = sum(p * s for p, s in prices.items())
    vwap = num / vol if vol else (lo + hi) / 2.0
    return lo, hi, vwap, float(bar["total"]), float(bar["delta"])


def build_bars(mod, bars_raw: list[dict]):
    """Map footprint records to the harness `B` type.

    high/low/volume exact; open/close approximated by VWAP (flagged).
    """
    out = []
    for b in bars_raw:
        lo, hi, vwap, vol, delta = ladder_extent(b)
        if hi != hi:                       # NaN -> empty ladder, skip
            continue
        tb = (vol + delta) / 2.0           # so B.delta == wire delta
        out.append(mod.B(t=_row_ms(b["time"]), o=vwap, h=hi, l=lo, c=vwap,
                         v=vol, tb=tb, n=0))
    out.sort(key=lambda x: x.t)
    return out


def invariants(bars_raw: list[dict]) -> dict:
    """Full-panel ladder invariant audit against the wire totals."""
    bad = {"total": 0, "delta": 0, "ladder": 0}
    for b in bars_raw:
        tot, d = float(b["total"]), float(b["delta"])
        bt, sl = float(b["buy_total"]), float(b["sell_total"])
        if abs(tot - (bt + sl)) > 1e-6:
            bad["total"] += 1
        if abs(d - (bt - sl)) > 1e-6:
            bad["delta"] += 1
        lb = sum(float(v) for v in (b.get("buy_detail") or {}).values())
        ls = sum(float(v) for v in (b.get("sell_detail") or {}).values())
        if abs(lb - bt) > 1e-6 or abs(ls - sl) > 1e-6:
            bad["ladder"] += 1
    return bad


def contiguity(bars_raw: list[dict]) -> Counter:
    ts = sorted(_row_ms(b["time"]) for b in bars_raw)
    gaps: Counter = Counter()
    for a, z in zip(ts, ts[1:]):
        gaps[(z - a) // 60_000] += 1
    return gaps


def window_and_participation(mod, bars) -> tuple[int, int, int]:
    """Reproduce the funnel's first two stages.

    NOTE: the frozen `simulate()` does not actually gate on the participation
    floor -- the docstring and the edge matrix list it, but the code only
    records `ctx_up` / `leg` / `candidates`. So the participation count below
    is for the accounting table only; it cannot change the setup count.
    Returns (bars_in_window, participation_pass_strict, participation_pass_pooled).
    """
    open_m = mod._minutes(dt.datetime(2000, 1, 1, *mod.OPEN_NY))
    end_m = open_m + mod.WINDOW_MIN
    vols = [b.v for b in bars]
    in_win = 0
    strict = 0
    pooled = 0
    win_seen: list[float] = []
    for i, b in enumerate(bars):
        if not (open_m <= mod._minutes(mod._ny(b.t)) < end_m):
            continue
        in_win += 1
        base = vols[max(0, i - mod.VOL_REGIME_LEN):i]
        if base and b.v >= mod.PARTICIPATION_MULT * st.median(base):
            strict += 1
        if win_seen and b.v >= mod.PARTICIPATION_MULT * st.median(win_seen):
            pooled += 1
        win_seen.append(b.v)
    return in_win, strict, pooled


def context_faithful(mod, days, day) -> str:
    """The written rule: prior 3 sessions making HIGHER highs and HIGHER lows.

    Same shape as the frozen `context()` but with the comparison the doc
    states (`d1 < d2 < d3` with d1 oldest), instead of the frozen function's
    inverted `d1 > d2 > d3`.
    """
    prior = [days[d] for d in sorted(days) if d < day][-3:]
    if len(prior) < 3:
        return "mixed"
    d1, d2, d3 = prior
    if d1.hi < d2.hi < d3.hi and d1.lo < d2.lo < d3.lo:
        return "up"
    if d1.hi > d2.hi > d3.hi and d1.lo > d2.lo > d3.lo:
        return "down"
    return "mixed"


def session_report(mod, bars, days, faithful: bool = False) -> list[dict]:
    """Per-session (ctx, leg, zone-outside-value) detail, frozen order."""
    open_m = mod._minutes(dt.datetime(2000, 1, 1, *mod.OPEN_NY))
    end_m = open_m + mod.WINDOW_MIN
    n = len(bars)
    rows: list[dict] = []
    i = mod.SWING_LOOKBACK
    while i < n - 3:
        if not (open_m <= mod._minutes(mod._ny(bars[i].t)) < end_m):
            i += 1
            continue
        day = dt.datetime.fromtimestamp(bars[i].t / 1000, dt.timezone.utc).date()
        ctx = (context_faithful(mod, days, day) if faithful
               else mod.context(days, day))
        va = mod.prior_va(days, day)
        il, ih = mod.swing_leg(bars, i)
        zlo = zhi = None
        outside = None
        if ctx == "up" and va is not None and il is not None and ih is not None:
            zlo, zhi, _ = mod.fib_zone(bars[il].l, bars[ih].h)
            if zlo == zlo:
                outside = zhi < va.val
        rows.append({"day": day, "ctx": ctx,
                     "va_val": None if va is None else round(va.val, 2),
                     "leg": il is not None and ih is not None,
                     "zlo": None if zlo is None else round(zlo, 2),
                     "zhi": None if zhi is None else round(zhi, 2),
                     "outside": outside})
        last = i
        while last < n and mod._minutes(mod._ny(bars[last].t)) < end_m:
            last += 1
        i = max(last - 1, i) + 1
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", type=Path, default=DEFAULT_PANEL,
                    help=f"cached get_futures_footprint JSON (default: {DEFAULT_PANEL.name})")
    args = ap.parse_args()

    if not args.panel.exists():
        sys.exit(f"panel not found: {args.panel}\n"
                 "Pull one with the user-webull MCP tool into "
                 f"{CACHE}/ (gitignored).")

    mod = load_harness()
    symbol, bars_raw = load_panel(args.panel)
    bars = build_bars(mod, bars_raw)
    days = mod.build_days(bars)

    print("Q7 footprint FIRE-CHECK (frozen rules, true ladder delta)")
    print(f"panel : {args.panel}  symbol={symbol}  bars={len(bars_raw)}")
    t0 = dt.datetime.fromtimestamp(bars[0].t / 1000, dt.timezone.utc)
    t1 = dt.datetime.fromtimestamp(bars[-1].t / 1000, dt.timezone.utc)
    print(f"span  : {t0:%Y-%m-%d %H:%M} -> {t1:%Y-%m-%d %H:%M} UTC "
          f"({(bars[-1].t - bars[0].t) / 86_400_000:.2f} calendar days)")
    print(f"sessions built: {len(days)}  "
          f"session tags: {dict(Counter(b['trading_session'] for b in bars_raw))}")
    gaps = contiguity(bars_raw)
    print(f"gaps (minutes -> n): {dict(sorted(gaps.items()))}")
    inv = invariants(bars_raw)
    print(f"invariant violations: total!={inv['total']} delta!={inv['delta']} "
          f"ladder!={inv['ladder']}")

    in_win, strict, pooled = window_and_participation(mod, bars)
    funnel: dict = {}
    trades = mod.simulate(bars, days, funnel)

    print("\nfrozen funnel (harness `context()`)")
    print(f"  bars in the 90-minute window       : {in_win}")
    print(f"  passes the participation floor     : {strict} (strict) / {pooled} (window-pooled)")
    print(f"  value-up context                   : {funnel.get('ctx_up', 0)}")
    print(f"  has a swing leg to draw the Fib on : {funnel.get('leg', 0)}")
    print(f"  Fib zone sits outside the value area: {funnel.get('candidates', 0)}")
    print(f"  COMPLETED SETUPS                   : {len(trades)}")

    print("\nper-session detail (frozen context):")
    for r in session_report(mod, bars, days):
        print(f"  {r['day']}  ctx={r['ctx']:5s}  va_val={r['va_val']}  "
              f"leg={r['leg']}  zone=[{r['zlo']},{r['zhi']}]  outside={r['outside']}")

    print("\nvariant: doc-faithful context direction (higher highs / higher lows)")
    fr: dict = {}
    trades_f = mod.simulate(bars, days, fr)  # simulate still uses frozen ctx;
    rows_f = session_report(mod, bars, days, faithful=True)
    ctx_up_f = sum(1 for r in rows_f if r["ctx"] == "up")
    leg_f = sum(1 for r in rows_f if r["ctx"] == "up" and r["leg"])
    cand_f = sum(1 for r in rows_f if r["outside"] is True)
    print(f"  value-up context                   : {ctx_up_f}")
    print(f"  has a swing leg to draw the Fib on : {leg_f}")
    print(f"  Fib zone sits outside the value area: {cand_f}")
    print("  (confirmation is reached only if the zone-armed count > 0; the "
          "close/open stages are APPROXIMATED, see module docstring)")

    if trades:
        print("\nsetups fired (shapes only -- NOT a result):")
        for t in trades:
            print(f"  entry={t.entry:.2f} stop={t.stop:.2f} target={t.target:.2f} "
                  f"exit={t.exit:.2f} reason={t.reason} R={t.R:.2f} netR={t.net_R:+.3f}")
        print(f"  exit reasons: {dict(Counter(t.reason for t in trades))}")
        print("  n is far too small for any split; no holdout is opened.")
    else:
        print("\nZERO setups. The funnel dies at the location stage "
              "(zone not armed in discount).")

    print("\nfunnel-side note: `simulate()` does not gate on participation, "
          "so the participation row cannot change the setup count.")

    return trades


if __name__ == "__main__":
    main()
