#!/usr/bin/env python
"""Q9: Ross Cameron's "first pullback" entry -- a DIFFERENTIAL test on a fixed
momentum-event population, split by liquidity bucket.

Source: "Growing a $2k Account to $65,662.04 in 30 Days (My Day Trading
Strategy)," Ross Cameron, Warrior Trading, https://www.youtube.com/watch?v=xGIa8Vg0PWM.
See the Q9 section of docs/DAY_TRADE_EDGE_MATRIX.md for the frozen spec. This
module implements exactly that spec and nothing else.

WHAT IS TESTED, AND WHAT IS NOT
-------------------------------
The video carries NO statistical evidence: one narrated worked example plus a
self-reported, unaudited $2k -> $65,662 account-growth claim. There is no win
rate, no sample and no profit factor, and the source says his results are not
typical. This harness does NOT treat that claim as a result and does NOT try to
reproduce his universe.

His edge claim LOCATES the profitability in step 1, which is not reproducible
here: relative volume, total volume, the gap and the price band map onto a
crypto pair, but the share-float pillar has no crypto analog, and he says the
pattern "does not work very well" on liquid names -- which is where this test
must run. The frozen screen is therefore relative volume plus the momentum
move, and the test runs where the source says the pattern fails.

What IS testable is his stated MECHANISM: "once it pulls back... that becomes
your low... and that is your stop", i.e. patience in the pullback buys a
tighter stop, so the same move yields a far better payoff ratio. The claim is
about payoff geometry, not accuracy. So:

    Given an IDENTICAL momentum event, does entering on the first-pullback
    "crossing candle" beat CHASING the extension?

    Both arms are scored on the SAME events. Hit rate AND realized R are both
    reported, because a pullback entry can win on R while losing on hit rate,
    and a design that measured only win rate would miss the source's claim.

The design is the mirror of Q8: Q8 held the ZONE set fixed and varied the
confirmation label; Q9 holds the EVENT set fixed and varies the ENTRY rule.
Pairing removes event selection from the comparison and leaves only the entry.

FROZEN, EX ANTE (see the matrix row; do not change these after seeing output)
-----------------------------------------------------------------------------
  DISP_ATR_K = 1.5, DISP_MAX_BARS = 3   "squeeze" = <=3 consecutive same-direction
                                        (all-green) 5m bars whose net move is
                                        >= 1.5 * ATR(14) measured before the run
  RVOL_MULT = 5.0, RVOL_WIN = 288       his 5x relative-volume floor: the run's
                                        summed volume vs 5x *prior* 288-bar median
  RETRACE_MAX = 0.50                    his disclosed retracement cap
  VWAP + 9 EMA hold                     every pullback bar's LOW at/above both
  crossing candle, stop = pullback low, target = 2R, MAX_HOLD = 36 bars

Every other discretionary choice is a NAMED PARAMETER below and is MY
operationalization, not the source's rule. The sensitivity grid reports each;
none is tuned to a result. Nothing is changed after the run.

DATA
----
Binance Vision 5-minute spot bars, 30 USDT pairs (large -> small liquidity),
frozen to reports/.binance_cache/ by scripts/feeds/binance_vision_bulk.py
before any statistic is computed.

Run:
    .venv/bin/python scripts/experiments/q9_first_pullback_differential.py
    .venv/bin/python scripts/experiments/q9_first_pullback_differential.py --grid
    .venv/bin/python scripts/experiments/q9_first_pullback_differential.py --pairs BTCUSDT ETHUSDT
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "feeds"))
from hyperliquid_bot.indicators import atr, ema  # noqa: E402
import binance_vision_bulk as bvb  # noqa: E402

# ---- locked ex ante (the numbers the row freezes) --------------------------
DISP_ATR_K = 1.5          # "sharp and aggressive move" vs ATR(14)
DISP_MAX_BARS = 3         # the run may span at most this many 5m bars
RVOL_MULT = 5.0           # his 5x relative-volume participation floor
RVOL_WIN = 288            # prior 288 bars (1 day of 5m) for the volume median
RETRACE_MAX = 0.50        # his disclosed "no more than 50%" retracement cap
TARGET_R = 2.0            # his disclosed target
MAX_HOLD = 36             # time stop: 36 bars = 3 hours, then flat at close
SCATTER_FRAC = 0.70       # first 70% of calendar time
MIN_PAIRS = 120           # rule 3 floor, on paired events
EMA_LEN = 9               # his 9 EMA

# ---- my operationalizations (named, disclosed, not the source's rules) -----
PB_MAX_BARS = 12          # pullback + crossing candle must complete in this many bars
HOLD_TEST = "low"         # VWAP/EMA hold tested on the bar LOW (locked);
                          # "close" is a disclosed grid variant
CROSS_TEST = "bar_high"   # "new high vs the prior candle" = high[k] > high[k-1]
MIN_R_ATR = 0.25          # drop stops tighter than 0.25*ATR(14): an R that small
                          # makes costs explode, the ifvg convention. Both arms
                          # get the same filter, so it cannot bias the gap.
APPLY_VOL_GREEN = False   # source rule (ii) "higher volume on green candles than
                          # red" -- only as a grid variant, not locked
ATR_LEN = 14
WARMUP = max(RVOL_WIN, ATR_LEN) + 5

# ---- cost (matrix taker row: 4.5 bp fee + 3 bp slippage, each side) --------
COST_BP = 15.0


# ============================================================== data / panel
@dataclass
class Pair:
    """A frozen 5m panel with the indicators the rule needs."""
    symbol: str
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    qv: np.ndarray
    day: np.ndarray          # UTC date ordinal
    atr: np.ndarray
    ema9: np.ndarray
    vwap: np.ndarray
    vmed: np.ndarray         # rolling prior-288-bar median volume
    med_dollar_vol: float    # median per-bar quote volume (liquidity rank)

    def __len__(self) -> int:
        return len(self.t)


def _session_vwap(h, l, c, v, day) -> np.ndarray:
    """VWAP anchored to the UTC day, using the typical price (h+l+c)/3.

    Resets at each UTC midnight. A rolling VWAP would be a different series;
    the source's "does not break VWAP" is the session line. Vectorised over
    day-blocks so a multi-year panel is cheap.
    """
    tp = (h + l + c) / 3.0
    pv = tp * v
    out = np.empty_like(c)
    # day boundaries
    bnd = np.flatnonzero(np.concatenate([[True], day[1:] != day[:-1]]))
    bnd = np.append(bnd, len(day))
    for a, b in zip(bnd[:-1], bnd[1:]):
        cpv = np.cumsum(pv[a:b])
        cv = np.cumsum(v[a:b])
        with np.errstate(invalid="ignore", divide="ignore"):
            out[a:b] = cpv / cv
    out[~np.isfinite(out)] = np.nan
    return out


def _rolling_median_vol(v: np.ndarray, win: int) -> np.ndarray:
    """Median volume of the `win` bars strictly BEFORE each index.

    Exact, not approximated: `np.median` over a strided view in chunks. Uses
    the prior bars only, so the gate never sees the move it is gating.
    """
    n = len(v)
    out = np.full(n, np.nan)
    if n <= win:
        return out
    from numpy.lib.stride_tricks import sliding_window_view
    sw = sliding_window_view(v, win)          # sw[j] = v[j : j+win]
    med = np.empty(len(sw))
    step = 20_000
    for a in range(0, len(sw), step):
        med[a:a + step] = np.median(sw[a:a + step], axis=1)
    out[win:] = med[:n - win]                 # out[i] = median(v[i-win:i])
    return out


def build_pair(symbol: str, interval: str = "5m",
               skip_download: bool = False) -> Pair:
    p = (bvb.load_panel(symbol, interval) if skip_download
         else bvb.build_panel(symbol, interval))
    day = np.array([dt.datetime.fromtimestamp(int(x) / 1000, dt.timezone.utc)
                    .date().toordinal() for x in p.t], dtype=np.int64)
    a = atr(p.h, p.l, p.c, ATR_LEN)
    e9 = ema(p.c, EMA_LEN)
    vw = _session_vwap(p.h, p.l, p.c, p.v, day)
    vmed = _rolling_median_vol(p.v, RVOL_WIN)
    return Pair(symbol=symbol, t=p.t, o=p.o, h=p.h, l=p.l, c=p.c, v=p.v,
                qv=p.qv, day=day, atr=a, ema9=e9, vwap=vw, vmed=vmed,
                med_dollar_vol=float(np.median(p.qv)))


# ============================================================== event scan
@dataclass
class Event:
    symbol: str
    day: int
    base: int            # index of the base candle (i-1)
    move_start: int      # index of the first bar of the run
    move_end: int        # index of the first bar AFTER the run
    leg_low: float
    leg_high: float
    atr_at: float
    # pullback arm
    has_pullback: bool = False
    cross_bar: int = -1
    pb_low: float = float("nan")
    # scored results (filled later)
    pb: "ArmResult | None" = None
    ch: "ArmResult | None" = None


@dataclass
class ArmResult:
    entry: float
    stop: float
    target: float
    exit_px: float
    reason: str
    gross_R: float
    net_R: float
    R_abs: float
    entry_idx: int
    bars_held: int


def candidate_moves(p: Pair, *, disp_k: float, disp_max: int,
                    rvol_mult: float) -> np.ndarray:
    """Vectorised scan. Returns `best[i]` = the smallest run length m (1..disp_max)
    for which the run starting at bar i is all-green, moves >= disp_k*ATR(14)
    before the run, and its summed volume clears rvol_mult*m*prior-median.
    -1 where no run qualifies. This is the locked momentum-event definition."""
    n = len(p.t)
    atrp = np.empty(n)
    atrp[0] = np.nan
    atrp[1:] = p.atr[:-1]
    vmedp = np.empty(n)
    vmedp[0] = np.nan
    vmedp[1:] = p.vmed[:-1]

    pad = disp_max
    green = (p.c > p.o)
    gpad = np.concatenate([green, np.zeros(pad, dtype=bool)])
    cpad = np.concatenate([p.c, np.full(pad, np.nan)])
    vpad = np.concatenate([p.v, np.zeros(pad)])
    idx = np.arange(n)

    best = np.full(n, -1, dtype=np.int64)
    run_g = np.ones(n, dtype=bool)
    sum_v = np.zeros(n)
    ii = np.arange(n)
    for m in range(1, disp_max + 1):
        run_g &= gpad[m - 1:m - 1 + n]
        sum_v = sum_v + vpad[m - 1:m - 1 + n]
        net = cpad[m - 1:m - 1 + n] - p.o
        ok = (run_g
              & np.isfinite(atrp) & np.isfinite(vmedp)
              & (net >= disp_k * atrp)
              & (sum_v >= rvol_mult * m * vmedp)
              & (ii + m - 1 < n - 2))
        best = np.where((best < 0) & ok, m, best)
    return best


def detect_events(p: Pair, *, disp_k: float, disp_max: int, rvol_mult: float,
                  retrace_max: float, pb_max: int, hold_test: str,
                  cross_test: str, target_r: float, max_hold: int,
                  cost_bp: float, min_r_atr: float,
                  apply_vol_green: bool) -> list[Event]:
    """Find the momentum-event population, then the first-pullback entry.

    Greedy in bar order, exactly as the locked rule reads: after an accepted
    event the scan resumes at the first bar following its run, so overlapping
    runs are not counted twice.
    """
    n = len(p.t)
    best = candidate_moves(p, disp_k=disp_k, disp_max=disp_max,
                           rvol_mult=rvol_mult)
    cand = np.flatnonzero(best >= 0)
    events: list[Event] = []
    cursor = WARMUP
    for i in cand:
        if i < cursor:
            continue
        m = int(best[i])
        e = i + m - 1
        leg_low = float(p.l[max(0, i - 1):e + 1].min())
        leg_high = float(p.h[max(0, i - 1):e + 1].max())
        if leg_high <= leg_low:
            cursor = e + 1
            continue
        ev = Event(symbol=p.symbol, day=int(p.day[i]), base=i - 1,
                   move_start=int(i), move_end=e + 1, leg_low=leg_low,
                   leg_high=leg_high, atr_at=float(p.atr[i - 1]))
        _attach_pullback(p, ev, retrace_max=retrace_max, pb_max=pb_max,
                         hold_test=hold_test, cross_test=cross_test,
                         apply_vol_green=apply_vol_green)
        events.append(ev)
        cursor = e + 1
    return events


def _attach_pullback(p: Pair, ev: Event, *, retrace_max: float, pb_max: int,
                     hold_test: str, cross_test: str,
                     apply_vol_green: bool) -> None:
    """Find the crossing candle after the move, if the pullback qualifies.

    Walks forward from the move's end. Every bar must hold VWAP and the 9 EMA
    on its LOW (the locked reading; `hold_test='close'` is the grid variant).
    The retracement is measured from the leg high to the running pullback low
    and must stay at or under `retrace_max` of the leg. The crossing candle is
    the first bar that makes a new high against the immediately prior candle
    once a pullback has begun.
    """
    n = len(p.t)
    pb_low = float("inf")
    started = False
    last = min(n - 1, ev.move_end + pb_max)
    gv = rv = 0.0
    for k in range(ev.move_end, last):
        if not (np.isfinite(p.vwap[k]) and np.isfinite(p.ema9[k])):
            return
        ref = p.l[k] if hold_test == "low" else p.c[k]
        if ref < p.vwap[k] or ref < p.ema9[k]:
            return                        # broke VWAP or the 9 EMA -> dead
        pb_low = min(pb_low, float(p.l[k]))
        if p.c[k] >= p.o[k]:
            gv += float(p.v[k])
        else:
            rv += float(p.v[k])
        if not started:
            if p.l[k] < ev.leg_high:
                started = True
            else:
                continue                  # still extending; no pullback yet
        retraced = (ev.leg_high - pb_low) / (ev.leg_high - ev.leg_low)
        if retraced > retrace_max:
            return                        # retraced too much -> dead
        new_high = (p.h[k] > p.h[k - 1]) if cross_test == "bar_high" else \
                   (p.c[k] > p.c[k - 1])
        if new_high and pb_low < ev.leg_high:
            if apply_vol_green and gv < rv:
                return                    # rule (ii) variant: green volume must lead
            ev.has_pullback = True
            ev.cross_bar = k
            ev.pb_low = pb_low
            return


# ============================================================== simulation
def simulate(p: Pair, entry_idx: int, direction: int, stop: float,
             target: float, max_hold: int, cost_bp: float) -> ArmResult:
    """Walk bars from entry. Ambiguous bars (stop AND target in one bar) resolve
    to the STOP, the pessimistic convention. Gaps are not improved. After
    `max_hold` bars the position is flat at that bar's close."""
    n = len(p.t)
    entry = float(p.o[entry_idx])
    R = abs(entry - stop)
    exit_px, reason, k = float(p.c[n - 1]), "eod", entry_idx
    for k in range(entry_idx, min(n, entry_idx + max_hold)):
        if direction == 1:
            hit_stop = p.l[k] <= stop
            hit_tgt = p.h[k] >= target
        else:
            hit_stop = p.h[k] >= stop
            hit_tgt = p.l[k] <= target
        if hit_stop:                      # stop first (pessimistic)
            exit_px, reason = stop, "stop"
            break
        if hit_tgt:
            exit_px, reason = target, "target"
            break
        exit_px, reason = float(p.c[k]), "time"
    gross = (exit_px - entry) * direction
    net = gross - entry * cost_bp / 1e4
    return ArmResult(entry=entry, stop=stop, target=target, exit_px=exit_px,
                     reason=reason, gross_R=(gross / R if R > 0 else 0.0),
                     net_R=(net / R if R > 0 else 0.0), R_abs=R,
                     entry_idx=entry_idx, bars_held=k - entry_idx)


