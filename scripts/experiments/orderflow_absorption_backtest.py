#!/usr/bin/env python
"""Orderflow absorption strategy ("World Champion", Chris Creamer).

Source: IQCapital, "Trading WORLD CHAMPION Reveals the Orderflow Strategy
That Won the Robbins Cup", https://www.youtube.com/watch?v=PL7LKUsCgIQ
(58 min, Aug 2026). Chris Creamer won the Robbins World Cup micro
division with a 100% month. He reports 60-65% win rate and ~1.8 profit
factor, aiming for 1.5-2R.

Framework: environment -> location -> confirmation -> execution.

  ENVIRONMENT
    * higher-timeframe structure: value up, value down, or sideways.
    * gamma regime. Positive gamma -> dealers sell rips / buy dips ->
      volatility damping -> failed breakouts are common. Negative gamma
      -> dealers buy rips / sell dips -> amplification.
    * participation floor; he will not trade when volume dies.

  LOCATION
    * prior session value area (VAL / VAH / POC). Below = discount.
    * in a value-up structure, buy pullbacks into DISCOUNT; never sell
      premium. Discount zone = Fib 0.705 / 0.786 / 0.886 on the recent
      swing low -> swing high, and the zone must sit OUTSIDE the value
      area. 0.886 is the invalidation line.

  CONFIRMATION (5-minute order flow)
    * absorption: aggressive sellers hit the lows (negative delta) at
      the extreme of a candle and are not rewarded with progress.
    * dominance shift: that candle closes bullish, then the next candle
      pulls back, sellers try again and FAIL HIGHER, and it flips
      bullish again.

  EXECUTION
    * enter on the flip; stop beyond the failed sellers; target the
      prior swing high (or 1.5R), trail toward 1.5-2R.

Honest limits of this harness
-----------------------------
Two inputs the video leans on are not freely available as deep history:

  * GEX regime. Historical option chains are not served for past dates
    and CBOE data is paid. This harness substitutes a PRICE-DERIVED
    proxy: realized vol above its own rolling median = the amplifying
    ("negative gamma") regime, below = the damping regime. That tests
    the behavioural claim, not the options positioning. A live Deribit
    read is possible; a multi-year one is not.
  * true footprint / bid-by-ask ladder. Not available as history. The
    harness uses the exchange's own aggressor split -- Binance publishes
    taker_buy_base per candle, so delta = 2*taker_buy - volume. Real
    orderflow, one level coarser than a footprint.

Every discretionary choice (fib anchor, target, trailing) is frozen to
a mechanical rule below, before any test, as the house rules require.

Data: frozen Binance Vision 5-minute panels with aggressor volume.
Costs: 15 bp round trip on notional.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics as st
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "reports" / ".orderflow_cache"
CACHE.mkdir(parents=True, exist_ok=True)
KLINE = "https://data-api.binance.vision/api/v3/klines"
NY = ZoneInfo("America/New_York")

# ---- frozen cost model ----------------------------------------------------
FEE_PER_SIDE = 0.00045
SLIP_PER_SIDE = 0.00030
COST_ROUND_TRIP = 2 * (FEE_PER_SIDE + SLIP_PER_SIDE)      # 15 bp

# ---- frozen strategy parameters -------------------------------------------
OPEN_NY = (9, 30)          # New York cash open
WINDOW_MIN = 90            # he trades the first ~90 minutes
FLAT_NY = (16, 0)          # flat by the cash close
VA_FRACTION = 0.70
PROFILE_BINS = 60
FIB_LOW, FIB_MID, FIB_HIGH = 0.705, 0.786, 0.886
SWING_K = 5
SWING_LOOKBACK = 288       # ~1 day of 5m bars
ABSORB_DELTA_Z = -0.75     # seller aggression vs recent delta
PARTICIPATION_MULT = 0.6
VOL_REGIME_LEN = 288
MOVE_TO_BE_AT = 1.0
TARGET_FLOOR_R = 1.5
SCATTER_FRAC = 0.70
MIN_TRADES = 80


@dataclass
class B:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float
    tb: float
    n: int

    @property
    def delta(self) -> float:
        return 2.0 * self.tb - self.v


@dataclass
class Trade:
    i: int
    entry: float
    stop: float
    target: float
    exit: float
    reason: str
    R: float
    gross_R: float
    net_R: float
    regime: str
    context: str
    session: str


def _ny(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, NY)


def _minutes(d: dt.datetime) -> int:
    return d.hour * 60 + d.minute


# ------------------------------------------------------------------- data
def fetch(symbol: str, interval: str, days: int) -> list[B]:
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
    seen = {int(r[0]): r for r in raw}
    return [B(t=int(r[0]), o=float(r[1]), h=float(r[2]), l=float(r[3]),
              c=float(r[4]), v=float(r[5]), tb=float(r[9]), n=int(r[8]))
            for _, r in sorted(seen.items(), key=lambda kv: kv[0])]


# --------------------------------------------------- per-session aggregates
@dataclass
class DayAgg:
    day: dt.date
    hi: float
    lo: float
    val: float
    vah: float
    poc: float


def build_days(bars: list[B]) -> dict[dt.date, DayAgg]:
    """One pass: profile (value area) and range per UTC day."""
    buckets: dict[dt.date, list[B]] = {}
    for b in bars:
        buckets.setdefault(dt.datetime.fromtimestamp(
            b.t / 1000, dt.timezone.utc).date(), []).append(b)
    out: dict[dt.date, DayAgg] = {}
    for day, seg in buckets.items():
        if len(seg) < 20:
            continue
        hi = max(b.h for b in seg)
        lo = min(b.l for b in seg)
        if hi <= lo:
            continue
        w = (hi - lo) / PROFILE_BINS
        bins = [0.0] * PROFILE_BINS
        for b in seg:
            if b.h <= b.l:
                k = min(PROFILE_BINS - 1, max(0, int((b.c - lo) / w)))
                bins[k] += b.v
                continue
            a = max(0, int((b.l - lo) / w))
            z = min(PROFILE_BINS - 1, int((b.h - lo) / w))
            span = z - a + 1
            add = b.v / span
            for k in range(a, z + 1):
                bins[k] += add
        total = sum(bins)
        if total <= 0:
            continue
        poc = max(range(PROFILE_BINS), key=lambda k: bins[k])
        acc, a, z = bins[poc], poc, poc
        while acc < VA_FRACTION * total and (a > 0 or z < PROFILE_BINS - 1):
            dn = bins[a - 1] if a > 0 else -1.0
            up = bins[z + 1] if z < PROFILE_BINS - 1 else -1.0
            if up >= dn:
                z += 1
                acc += max(up, 0.0)
            else:
                a -= 1
                acc += max(dn, 0.0)
        out[day] = DayAgg(day=day, hi=hi, lo=lo, val=lo + a * w,
                          vah=lo + (z + 1) * w, poc=lo + (poc + 0.5) * w)
    return out


def context(days: dict[dt.date, DayAgg], day: dt.date) -> str:
    prior = [days[d] for d in sorted(days) if d < day][-3:]
    if len(prior) < 3:
        return "mixed"
    (d1, d2, d3) = prior
    if d1.hi > d2.hi > d3.hi and d1.lo > d2.lo > d3.lo:
        return "up"
    if d1.hi < d2.hi < d3.hi and d1.lo < d2.lo < d3.lo:
        return "down"
    return "mixed"


def prior_va(days: dict[dt.date, DayAgg], day: dt.date) -> DayAgg | None:
    prior = [days[d] for d in sorted(days) if d < day]
    return prior[-1] if prior else None


# ------------------------------------------------------- regime proxy
def vol_regime(bars: list[B], i: int) -> str:
    lo = max(1, i - VOL_REGIME_LEN * 4)
    rets = []
    for k in range(lo, i):
        if bars[k - 1].c > 0:
            rets.append(abs(bars[k].c / bars[k - 1].c - 1.0))
    if len(rets) < VOL_REGIME_LEN * 2:
        return "positive"
    blocks = [st.mean(rets[k:k + VOL_REGIME_LEN])
              for k in range(0, len(rets) - VOL_REGIME_LEN, VOL_REGIME_LEN)]
    if not blocks:
        return "positive"
    recent = blocks[-1]
    return "negative" if recent > st.median(blocks) else "positive"


# ------------------------------------------------------------- location
def swing_leg(bars: list[B], i: int) -> tuple[int | None, int | None]:
    lo_i = max(0, i - SWING_LOOKBACK)
    lows, highs = [], []
    for k in range(lo_i + SWING_K, i - SWING_K):
        win = bars[k - SWING_K:k + SWING_K + 1]
        if bars[k].l <= min(x.l for x in win):
            lows.append(k)
        if bars[k].h >= max(x.h for x in win):
            highs.append(k)
    if not lows:
        return None, None
    il = lows[-1]
    after = [k for k in highs if k > il]
    ih = max(after, key=lambda k: bars[k].h) if after else None
    return il, ih


def fib_zone(plo: float, phi: float) -> tuple[float, float, float]:
    """(zone_low, zone_high, invalidation). Retracement drawn low->high,
    so the 0.886 level sits deepest."""
    if phi <= plo:
        return (float("nan"),) * 3
    rng = phi - plo
    return (phi - FIB_HIGH * rng, phi - FIB_LOW * rng, phi - FIB_HIGH * rng)


# --------------------------------------------------------- confirmation
def delta_z(bars: list[B], i: int, n: int = 100) -> tuple[float, float]:
    seg = [bars[k].delta for k in range(max(0, i - n), i)]
    if len(seg) < 20:
        return 0.0, 1.0
    return st.mean(seg), (st.pstdev(seg) or 1.0)


def absorption_and_shift(bars: list[B], i: int, zlo: float, zhi: float,
                         inval: float) -> bool:
    """Frozen two-bar confirmation, exactly as described on the board."""
    if i + 1 >= len(bars):
        return False
    a, b = bars[i], bars[i + 1]
    mu, sd = delta_z(bars, i)
    tol = 0.15 * (zhi - zlo)

    # bar a: low taps the discount zone, sellers aggressive, unrewarded
    if not (zlo - tol <= a.l <= zhi):
        return False
    if a.c < inval:                       # closed below 0.886 -> dead
        return False
    if not (a.delta < mu + ABSORB_DELTA_Z * sd):
        return False
    rng = a.h - a.l
    if rng <= 0 or (a.c - a.l) < 0.3 * rng:
        return False
    if a.c <= a.o:                        # must flip bullish
        return False

    # bar b: second attempt by sellers FAILS HIGHER, then flips bullish
    if b.l < a.l:                         # sellers reached lower -> not a failure
        return False
    if b.c <= b.o or b.c <= a.c:          # momentum must continue
        return False
    return True


# --------------------------------------------------------------- simulate
def simulate(bars: list[B], days: dict[dt.date, DayAgg],
             funnel: dict | None = None) -> list[Trade]:
    """One pre-session plan per day, then scan the window for the pattern.

    The video is explicit that the environment, the value area and the
    Fib zone are all marked BEFORE the open ("I will never do this while
    price is moving a million miles an hour"). So the zone is frozen at
    the first bar of the window and does not drift while price moves.
    """
    trades: list[Trade] = []
    n = len(bars)
    open_m = _minutes(dt.datetime(2000, 1, 1, *OPEN_NY))
    end_m = open_m + WINDOW_MIN
    flat_m = _minutes(dt.datetime(2000, 1, 1, *FLAT_NY))

    # index the first bar of each session window
    i = SWING_LOOKBACK
    while i < n - 3:
        ny = _ny(bars[i].t)
        if not (open_m <= _minutes(ny) < end_m):
            i += 1
            continue

        day = dt.datetime.fromtimestamp(bars[i].t / 1000, dt.timezone.utc).date()
        ctx = context(days, day)
        va = prior_va(days, day)
        il, ih = swing_leg(bars, i)

        plan = None
        if ctx == "up":
            funnel["ctx_up"] = funnel.get("ctx_up", 0) + 1
        if ctx == "up" and va is not None and il is not None and ih is not None:
            funnel["leg"] = funnel.get("leg", 0) + 1
            zlo, zhi, inval = fib_zone(bars[il].l, bars[ih].h)
            # the fib zone must sit in discount, outside the value area
            if zlo == zlo and zhi < va.val:
                plan = (zlo, zhi, inval, bars[ih].h, va)
                funnel["candidates"] = funnel.get("candidates", 0) + 1

        # walk to the end of this session's window
        j = i
        last = i
        while j < n and _minutes(_ny(bars[j].t)) < end_m:
            last = j
            if plan is not None and j + 1 < n:
                zlo, zhi, inval, swhigh, va = plan
                if absorption_and_shift(bars, j, zlo, zhi, inval):
                    tr = _enter(bars, j, n, zlo, zhi, inval, swhigh, flat_m, va)
                    if tr is not None:
                        trades.append(tr)
                        j = tr.i
            j += 1
        i = max(last, i) + 1
    return trades


def _enter(bars: list[B], i: int, n: int, zlo: float, zhi: float,
           inval: float, swhigh: float, flat_m: int,
           va: DayAgg) -> Trade | None:
    j = i + 2
    if j >= n:
        return None
    entry = bars[j].o
    stop = min(bars[i].l, bars[i + 1].l) * (1 - 0.0005)
    R = entry - stop
    if R <= 0:
        return None
    target = max(swhigh, entry + TARGET_FLOOR_R * R)

    exit_px, reason, k = None, "eod", j
    live = stop
    trailed = False
    for k in range(j, n):
        b = bars[k]
        if b.l <= live:
            exit_px, reason = live, ("stop" if not trailed else "be")
            break
        if b.h >= target:
            exit_px, reason = target, "target"
            break
        if not trailed and b.h >= entry + MOVE_TO_BE_AT * R:
            live, trailed = entry, True
        if _minutes(_ny(b.t)) >= flat_m:
            exit_px, reason, k = b.c, "eod", k
            break
    if exit_px is None:
        exit_px, reason, k = bars[n - 1].c, "eod", n - 1

    gross_px = exit_px - entry
    cost_px = entry * COST_ROUND_TRIP
    return Trade(
        i=j, entry=entry, stop=stop, target=target, exit=exit_px,
        reason=reason, R=R, gross_R=gross_px / R,
        net_R=(gross_px - cost_px) / R,
        regime=vol_regime(bars, i),
        context=str(dt.datetime.fromtimestamp(bars[i].t / 1000, dt.timezone.utc).date()),
        session=str(dt.datetime.fromtimestamp(bars[i].t / 1000, dt.timezone.utc).date()))


# -------------------------------------------------------------- reporting
def report(label: str, trades: list[Trade]) -> dict:
    if not trades:
        print(f"  {label:24s} no trades")
        return {}
    wins = [t for t in trades if t.net_R > 0]
    gw = sum(t.net_R for t in wins)
    gl = -sum(t.net_R for t in trades if t.net_R <= 0)
    pf = (gw / gl) if gl > 0 else float("inf")
    net = st.mean(t.net_R for t in trades)
    sd = st.pstdev([t.net_R for t in trades]) if len(trades) > 1 else 0.0
    tstat = net / (sd / len(trades) ** 0.5) if sd > 0 else float("nan")
    print(f"  {label:24s} n={len(trades):4d}  win={len(wins)/len(trades):5.1%}  "
          f"netR={net:+.3f}  PF={pf:5.2f}  t={tstat:+.2f}")
    return {"n": len(trades), "win": len(wins) / len(trades), "net": net,
            "pf": pf, "t": tstat}


def main() -> None:
    days_back = int(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 1095
    print("Orderflow absorption strategy (Chris Creamer, Robbins Cup winner)")
    print(f"window {OPEN_NY[0]:02d}:{OPEN_NY[1]:02d} NY +{WINDOW_MIN}min | "
          f"fib {FIB_LOW}/{FIB_MID}/{FIB_HIGH} outside value | "
          f"cost {COST_ROUND_TRIP*1e4:.0f}bp RT")
    print("claimed: 60-65% win rate, ~1.8 profit factor, 1.5-2R targets\n")

    bars = fetch("BTCUSDT", "5m", days_back)
    days = build_days(bars)
    print(f"panel: {len(bars)} bars  "
          f"{dt.datetime.fromtimestamp(bars[0].t/1000,dt.timezone.utc):%Y-%m-%d} -> "
          f"{dt.datetime.fromtimestamp(bars[-1].t/1000,dt.timezone.utc):%Y-%m-%d}")
    print(f"sessions: {len(days)}   value-up days: "
          f"{sum(1 for d in days if context(days,d)=='up')}")

    funnel: dict = {}
    trades = simulate(bars, days, funnel)
    print(f"\nfunnel: value-up sessions {funnel.get('ctx_up',0)} -> "
          f"with a swing leg {funnel.get('leg',0)} -> "
          f"fib zone outside value {funnel.get('candidates',0)} -> "
          f"completed setups {len(trades)}")
    cut = bars[0].t + int((bars[-1].t - bars[0].t) * SCATTER_FRAC)
    sc = [t for t in trades if bars[t.i].t <= cut]
    ho = [t for t in trades if bars[t.i].t > cut]

    print()
    full = report("ALL setups", trades)
    a = report("scatter (first 70%)", sc)
    b = report("holdout (last 30%)", ho)

    if trades:
        by_reg: dict[str, list[Trade]] = {}
        for t in trades:
            by_reg.setdefault(t.regime, []).append(t)
        print("\n  by regime proxy (price-derived, NOT true GEX):")
        for r in sorted(by_reg):
            report(f"    {r}-gamma proxy", by_reg[r])
        reasons: dict[str, int] = {}
        for t in trades:
            reasons[t.reason] = reasons.get(t.reason, 0) + 1
        print(f"\n  exits: {reasons}")
        print(f"  mean R: {st.mean(t.R for t in trades):.1f} "
              f"({st.mean(t.R/t.entry*1e4 for t in trades):.0f} bp of price)")

    print("\n--- verdict ---")
    if not full:
        print("  No setups fired. Check the frozen definitions before concluding.")
        return
    print(f"  claimed win rate       : 60-65%")
    print(f"  observed win rate      : {full['win']:.1%}")
    print(f"  claimed profit factor  : ~1.8")
    print(f"  observed profit factor : {full['pf']:.2f}")
    print(f"  observed net R/trade   : {full['net']:+.3f}")
    if full["net"] > 0 and full["t"] >= 2 and a and b and a["net"] > 0 and b["net"] > 0:
        print("  VERDICT: net-positive, same sign in both halves.")
        print("  Not a reason to trade it. Sizing is a separate decision.")
    else:
        print("  VERDICT: does not reproduce the claim on this data. Holdout closed.")


if __name__ == "__main__":
    main()