def score_event(p: Pair, ev: Event, *, target_r: float, max_hold: int,
                cost_bp: float, min_r_atr: float) -> None:
    """Fill both arms for one event. An arm is dropped if its R is degenerate
    (tighter than min_r_atr * ATR), applied identically to both arms."""
    floor = min_r_atr * ev.atr_at

    # --- chase arm: entry next open after the move, stop at the base of the move
    j = ev.move_end
    if j < len(p.t) - max_hold - 1:
        entry = float(p.o[j])
        stop = ev.leg_low
        R = entry - stop
        if R >= floor and R > 0:
            ev.ch = simulate(p, j, 1, stop, entry + target_r * R, max_hold, cost_bp)

    # --- pullback arm: crossing candle + 1 open, stop at the pullback low
    if ev.has_pullback:
        j2 = ev.cross_bar + 1
        if j2 < len(p.t) - max_hold - 1:
            entry = float(p.o[j2])
            stop = ev.pb_low
            R = entry - stop
            if R >= floor and R > 0:
                ev.pb = simulate(p, j2, 1, stop, entry + target_r * R,
                                 max_hold, cost_bp)


# ============================================================== statistics
def cluster_t(x: np.ndarray, day: np.ndarray) -> tuple[float, float, int]:
    """Mean of x with a day-clustered t-statistic.

    Cluster-robust variance of a mean: each observation's influence on the
    mean is summed inside its UTC day, and the variance is the sum of squared
    cluster sums. Events on one day are not independent; the unit is one day.
    Because the two arms are paired, x is already a per-event difference and
    the same estimator applies to it unchanged.
    """
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n == 0:
        return (float("nan"), float("nan"), 0)
    m = x.mean()
    u = (x - m) / n
    order = np.argsort(day, kind="stable")
    ds = day[order]
    us = u[order]
    bnd = np.flatnonzero(np.concatenate([[True], ds[1:] != ds[:-1]]))
    G = len(bnd)
    sums = np.add.reduceat(us, bnd)
    if G < 2:
        return (float(m), float("nan"), G)
    var = (G / (G - 1)) * float((sums ** 2).sum())
    se = float(np.sqrt(var))
    return (float(m), (float(m) / se if se > 0 else float("nan")), G)


def describe(x: np.ndarray) -> dict:
    x = np.asarray(x, dtype=float)
    if len(x) == 0:
        return {"n": 0}
    q = np.percentile(x, [10, 25, 50, 75, 90])
    return {"n": len(x), "mean": float(x.mean()), "median": float(np.median(x)),
            "sd": float(x.std(ddof=1)) if len(x) > 1 else 0.0,
            "p10": q[0], "p25": q[1], "p50": q[2], "p75": q[3], "p90": q[4],
            "win": float((x > 0).mean())}


@dataclass
class Paired:
    """One row per event that has both arms, with the fields the report needs."""
    symbol: str
    day: int
    bucket: str
    pb_net: float
    ch_net: float
    pb_gross: float
    ch_gross: float
    pb_win: float
    ch_win: float
    pb_R_bp: float            # stop distance in bp of price
    ch_R_bp: float
    pb_reason: str
    ch_reason: str
    extras: dict = field(default_factory=dict)


def pair_up(events: list[Event], p: Pair, bucket: str) -> list[Paired]:
    out: list[Paired] = []
    for ev in events:
        if ev.pb is None or ev.ch is None:
            continue
        out.append(Paired(
            symbol=ev.symbol, day=ev.day, bucket=bucket,
            pb_net=ev.pb.net_R, ch_net=ev.ch.net_R,
            pb_gross=ev.pb.gross_R, ch_gross=ev.ch.gross_R,
            pb_win=1.0 if ev.pb.net_R > 0 else 0.0,
            ch_win=1.0 if ev.ch.net_R > 0 else 0.0,
            pb_R_bp=ev.pb.R_abs / ev.pb.entry * 1e4,
            ch_R_bp=ev.ch.R_abs / ev.ch.entry * 1e4,
            pb_reason=ev.pb.reason, ch_reason=ev.ch.reason))
    return out


def report_arms(rows: list[Paired], label: str, quiet: bool = False) -> dict:
    if not rows:
        print(f"  {label}: no paired events")
        return {"n": 0}
    pb = np.array([r.pb_net for r in rows])
    ch = np.array([r.ch_net for r in rows])
    day = np.array([r.day for r in rows])
    diff = pb - ch
    md, td, G = cluster_t(diff, day)
    mw, tw, _ = cluster_t(np.array([r.pb_win - r.ch_win for r in rows]), day)
    mr, tr, _ = cluster_t(np.array([r.pb_R_bp - r.ch_R_bp for r in rows]), day)
    # gross differential: geometry without the cost term, so the two effects can
    # be told apart (the pullback's tighter stop means cost bites it harder in R)
    gdiff = np.array([r.pb_gross - r.ch_gross for r in rows])
    mg, tg, _ = cluster_t(gdiff, day)
    # one-sample clustered t for each arm's net R vs zero: is either arm
    # positive on its own, or is the differential only a pullback failure?
    pb_m, pb_t, _ = cluster_t(pb, day)
    ch_m, ch_t, _ = cluster_t(ch, day)
    d = {"n": len(rows), "G": G,
         "pb": describe(pb), "ch": describe(ch),
         "pb_gross": describe(np.array([r.pb_gross for r in rows])),
         "ch_gross": describe(np.array([r.ch_gross for r in rows])),
         "pb_t": pb_t, "ch_t": ch_t,
         "diff_R": md, "t_R": td, "diff_win": mw, "t_win": tw,
         "diff_Rbp": mr, "t_Rbp": tr, "diff_gross": mg, "t_gross": tg,
         "pb_R_bp": describe(np.array([r.pb_R_bp for r in rows])),
         "ch_R_bp": describe(np.array([r.ch_R_bp for r in rows])),
         "diff_dist": describe(diff)}
    if not quiet:
        print(f"  {label}: paired events={len(rows)}  clustered days={G}")
        print(f"    {'':9s} {'n':>6} {'gross R':>9} {'net R':>9} {'net t':>7} "
              f"{'med net':>9} {'win':>7} {'median R(bp)':>13}")
        for key, nm, tt in (("pb", "PULLBACK", pb_t), ("ch", "CHASE", ch_t)):
            s, g = d[key], d[f"{key}_gross"]
            rbp = d[f"{key}_R_bp"]["median"]
            print(f"    {nm:9s} {s['n']:>6} {g['mean']:>+9.3f} {s['mean']:>+9.3f} "
                  f"{tt:>+7.2f} {s['median']:>+9.3f} {s['win']*100:>6.1f}% {rbp:>13.1f}")
        print(f"    differential (pullback - chase):")
        print(f"      gross R    : {mg:>+8.3f}   day-clustered t = {tg:>+.2f}")
        print(f"      net R      : {md:>+8.3f}   day-clustered t = {td:>+.2f}")
        print(f"      hit rate   : {mw*100:>+8.1f}pt  day-clustered t = {tw:>+.2f}")
        print(f"      stop dist  : {mr:>+8.1f}bp  day-clustered t = {tr:>+.2f}"
              f"   (negative = pullback stop is tighter)")
        dd = d["diff_dist"]
        print(f"      per-event net-R differential deciles (p10/25/50/75/90): "
              f"{dd['p10']:+.2f} / {dd['p25']:+.2f} / {dd['p50']:+.2f} / "
              f"{dd['p75']:+.2f} / {dd['p90']:+.2f}    sd {dd['sd']:.2f}")
    return d


# ============================================================== main
def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", nargs="*", default=None,
                    help="symbols to use (default: the frozen Q9 30-pair universe)")
    ap.add_argument("--grid", action="store_true",
                    help="also print the full sensitivity grid")
    ap.add_argument("--skip-download", action="store_true",
                    help="use only panels already cached in reports/.binance_cache/")
    ap.add_argument("--pairs-file", type=Path, default=None,
                    help="optional JSON cache of built Pair arrays (speeds reruns)")
    args = ap.parse_args()

    symbols = args.pairs or bvb.Q9_UNIVERSE

    print("=" * 82)
    print("Q9  Ross Cameron 'first pullback' -- differential test on a fixed event set")
    print("=" * 82)
    print(f"locked : squeeze >= {DISP_ATR_K}xATR({ATR_LEN}) over <= {DISP_MAX_BARS} bars, "
          f"all green; rvol >= {RVOL_MULT}x prior-{RVOL_WIN}-bar median; "
          f"retrace <= {RETRACE_MAX:.0%}; VWAP + {EMA_LEN} EMA hold (bar LOW); "
          f"stop=pullback low; target={TARGET_R}R; time stop {MAX_HOLD} bars")
    print(f"cost   : {COST_BP:.0f} bp round trip on notional (matrix taker row)")
    print(f"mode   : hold '{HOLD_TEST}', cross '{CROSS_TEST}', min-R {MIN_R_ATR}xATR, "
          f"green-volume rule {'ON' if APPLY_VOL_GREEN else 'off'}")

    # ---- build panels -----------------------------------------------------
    pairs: dict[str, Pair] = {}
    for s in symbols:
        try:
            pairs[s] = build_pair(s, skip_download=args.skip_download)
        except (SystemExit, FileNotFoundError) as e:
            print(f"  {s}: {e}")
    if not pairs:
        raise SystemExit("no panels built")

    # ---- liquidity buckets (terciles of median dollar volume) -------------
    ranked = sorted(pairs, key=lambda s: pairs[s].med_dollar_vol)
    k = len(ranked)
    t1, t2 = k // 3, 2 * k // 3
    bucket_of = {}
    for idx, s in enumerate(ranked):
        bucket_of[s] = "small" if idx < t1 else ("mid" if idx < t2 else "large")

    print()
    print("-" * 82)
    print("UNIVERSE  (sorted by median per-bar dollar volume; buckets are terciles)")
    print("-" * 82)
    tot_bars = 0
    for s in ranked:
        p = pairs[s]
        t0 = dt.datetime.fromtimestamp(int(p.t[0]) / 1000, dt.timezone.utc)
        t1x = dt.datetime.fromtimestamp(int(p.t[-1]) / 1000, dt.timezone.utc)
        tot_bars += len(p)
        print(f"  {s:12s} {bucket_of[s]:5s} {len(p):>9,} bars  "
              f"{t0:%Y-%m-%d} -> {t1x:%Y-%m-%d}  "
              f"med $vol/bar {p.med_dollar_vol/1e3:>10,.0f}k")
    print(f"  total bars: {tot_bars:,}   pairs: {len(pairs)}   buckets: "
          f"small={sum(1 for s in ranked if bucket_of[s]=='small')} "
          f"mid={sum(1 for s in ranked if bucket_of[s]=='mid')} "
          f"large={sum(1 for s in ranked if bucket_of[s]=='large')}")

    # ---- run the differential --------------------------------------------
    def full_run(**kw):
        rows: list[Paired] = []
        n_ev = n_pb = 0
        for s in symbols:
            if s not in pairs:
                continue
            p = pairs[s]
            evs = detect_events(
                p, disp_k=kw.get("disp_k", DISP_ATR_K),
                disp_max=kw.get("disp_max", DISP_MAX_BARS),
                rvol_mult=kw.get("rvol_mult", RVOL_MULT),
                retrace_max=kw.get("retrace_max", RETRACE_MAX),
                pb_max=kw.get("pb_max", PB_MAX_BARS),
                hold_test=kw.get("hold_test", HOLD_TEST),
                cross_test=kw.get("cross_test", CROSS_TEST),
                target_r=kw.get("target_r", TARGET_R),
                max_hold=kw.get("max_hold", MAX_HOLD),
                cost_bp=COST_BP, min_r_atr=kw.get("min_r_atr", MIN_R_ATR),
                apply_vol_green=kw.get("apply_vol_green", APPLY_VOL_GREEN))
            for ev in evs:
                score_event(p, ev, target_r=kw.get("target_r", TARGET_R),
                            max_hold=kw.get("max_hold", MAX_HOLD),
                            cost_bp=COST_BP,
                            min_r_atr=kw.get("min_r_atr", MIN_R_ATR))
            n_ev += len(evs)
            n_pb += sum(1 for e in evs if e.has_pullback)
            rows.extend(pair_up(evs, p, bucket_of[s]))
        return rows, n_ev, n_pb

    all_rows, n_ev, n_pb = full_run()

    print()
    print("=" * 82)
    print("FUNNEL")
    print("=" * 82)
    print(f"  momentum events detected               : {n_ev:,}")
    print(f"  ...with a qualifying pullback entry    : {n_pb:,}")
    print(f"  ...scored on BOTH arms (the paired set): {len(all_rows):,}")

    if not all_rows:
        print("\n  No paired events. Nothing to score.")
        return

    # ---- headline: first 70% of calendar time -----------------------------
    ts = np.array([r.day for r in all_rows])
    t_lo, t_hi = int(ts.min()), int(ts.max())
    cut = t_lo + SCATTER_FRAC * (t_hi - t_lo)
    sc = [r for r in all_rows if r.day <= cut]
    ho = [r for r in all_rows if r.day > cut]
    cut_date = dt.date.fromordinal(int(cut))

    print()
    print("=" * 82)
    print(f"HEADLINE -- first 70% of calendar time (through {cut_date})")
    print("=" * 82)
    d_sc = report_arms(sc, "scatter 70%")

    passed = (np.isfinite(d_sc["t_R"]) and d_sc["t_R"] >= 2.0
              and d_sc["diff_R"] > 0)
    print()
    print("KILL RULE: pass requires day-clustered t >= 2 AND the R difference > 0.")
    print(f"  realized-R difference {d_sc['diff_R']:+.3f}R  (t={d_sc['t_R']:+.2f}, "
          f"G={d_sc['G']} days, n={d_sc['n']} pairs; rule 3 floor is {MIN_PAIRS})")
    print(f"  verdict: {'CLEARS -> walk-forward may open' if passed else 'FAILS -> last 30% stays SEALED'}")

    print()
    print("=" * 82)
    if passed:
        print(f"WALK-FORWARD -- last 30%, after {cut_date}, opened once")
        print("=" * 82)
        report_arms(ho, "holdout 30%")
    else:
        print("WALK-FORWARD: NOT OPENED. The first 70% failed its kill rule, so")
        print("the last 30% of calendar time stays sealed (matrix rule 5).")
        print("No holdout statistic is computed or printed.")

    # ---- BY LIQUIDITY BUCKET (headline finding) ---------------------------
    print()
    print("=" * 82)
    print("BY-LIQUIDITY BUCKET -- the test of the supply/demand (float) thesis")
    print("=" * 82)
    for b in ("large", "mid", "small"):
        sub = [r for r in all_rows if r.bucket == b]
        if sub:
            report_arms(sub, f"bucket {b:5s} (whole span)")
    print("\n  the same breakdown on the scatter 70% only:")
    for b in ("large", "mid", "small"):
        sub = [r for r in sc if r.bucket == b]
        if sub:
            report_arms(sub, f"bucket {b:5s} (70%)")

    # ---- context: exit mix and stop distances -----------------------------
    print()
    print("=" * 82)
    print("CONTEXT -- exit mix and stop distance (his 'tighter stop' mechanism)")
    print("=" * 82)
    for key in ("pb", "ch"):
        reasons: dict[str, int] = {}
        for r in all_rows:
            rr = r.pb_reason if key == "pb" else r.ch_reason
            reasons[rr] = reasons.get(rr, 0) + 1
        Rbp = np.array([getattr(r, f"{key}_R_bp") for r in all_rows])
        print(f"  {'PULLBACK' if key == 'pb' else 'CHASE':9s}: stop distance "
              f"median {np.median(Rbp):6.1f} bp  mean {Rbp.mean():6.1f} bp   "
              f"exits {dict(sorted(reasons.items()))}")
    pbR = np.array([r.pb_R_bp for r in all_rows])
    chR = np.array([r.ch_R_bp for r in all_rows])
    ratio = np.median(pbR) / np.median(chR) if np.median(chR) else float("nan")
    print(f"  median stop ratio pullback/chase = {ratio:.3f} "
          f"(his thesis predicts < 1: the pullback pays less for the same stop risk)")

    # ---- sensitivity ------------------------------------------------------
    if args.grid:
        print()
        print("=" * 82)
        print("SENSITIVITY GRID -- each cell re-runs the WHOLE test on the 70%.")
        print("Reported for transparency; NOT a search for the best cell.")
        print("=" * 82)
        locked = {"disp_k": DISP_ATR_K, "disp_max": DISP_MAX_BARS,
                  "rvol_mult": RVOL_MULT, "retrace_max": RETRACE_MAX,
                  "pb_max": PB_MAX_BARS, "hold_test": HOLD_TEST,
                  "cross_test": CROSS_TEST, "target_r": TARGET_R,
                  "max_hold": MAX_HOLD, "min_r_atr": MIN_R_ATR,
                  "apply_vol_green": APPLY_VOL_GREEN}
        grid = [
            ("squeeze k (x ATR)", "disp_k", [1.0, 1.5, 2.0, 2.5]),
            ("squeeze window (bars)", "disp_max", [2, 3, 4]),
            ("rvol multiple (x prior median)", "rvol_mult", [3.0, 5.0, 10.0]),
            ("retracement cap", "retrace_max", [0.382, 0.50, 0.618]),
            ("pullback window (bars)", "pb_max", [6, 12, 24]),
            ("VWAP/EMA hold test", "hold_test", ["low", "close"]),
            ("crossing-candle test", "cross_test", ["bar_high", "close"]),
            ("target (R)", "target_r", [1.5, 2.0, 3.0]),
            ("time stop (bars)", "max_hold", [12, 36, 72]),
            ("min-R filter (x ATR)", "min_r_atr", [0.0, 0.25, 0.5]),
            ("green-volume rule (ii)", "apply_vol_green", [False, True]),
        ]
        for name, key, vals in grid:
            print(f"\n  -- {name} --")
            for v in vals:
                rows, _, _ = full_run(**{key: v})
                sub = [r for r in rows if r.day <= cut]
                star = "*" if v == locked[key] else " "
                if not sub:
                    print(f"    {str(v):>8s}{star}  no paired events")
                    continue
                d = report_arms(sub, "cell", quiet=True)
                print(f"    {str(v):>8s}{star}  n={d['n']:>6} G={d['G']:>5}  "
                      f"dR={d['diff_R']:>+7.3f}  t={d['t_R']:>+5.2f}  "
                      f"dWin={d['diff_win']*100:>+6.1f}pt")
        print("\n  (*) = the locked value. Every cell is the WHOLE test, not a slice.")

    print()
    print("Nothing here is a live order, a sizing change, or a claim the source")
    print("makes money. The differential tests the entry GEOMETRY on one frozen")
    print("crypto universe over one frozen span.")


if __name__ == "__main__":
    main()
