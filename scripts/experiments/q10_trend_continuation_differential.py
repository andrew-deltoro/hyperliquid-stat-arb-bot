#!/usr/bin/env python
"""Q10: The Trading Channel's trend-continuation strategy -- TWO differential
tests on a fixed zone-touch population (Binance 5m spot).

Source: "The Only Day Trading Strategy You Will Ever Need (Full Tutorial:
Beginner To Advanced)," The Trading Channel,
https://www.youtube.com/watch?v=2h5ryPi6ZYo. See the Q10 section of
docs/DAY_TRADE_EDGE_MATRIX.md for the frozen spec. This module implements
exactly that spec and nothing else.

WHAT IS TESTED, AND WHAT IS NOT
    10|-------------------------------
The video carries NO statistical evidence: the ~50% win rate at 2:1 is the
author's own description of his own results, with no backtest, no sample
size, no profit factor and no audited record. It ends with a paid mentorship
pitch. That number is treated as an unverified claim, not evidence.

This harness does NOT try to reproduce the method's profitability and does NOT
issue a verdict on the method as a whole. It isolates TWO independent claims,
each on a fixed touch population, and neither is allowed to rescue the other:

    Claim A -- the HTF trend filter has value:
        touches taken WITH the higher-timeframe trend beat touches taken
    20|        AGAINST it.
    Claim B -- the rejection pattern has selectivity:
        touches that form a valid double-top/bottom + confirming candle beat
        touches at the SAME zones that do not.

The design is differential, the same shape as Q8 and Q9. ONE touch population
is built and frozen. Each touch is labelled twice -- alignment (Claim A) and
pattern outcome (Claim B) -- and the forward outcome of the two arms is
compared on the SAME events. Holding the touch set fixed removes event
selection from the comparison and leaves only each label's own discrimination.

    30|Every touch is scored on ONE trade whose geometry depends on the pattern:
    the confirmed arm uses the rejection stop, the unconfirmed control uses the
    zone edge. Because Claim A's two arms therefore mix both geometries, a
    geometry-FREE fixed-horizon forward return (Q8's headline measure) is also
    reported for both claims, so no conclusion rests on the stop geometry alone.
    Claim A is additionally shown inside the confirmed subset and inside the
    unconfirmed subset, so the two labels cannot confound each other.

Hit rate AND realized R are both reported, because the source's claim is
specifically ~50% at 2:1 and a design that reported only one of those would
    40|misstate it. The 2:1 is checked as a *realized* R:R distribution: the target
is the prior structure extreme, not a fixed R multiple, so R:R is an outcome,
not a setting.

FROZEN, EX ANTE (see the matrix row; do not change these after seeing output)
-----------------------------------------------------------------------------
  SWING_P          = 3      fractal swing, 3 bars each side, on the 15m series
  IMPULSE_ATR_K    = 0.5    a swing becomes MAJOR only when a close goes beyond
                            it by >= 0.5 * ATR(14) (the impulsive break)
  RETRACE_MIN_ATR  = 0.5    his "at least two candles and some movement"
  ZONE_HALF_ATR    = 0.5    the broken major level +/- 0.5 * ATR(14) on 15m
  DOUBLE_WIN       = 12     bars the two peaks/troughs may span
  DOUBLE_TOL_ATR   = 0.5    the two peaks must be within 0.5 * ATR(14)
  STOP_ATR_K       = 1.0    his ATR(14) buffer beyond the rejection swing
  MAX_HOLD         = 96     time stop, 96 bars = 8h on 5m, then flat at close
  H_BP             = 24     fixed-horizon forward window for the bp measure
  COST_BP          = 15.0   matrix taker round trip (4.5 bp fee + 3 bp slip x2)
  SCATTER_FRAC     = 0.70   first 70% of calendar time
  MIN_EVENTS       = 120    rule 3 floor on touches in the scatter

Everything else is a NAMED PARAMETER below and is MY operationalization of a
rule the source leaves loose (he concedes the method is not completely
objective). The sensitivity grid reports each; none is tuned to a result.

DATA
----
Binance Vision 5-minute spot bars, the frozen 30-pair Q9 universe, already
resident under reports/.binance_cache/ (gitignored). HTF = 15m resampled from
the same 5m tape (the ratio the source specifies for a 5m trading timeframe),
so the clocks are guaranteed consistent; no separate pull.

Run:
    .venv/bin/python scripts/experiments/q10_trend_continuation_differential.py --skip-download
    .venv/bin/python scripts/experiments/q10_trend_continuation_differential.py --skip-download --grid
    .venv/bin/python scripts/experiments/q10_trend_continuation_differential.py --skip-download --pairs BTCUSDT
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.ndimage import maximum_filter1d, minimum_filter1d

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "feeds"))
from hyperliquid_bot.indicators import atr  # noqa: E402
import binance_vision_bulk as bvb  # noqa: E402

# ---- locked ex ante (the numbers the row freezes) --------------------------
SWING_P = 3
IMPULSE_ATR_K = 0.5
RETRACE_MIN_ATR = 0.5
ZONE_HALF_ATR = 0.5
DOUBLE_WIN = 12
DOUBLE_TOL_ATR = 0.5
STOP_ATR_K = 1.0
MAX_HOLD = 96
H_BP = 24
COST_BP = 15.0
SCATTER_FRAC = 0.70
MIN_EVENTS = 120

# ---- my operationalizations (named, disclosed, not the source's rules) -----
CONF_BARS = 6             # confirming candle must appear within this many bars
RETRACE_LOOKBACK = 24     # bars to look back for the move that pulled back
MIN_R_BP = 0.0            # locked: no degenerate-R filter (0 = keep every trade)
ATR_LEN = 14
HTF_MS = 15 * 60 * 1000   # the one-timeframe-up ratio: 5m -> 15m
WARMUP = 2 * SWING_P + ATR_LEN + 5


# ============================================================== data
@dataclass
class Panel:
    symbol: str
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    qv: np.ndarray
    day: np.ndarray
    atr5: np.ndarray


@dataclass
class Htf:
    """The 15m series with the structure layers the rules read."""
    bucket_start: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    atr: np.ndarray
    trend: np.ndarray            # 'up' / 'down' / 'cons' per 15m bar
    level_hi: np.ndarray         # last broken major swing high (nan if none)
    level_lo: np.ndarray         # last broken major swing low (nan if none)
    zz_h_bar: np.ndarray         # zigzag swing-high pivot indices (for targets)
    zz_h_px: np.ndarray
    zz_l_bar: np.ndarray
    zz_l_px: np.ndarray


def load_panel(symbol: str, interval: str = "5m",
               skip_download: bool = False) -> Panel:
    p = (bvb.load_panel(symbol, interval) if skip_download
         else bvb.build_panel(symbol, interval))
    day = np.array([dt.datetime.fromtimestamp(int(x) / 1000, dt.timezone.utc)
                    .date().toordinal() for x in p.t], dtype=np.int64)
    return Panel(symbol=symbol, t=p.t, o=p.o, h=p.h, l=p.l, c=p.c, v=p.v,
                 qv=p.qv, day=day, atr5=atr(p.h, p.l, p.c, ATR_LEN))


# ============================================================== 15m structure
def resample(panel: Panel, ms: int) -> tuple[np.ndarray, ...]:
    """Fixed-width UTC buckets. Returns (bucket_start, o, h, l, c)."""
    bucket = panel.t // ms
    uniq, idx = np.unique(bucket, return_index=True)
    starts = uniq * ms
    o = panel.o[idx]
    h = np.maximum.reduceat(panel.h, idx)
    l = np.minimum.reduceat(panel.l, idx)
    c = panel.c[np.append(idx[1:] - 1, len(panel.t) - 1)]
    return starts, o, h, l, c


def zigzag(h: np.ndarray, l: np.ndarray, p: int,
           ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Alternating fractal pivots (the structure sequence).

    Symmetric p-bar fractals, then enforced alternation: between two consecutive
    highs keep the higher, between two lows keep the lower. Returns the pivot
    indices and prices for highs and lows separately, in time order.
    """
    mh = maximum_filter1d(h, size=2 * p + 1, mode="nearest")
    ml = minimum_filter1d(l, size=2 * p + 1, mode="nearest")
    ar = np.arange(len(h))
    sh = ar[(h == mh) & (ar >= p) & (ar < len(h) - p)]
    sl = ar[(l == ml) & (ar >= p) & (ar < len(l) - p)]

    piv = sorted([(int(j), float(h[j]), 1) for j in sh] +
                 [(int(j), float(l[j]), -1) for j in sl])
    seq: list[list[float]] = []
    for idx, px, typ in piv:
        if not seq:
            seq.append([idx, px, typ])
            continue
        if typ == seq[-1][2]:                 # same type: keep the extreme
            if (typ == 1 and px > seq[-1][1]) or (typ == -1 and px < seq[-1][1]):
                seq[-1] = [idx, px, typ]
        else:
            seq.append([idx, px, typ])
    hb = np.array([s[0] for s in seq if s[2] == 1], dtype=np.int64)
    hp = np.array([s[1] for s in seq if s[2] == 1], dtype=float)
    lb = np.array([s[0] for s in seq if s[2] == -1], dtype=np.int64)
    lp = np.array([s[1] for s in seq if s[2] == -1], dtype=float)
    return hb, hp, lb, lp


def _activation_bars(conf: np.ndarray, px: np.ndarray, c: np.ndarray,
                     k: np.ndarray, n: int, direction: int) -> np.ndarray:
    """For each pivot, the first bar m >= confirm whose CLOSE is at least
    IMPULSE_ATR_K * ATR beyond the pivot (above it for a high, below it for a
    low); np.inf if it never is. The break must be a decisive close beyond the
    swing, which is the impulsive-move definition."""
    if direction == 1:
        g = c - k
        run = np.maximum.accumulate(g)
        first = np.searchsorted(run, px, side="right")     # first m with g[m] > px
    else:
        g = -(c + k)
        run = np.maximum.accumulate(g)
        first = np.searchsorted(run, -px, side="right")    # first g > -px
    aj = np.maximum(conf, first)
    return np.where(aj >= n, np.inf, aj)


def _level_series(n: int, act_bar: np.ndarray, act_px: np.ndarray,
                  c: np.ndarray, k: np.ndarray, direction: int) -> np.ndarray:
    """Step function: the price of the most recently broken swing, held until a
    close invalidates it (a close back below `level - k*ATR` for a broken high;
    above `level + k*ATR` for a broken low). The invalidation is a genuine
    failure of the level, and it is the opposite test from the break that
    activated it, so the two cannot coincide on a monotone tape."""
    order = np.argsort(act_bar, kind="stable")
    act_bar = act_bar[order]
    act_px = act_px[order]
    ok = np.isfinite(act_bar)
    act_bar = act_bar[ok].astype(np.int64)
    act_px = act_px[ok]
    out = np.full(n, np.nan)
    m = len(act_bar)
    for j in range(m):
        a = act_bar[j]
        end = act_bar[j + 1] if j + 1 < m else n
        if a >= end:
            continue                       # same-bar activation: later pivot wins
        if direction == 1:
            fail = c[a:end] < act_px[j] - k[a:end]
        else:
            fail = c[a:end] > act_px[j] + k[a:end]
        if fail.any():
            cut = a + int(np.argmax(fail))
            out[a:cut] = act_px[j]
        else:
            out[a:end] = act_px[j]
    return out


def build_htf(panel: Panel, *, swing_p: int, impulse_k: float) -> Htf:
    """Resample to 15m and build the structure layers: alternating fractals,
    the last broken MAJOR swing level (high and low), and the HH/HL / LH/LL /
    consolidation trend state, all read on closed bars only."""
    bs, o, h, l, c = resample(panel, HTF_MS)
    n = len(c)
    a = atr(h, l, c, ATR_LEN)
    hb, hp, lb, lp = zigzag(h, l, swing_p)

    # confirm lag: a pivot is usable only p bars later
    conf_h = hb + swing_p
    conf_l = lb + swing_p

    kband = impulse_k * a
    act_h = _activation_bars(conf_h, hp, c, kband, n, direction=1)
    act_l = _activation_bars(conf_l, lp, c, kband, n, direction=-1)
    level_hi = _level_series(n, act_h, hp, c, kband, direction=1)
    level_lo = _level_series(n, act_l, lp, c, kband, direction=-1)

    # trend from the last two usable zigzag highs and the last two usable lows
    def _last_two(conf: np.ndarray, px: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        pos = np.searchsorted(conf, np.arange(n), side="right") - 1
        p1 = np.where(pos >= 0, px[np.clip(pos, 0, None)], np.nan)
        p2 = np.where(pos >= 1, px[np.clip(pos - 1, 0, None)], np.nan)
        return p1, p2

    hh1, hh2 = _last_two(conf_h, hp)
    ll1, ll2 = _last_two(conf_l, lp)
    trend = np.full(n, "cons", dtype=object)
    with np.errstate(invalid="ignore"):
        up = np.isfinite(hh1) & np.isfinite(hh2) & np.isfinite(ll1) & np.isfinite(ll2) \
            & (hh1 > hh2) & (ll1 > ll2)
        dn = np.isfinite(hh1) & np.isfinite(hh2) & np.isfinite(ll1) & np.isfinite(ll2) \
            & (hh1 < hh2) & (ll1 < ll2)
    trend[up] = "up"
    trend[dn] = "down"
    return Htf(bucket_start=bs, o=o, h=h, l=l, c=c, atr=a, trend=trend,
               level_hi=level_hi, level_lo=level_lo,
               zz_h_bar=hb, zz_h_px=hp, zz_l_bar=lb, zz_l_px=lp)


def map_htf(panel: Panel, htf: Htf) -> np.ndarray:
    """Index of the last FULLY CLOSED 15m bar at each 5m bar (no look-ahead)."""
    ends = htf.bucket_start + HTF_MS
    return np.searchsorted(ends, panel.t, side="right") - 1


# ============================================================== touches
@dataclass
class Touch:
    symbol: str
    day: int
    bar: int
    direction: int            # +1 long (broken-high zone), -1 short
    align: int                # 1 = with the HTF trend, 0 = counter
    confirmed: int            # 1 = double-top/bottom + confirming candle
    level: float
    entry: float
    stop: float
    target: float
    net_R: float
    gross_R: float
    R_abs: float
    rr: float                 # realized target/risk geometry, not a setting
    exit_reason: str
    bp_fwd: float             # geometry-free fixed-horizon forward return, net
    has_trade: bool


def simulate(p: Panel, entry_idx: int, direction: int, stop: float,
             target: float, max_hold: int, cost_bp: float,
             ) -> tuple[str, float, float]:
    """Walk bars from entry. PESSIMISTIC (ifvg convention): if one bar touches
    both stop and target, the STOP is taken. Gaps are not improved. After
    max_hold bars the position is flat at that bar's close."""
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
        if hit_stop:
            exit_px, reason = stop, "stop"
            break
        if hit_tgt:
            exit_px, reason = target, "target"
            break
        exit_px, reason = float(p.c[k]), "time"
    gross = (exit_px - entry) * direction
    net = gross - entry * cost_bp / 1e4
    return (reason, (net / R if R > 0 else 0.0), (gross / R if R > 0 else 0.0))


def confirm_pattern(p: Panel, t: int, direction: int, atr5: float, *,
                    win: int, tol_atr: float, conf_bars: int,
                    ) -> tuple[bool, int, float]:
    """A double top (short) / double bottom (long) at the zone, then a
    confirming red/green candle.

    Peaks are local extrema of the high (short) / low (long). Two of them, in
    order, must sit within tol_atr * ATR(14) of each other; for a short no bar
    may CLOSE above the higher peak (that is continuation through, not
    rejection). The confirming candle is the first red (short) / green (long)
    close after the second peak, within conf_bars. Returns
    (confirmed, entry_idx, rejection_extreme)."""
    n = len(p.t)
    k_lo, k_hi = t + 1, min(t + win, n - 2)      # peaks need a right neighbour
    if k_hi - k_lo + 1 < 3:
        return (False, -1, float("nan"))
    if direction == -1:
        peaks = [k for k in range(k_lo, k_hi + 1)
                 if p.h[k] >= p.h[k - 1] and p.h[k] >= p.h[k + 1]]
        if len(peaks) < 2:
            return (False, -1, float("nan"))
        peaks.sort(key=lambda k: p.h[k], reverse=True)
        p1, p2 = sorted(peaks[:2])
        if abs(p.h[p1] - p.h[p2]) > tol_atr * atr5:
            return (False, -1, float("nan"))
        top = max(p.h[p1], p.h[p2])
        if np.any(p.c[t:p2 + 1] > top):
            return (False, -1, float("nan"))
        for k in range(p2 + 1, min(n - 1, p2 + 1 + conf_bars)):
            if p.c[k] < p.o[k]:
                return (True, k + 1, top)
        return (False, -1, float("nan"))
    troughs = [k for k in range(k_lo, k_hi + 1)
               if p.l[k] <= p.l[k - 1] and p.l[k] <= p.l[k + 1]]
    if len(troughs) < 2:
        return (False, -1, float("nan"))
    troughs.sort(key=lambda k: p.l[k])
    t1, t2 = sorted(troughs[:2])
    if abs(p.l[t1] - p.l[t2]) > tol_atr * atr5:
        return (False, -1, float("nan"))
    bot = min(p.l[t1], p.l[t2])
    if np.any(p.c[t:t2 + 1] < bot):
        return (False, -1, float("nan"))
    for k in range(t2 + 1, min(n - 1, t2 + 1 + conf_bars)):
        if p.c[k] > p.o[k]:
            return (True, k + 1, bot)
    return (False, -1, float("nan"))


def _target_price(htf: Htf, k_htf: int, entry: float, direction: int,
                  swing_p: int) -> float:
    """The prior structure extreme: the most recent zigzag swing high (long) /
    low (short) that lies beyond the entry. A pivot is only used once it is
    CONFIRMED (bar <= k_htf - swing_p), so the target never peeks at a swing
    that had not been established at the entry bar."""
    last_conf = k_htf - swing_p
    if direction == 1:
        bars, px = htf.zz_h_bar, htf.zz_h_px
        m = (bars <= last_conf) & (px > entry)
    else:
        bars, px = htf.zz_l_bar, htf.zz_l_px
        m = (bars <= last_conf) & (px < entry)
    if not m.any():
        return float("nan")
    idx = np.flatnonzero(m)
    return float(px[idx[-1]])


def build_touches(p: Panel, htf: Htf, *, swing_p: int, zone_half: float,
                  double_win: int, double_tol: float, stop_k: float,
                  max_hold: int, conf_bars: int, h_bp: int, cost_bp: float,
                  retrace_min: float, retrace_lookback: int,
                  min_r_bp: float = 0.0, stats: dict | None = None) -> list[Touch]:
    """Build the fixed touch population and one trade per touch.

    Zone building (here), pattern confirmation (`confirm_pattern`) and
    simulation (`simulate`) are separate so the differential logic is
    auditable. A touch is a 5m bar whose [low,high] first intersects the zone of
    the last broken major swing level, entering from the correct side (from
    above for a long, from below for a short). Only touches in an aligned
    trend state are kept (the source stands down in consolidation)."""
    n = len(p.t)
    k = map_htf(p, htf)
    kk = np.clip(k, 0, None)
    valid = k >= 0

    atr15 = htf.atr[kk]
    band = zone_half * atr15
    hi_lvl = htf.level_hi[kk]
    lo_lvl = htf.level_lo[kk]
    trend = htf.trend[kk]

    with np.errstate(invalid="ignore"):
        z_hi_lo, z_hi_hi = hi_lvl - band, hi_lvl + band      # long zone
        z_lo_lo, z_lo_hi = lo_lvl - band, lo_lvl + band      # short zone
    l_prev = np.concatenate([[np.inf], p.l[:-1]])
    h_prev = np.concatenate([[-np.inf], p.h[:-1]])

    inside_long = valid & np.isfinite(hi_lvl) & (p.l <= z_hi_hi) & (p.h >= z_hi_lo)
    inside_short = valid & np.isfinite(lo_lvl) & (p.l <= z_lo_hi) & (p.h >= z_lo_lo)
    touch_long = inside_long & (l_prev > z_hi_hi)      # returned from above
    touch_short = inside_short & (h_prev < z_lo_lo)    # returned from below
    counts = {"touch_long": int(touch_long.sum()),
              "touch_short": int(touch_short.sum()),
              "level_hi_bars": int((valid & np.isfinite(hi_lvl)).sum()),
              "level_lo_bars": int((valid & np.isfinite(lo_lvl)).sum())}

    touches: list[Touch] = []
    for i in np.flatnonzero(touch_long | touch_short):
        if i < WARMUP or i >= n - 2:
            continue
        kt = int(kk[i])
        if kt < 0 or not np.isfinite(atr15[i]) or atr15[i] <= 0:
            continue
        direction = 1 if touch_long[i] else -1
        lvl = float(hi_lvl[i]) if direction == 1 else float(lo_lvl[i])
        tr = trend[i]
        if tr == "up":
            align = 1 if direction == 1 else 0
        elif tr == "down":
            align = 1 if direction == -1 else 0
        else:
            continue                        # consolidation -> stand down

        # his "at least two candles and some movement" pullback filter
        a0 = max(0, i - retrace_lookback)
        if direction == 1:
            j = int(a0 + np.argmax(p.h[a0:i + 1]))
            if (i - j) < 2 or (p.h[j] - p.l[i]) < retrace_min * atr15[i]:
                continue
        else:
            j = int(a0 + np.argmin(p.l[a0:i + 1]))
            if (i - j) < 2 or (p.h[i] - p.l[j]) < retrace_min * atr15[i]:
                continue

        confirmed, entry_idx, rej = confirm_pattern(
            p, i, direction, float(p.atr5[i]), win=double_win,
            tol_atr=double_tol, conf_bars=conf_bars)

        buf = stop_k * float(p.atr5[i])
        if confirmed and 0 < entry_idx < n - 2:
            entry_bar = entry_idx
            stop = (rej + buf) if direction == -1 else (rej - buf)
        else:
            confirmed = 0
            entry_bar = i + 1
            stop = (z_hi_hi[i] + buf) if direction == -1 else (z_hi_lo[i] - buf)
        if entry_bar >= n - 2 or not np.isfinite(stop):
            continue
        entry = float(p.o[entry_bar])
        if (direction == 1 and stop >= entry) or (direction == -1 and stop <= entry):
            continue
        kt_e = int(kk[entry_bar])
        target = _target_price(htf, kt_e, entry, direction, swing_p)
        R = abs(entry - stop)
        has_trade = bool(np.isfinite(target) and R > 0
                         and (target - entry) * direction > 0)
        if has_trade and min_r_bp > 0 and R / entry * 1e4 < min_r_bp:
            has_trade = False               # degenerate R: cost-in-R explodes
        if has_trade:
            reason, net_R, gross_R = simulate(p, entry_bar, direction, stop,
                                              target, max_hold, cost_bp)
            rr = abs(target - entry) / R
        else:
            reason, net_R, gross_R, rr = ("no_target", 0.0, 0.0, float("nan"))
        fwd_idx = entry_bar + h_bp
        bp = (direction * (p.c[fwd_idx] - entry) / entry * 1e4 - cost_bp) \
            if fwd_idx < n else float("nan")
        touches.append(Touch(
            symbol=p.symbol, day=int(p.day[i]), bar=int(i), direction=direction,
            align=align, confirmed=confirmed, level=lvl, entry=entry, stop=stop,
            target=(float(target) if np.isfinite(target) else float("nan")),
            net_R=net_R, gross_R=gross_R, R_abs=R, rr=rr, exit_reason=reason,
            bp_fwd=bp, has_trade=has_trade))
    if stats is not None:
        stats.update(counts)
    return touches


# ============================================================== statistics
def cluster_t(x: np.ndarray, g: np.ndarray, day: np.ndarray,
              ) -> tuple[float, float, int]:
    """Difference in means (g==1 minus g==0) with a day-clustered t.

    Cluster-robust variance for a two-sample difference: each observation's
    influence on the estimator is summed inside its UTC day and the variance is
    the sum of squared cluster sums. Touches inside a day are not independent,
    so the unit is one day, per the matrix protocol."""
    x = np.asarray(x, dtype=float)
    g = np.asarray(g, dtype=float)
    keep = np.isfinite(x)
    x, g, day = x[keep], g[keep], np.asarray(day)[keep]
    x1, x0 = x[g == 1], x[g == 0]
    if len(x1) < 2 or len(x0) < 2:
        return (float("nan"), float("nan"), 0)
    m1, m0 = x1.mean(), x0.mean()
    beta = m1 - m0
    infl = np.where(g == 1, (x - m1) / len(x1), -(x - m0) / len(x0))
    clusters = np.unique(day)
    sums = np.array([infl[day == c].sum() for c in clusters])
    G = len(clusters)
    if G < 2:
        return (beta, float("nan"), G)
    var = (G / (G - 1)) * float((sums ** 2).sum())
    se = float(np.sqrt(var))
    return (beta, (beta / se if se > 0 else float("nan")), G)


def describe(x: np.ndarray) -> dict:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return {"n": 0}
    q = np.percentile(x, [10, 25, 50, 75, 90])
    return {"n": len(x), "mean": float(x.mean()), "median": float(np.median(x)),
            "sd": float(x.std(ddof=1)) if len(x) > 1 else 0.0,
            "p10": q[0], "p25": q[1], "p50": q[2], "p75": q[3], "p90": q[4],
            "win": float((x > 0).mean())}


def differential(touches: list[Touch], label: str) -> dict:
    """Split the fixed touch set by `label` ('align' or 'confirmed') and compare
    realized net R, hit rate, gross R and the geometry-free bp measure."""
    if not touches:
        return {"label": label, "n": 0}
    tr = [t for t in touches if t.has_trade]
    x_r = np.array([t.net_R for t in tr])
    g_r = np.array([getattr(t, label) for t in tr])
    d_r = np.array([t.day for t in tr])
    x_g = np.array([t.gross_R for t in tr])
    x_w = np.array([1.0 if t.net_R > 0 else 0.0 for t in tr])
    x_b = np.array([t.bp_fwd for t in touches])
    g_b = np.array([getattr(t, label) for t in touches])
    d_b = np.array([t.day for t in touches])
    gap_r, t_r, G = cluster_t(x_r, g_r, d_r)
    gap_g, t_g, _ = cluster_t(x_g, g_r, d_r)
    gap_w, t_w, _ = cluster_t(x_w, g_r, d_r)
    gap_b, t_b, _ = cluster_t(x_b, g_b, d_b)
    return {"label": label, "n": len(touches), "n_trade": int(len(x_r)),
            "G": G, "n1": int(g_r.sum()), "n0": int(len(g_r) - g_r.sum()),
            "gap_r": gap_r, "t_r": t_r, "gap_gross": gap_g, "t_gross": t_g,
            "gap_win": gap_w, "t_win": t_w, "gap_bp": gap_b, "t_bp": t_b,
            "a1": describe(x_r[g_r == 1]), "a0": describe(x_r[g_r == 0]),
            "bp1": describe(x_b[g_b == 1]), "bp0": describe(x_b[g_b == 0])}


def show(d: dict, name: str, quiet: bool = False) -> None:
    if not d["n"]:
        print(f"  {name}: no touches")
        return
    if not quiet:
        print(f"  {name}: touches={d['n']}  with a simulated trade={d['n_trade']}  "
              f"split {d['n1']} / {d['n0']}  clustered days={d['G']}")
    print(f"    realized net R  : gap {d['gap_r']:+.3f}R  day-clustered t = {d['t_r']:+.2f}")
    print(f"    gross R         : gap {d['gap_gross']:+.3f}R  day-clustered t = {d['t_gross']:+.2f}")
    print(f"    hit rate        : gap {d['gap_win']*100:+.1f}pt day-clustered t = {d['t_win']:+.2f}")
    print(f"    fixed-horizon bp: gap {d['gap_bp']:+.2f}bp day-clustered t = {d['t_bp']:+.2f}")
    for key, lab in (("a1", "arm 1"), ("a0", "arm 0")):
        s = d[key]
        if s["n"]:
            print(f"      {lab:7s} n={s['n']:>5} netR mean={s['mean']:+.3f} "
                  f"med={s['median']:+.3f} win={s['win']*100:5.1f}%")
    for key, lab in (("bp1", "arm 1"), ("bp0", "arm 0")):
        s = d[key]
        if s["n"]:
            print(f"      {lab:7s} n={s['n']:>5} bp mean={s['mean']:+.2f} "
                  f"med={s['median']:+.2f}")


# ============================================================== main
_PANEL_CACHE: dict[str, Panel] = {}


def get_panel(symbol: str, skip_download: bool) -> Panel:
    if symbol not in _PANEL_CACHE:
        _PANEL_CACHE[symbol] = load_panel(symbol, skip_download=skip_download)
    return _PANEL_CACHE[symbol]


def build_all(symbols: list[str], skip_download: bool, **kw) -> tuple[list[Touch], dict]:
    touches: list[Touch] = []
    meta = {"bars": 0, "pairs": 0, "spans": {},
            "touch_long": 0, "touch_short": 0,
            "level_hi_bars": 0, "level_lo_bars": 0}
    for s in symbols:
        try:
            p = get_panel(s, skip_download)
        except (SystemExit, FileNotFoundError) as e:
            print(f"  {s}: {e}")
            continue
        htf = build_htf(p, swing_p=kw.get("swing_p", SWING_P),
                        impulse_k=kw.get("impulse_k", IMPULSE_ATR_K))
        st: dict = {}
        touches.extend(build_touches(
            p, htf, swing_p=kw.get("swing_p", SWING_P),
            zone_half=kw.get("zone_half", ZONE_HALF_ATR),
            double_win=kw.get("double_win", DOUBLE_WIN),
            double_tol=kw.get("double_tol", DOUBLE_TOL_ATR),
            stop_k=kw.get("stop_k", STOP_ATR_K),
            max_hold=kw.get("max_hold", MAX_HOLD),
            conf_bars=kw.get("conf_bars", CONF_BARS),
            h_bp=kw.get("h_bp", H_BP), cost_bp=COST_BP,
            retrace_min=kw.get("retrace_min", RETRACE_MIN_ATR),
            retrace_lookback=kw.get("retrace_lookback", RETRACE_LOOKBACK),
            min_r_bp=kw.get("min_r_bp", MIN_R_BP), stats=st))
        for key in ("touch_long", "touch_short", "level_hi_bars", "level_lo_bars"):
            meta[key] += st.get(key, 0)
        meta["bars"] += len(p.t)
        meta["pairs"] += 1
        meta["spans"][s] = (int(p.t[0]), int(p.t[-1]), len(p.t))
    return touches, meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", nargs="*", default=None,
                    help="symbols (default: the frozen Q9 30-pair universe)")
    ap.add_argument("--grid", action="store_true",
                    help="also print the full sensitivity grid")
    ap.add_argument("--skip-download", action="store_true",
                    help="use only cached panels")
    args = ap.parse_args()

    symbols = args.pairs or bvb.Q9_UNIVERSE

    print("=" * 84)
    print("Q10  The Trading Channel trend-continuation -- TWO differential tests")
    print("=" * 84)
    print(f"locked : swing fractal p={SWING_P} on 15m; MAJOR when a close goes >= "
          f"{IMPULSE_ATR_K}xATR beyond it; zone = level +/- {ZONE_HALF_ATR}xATR15")
    print(f"         double window {DOUBLE_WIN} bars, tol {DOUBLE_TOL_ATR}xATR; stop = "
          f"rejection +/- {STOP_ATR_K}xATR5; target = prior structure extreme; "
          f"time stop {MAX_HOLD} bars")
    print(f"cost   : {COST_BP:.0f} bp round trip on notional (matrix taker row); "
          f"pessimistic intrabar")
    print(f"universe: {len(symbols)} pairs; HTF 15m resampled from 5m")

    touches, meta = build_all(symbols, args.skip_download)
    if not touches:
        raise SystemExit("no touches")

    print()
    print("-" * 84)
    print(f"DATA  pairs used={meta['pairs']}  total 5m bars={meta['bars']:,}")
    t0 = min(v[0] for v in meta["spans"].values())
    t1 = max(v[1] for v in meta["spans"].values())
    print(f"span  {dt.datetime.fromtimestamp(t0/1000, dt.timezone.utc):%Y-%m-%d} -> "
          f"{dt.datetime.fromtimestamp(t1/1000, dt.timezone.utc):%Y-%m-%d}")

    n_align1 = sum(1 for t in touches if t.align == 1)
    n_conf = sum(1 for t in touches if t.confirmed == 1)
    n_long = sum(1 for t in touches if t.direction == 1)
    n_trade = sum(1 for t in touches if t.has_trade)
    print()
    print("FUNNEL")
    print(f"  raw zone touches (first bar of each excursion, before filters):")
    print(f"    long {meta['touch_long']:,} / short {meta['touch_short']:,}   "
          f"(a level is live on hi {meta['level_hi_bars']:,} / "
          f"lo {meta['level_lo_bars']:,} 5m bars)")
    print(f"  qualifying zone touches                : {len(touches):,}")
    print(f"    long (broken-high zone) / short      : {n_long:,} / {len(touches)-n_long:,}")
    print(f"    with-trend / counter-trend (Claim A) : {n_align1:,} / {len(touches)-n_align1:,}")
    print(f"    confirmed / unconfirmed   (Claim B)  : {n_conf:,} / {len(touches)-n_conf:,}")
    print(f"    with a simulated trade (target found): {n_trade:,}")

    days = np.array([t.day for t in touches])
    d_lo, d_hi = int(days.min()), int(days.max())
    cut = d_lo + SCATTER_FRAC * (d_hi - d_lo)
    sc = [t for t in touches if t.day <= cut]
    ho = [t for t in touches if t.day > cut]
    cut_date = dt.date.fromordinal(int(cut))

    print()
    print("=" * 84)
    print(f"CLAIM A -- trend alignment (with-trend minus counter-trend), "
          f"first 70% through {cut_date}")
    print("=" * 84)
    dA = differential(sc, "align")
    show(dA, "scatter 70%")
    passA = bool(np.isfinite(dA["t_r"]) and dA["t_r"] >= 2.0 and dA["gap_r"] > 0)
    print("  KILL RULE (Claim A): requires day-clustered t >= 2 AND gap > 0 on the 70%.")
    print(f"  verdict: {'CLEARS' if passA else 'FAILS -> Claim A holdout stays SEALED'}"
          f"  ({len(sc)} touches, rule-3 floor {MIN_EVENTS})")

    # confounding check: Claim A inside each confirmed/unconfirmed subset
    print("\n  Claim A inside each Claim-B subset (so the two labels cannot confound):")
    for cflag, nm in ((1, "confirmed  "), (0, "unconfirmed")):
        sub = [t for t in sc if t.confirmed == cflag]
        dd = differential(sub, "align")
        if dd["n_trade"]:
            print(f"    {nm}: n={dd['n']:>6} nT={dd['n_trade']:>6} "
                  f"gapR={dd['gap_r']:>+7.3f} t={dd['t_r']:>+6.2f}  "
                  f"gapbp={dd['gap_bp']:>+7.2f} t={dd['t_bp']:>+6.2f}")

    print()
    print("=" * 84)
    print(f"CLAIM B -- rejection pattern (confirmed minus unconfirmed), "
          f"first 70% through {cut_date}")
    print("=" * 84)
    dB = differential(sc, "confirmed")
    show(dB, "scatter 70%")
    passB = bool(np.isfinite(dB["t_r"]) and dB["t_r"] >= 2.0 and dB["gap_r"] > 0)
    print("  KILL RULE (Claim B): requires day-clustered t >= 2 AND gap > 0 on the 70%.")
    print(f"  verdict: {'CLEARS' if passB else 'FAILS -> Claim B holdout stays SEALED'}")

    print()
    print("=" * 84)
    print("WALK-FORWARD -- opened PER CLAIM, only if that claim's 70% cleared")
    print("=" * 84)
    for name, ok, lab in (("Claim A", passA, "align"), ("Claim B", passB, "confirmed")):
        if ok:
            print(f"\n  {name}: last 30%, opened once")
            dd = differential(ho, lab)
            show(dd, f"{name} holdout 30%")
            print(f"    same sign as scatter: {dd['gap_r'] > 0}")
        else:
            print(f"\n  {name}: NOT OPENED. The first 70% failed its kill rule, so the")
            print("  last 30% stays sealed (matrix rule 5). No holdout statistic")
            print("  is computed or printed.")

    print()
    print("=" * 84)
    print("THE SOURCE'S CLAIM -- ~50% win rate at 2:1, checked on the first 70%")
    print("=" * 84)
    for cflag, nm in ((1, "CONFIRMED (his entry)"), (0, "UNCONFIRMED (control)")):
        sub = [t for t in sc if t.confirmed == cflag and t.has_trade]
        if not sub:
            print(f"  {nm}: no trades")
            continue
        netR = np.array([t.net_R for t in sub])
        rr = np.array([t.rr for t in sub])
        wins = 100.0 * (netR > 0).mean()
        wl = netR[netR > 0].mean() if (netR > 0).any() else float("nan")
        ll = netR[netR <= 0].mean() if (netR <= 0).any() else float("nan")
        print(f"  {nm}: n={len(sub)}  win={wins:.1f}%  mean netR={netR.mean():+.3f}  "
              f"median R(bp)={np.median([t.R_abs/t.entry*1e4 for t in sub]):.1f}")
        print(f"    realized R:R (target/risk) median={np.median(rr):.2f} "
              f"mean={rr.mean():.2f}  (his claim: 2.00)")
        print(f"    realized avg win / avg loss in R = {wl:+.3f} / {ll:+.3f}")

    allrr = np.array([t.rr for t in touches if t.has_trade and np.isfinite(t.rr)])
    if len(allrr):
        q = np.percentile(allrr, [10, 25, 50, 75, 90])
        print(f"\n  R:R distribution, ALL trades (whole span): n={len(allrr)} "
              f"median={np.median(allrr):.2f} "
              f"p10/25/50/75/90={q[0]:.2f}/{q[1]:.2f}/{q[2]:.2f}/{q[3]:.2f}/{q[4]:.2f}")

    print()
    print("CONTEXT -- exit mix on the first 70%")
    for cflag, nm in ((1, "confirmed"), (0, "unconfirmed")):
        sub = [t for t in sc if t.confirmed == cflag and t.has_trade]
        reasons: dict[str, int] = {}
        for t in sub:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        print(f"  {nm:11s} n={len(sub):>5}  exits {dict(sorted(reasons.items()))}")

    if args.grid:
        print()
        print("=" * 84)
        print("SENSITIVITY GRID -- each cell re-runs the WHOLE test on the 70%.")
        print("Reported for transparency; NOT a search for the best cell.")
        print("=" * 84)
        grid = [
            ("swing fractal p", "swing_p", [2, 3, 4]),
            ("impulse k (x ATR)", "impulse_k", [0.25, 0.5, 1.0]),
            ("zone half-width (x ATR)", "zone_half", [0.25, 0.5, 1.0]),
            ("double window (bars)", "double_win", [6, 12, 24]),
            ("double tol (x ATR)", "double_tol", [0.25, 0.5, 1.0]),
            ("stop buffer (x ATR)", "stop_k", [0.5, 1.0, 1.5]),
            ("time stop (bars)", "max_hold", [48, 96, 192]),
            ("bp horizon (bars)", "h_bp", [12, 24, 96]),
            ("retrace min (x ATR)", "retrace_min", [0.25, 0.5, 1.0]),
            ("confirm window (bars)", "conf_bars", [3, 6, 12]),
            ("min-R filter (bp)", "min_r_bp", [0.0, 20.0, 50.0]),
        ]
        locked = {"swing_p": SWING_P, "impulse_k": IMPULSE_ATR_K,
                  "zone_half": ZONE_HALF_ATR, "double_win": DOUBLE_WIN,
                  "double_tol": DOUBLE_TOL_ATR, "stop_k": STOP_ATR_K,
                  "max_hold": MAX_HOLD, "h_bp": H_BP,
                  "retrace_min": RETRACE_MIN_ATR, "conf_bars": CONF_BARS,
                  "min_r_bp": MIN_R_BP}
        for name, key, vals in grid:
            print(f"\n  -- {name} --")
            for v in vals:
                tt, _ = build_all(symbols, args.skip_download, **{key: v})
                sub = [t for t in tt if t.day <= cut]
                star = "*" if v == locked[key] else " "
                for lab, nm in (("align", "A"), ("confirmed", "B")):
                    dd = differential(sub, lab)
                    if not dd["n_trade"]:
                        print(f"    {str(v):>8s}{star} {nm}: no trades")
                        continue
                    print(f"    {str(v):>8s}{star} {nm}: n={dd['n']:>6} "
                          f"nT={dd['n_trade']:>6} gapR={dd['gap_r']:>+7.3f} "
                          f"t={dd['t_r']:>+6.2f} dWin={dd['gap_win']*100:>+6.1f}pt")
        print("\n  (*) = the locked value. Each cell is the WHOLE test, not a slice.")

    print()
    print("Nothing here is a live order, a sizing change, or a claim the source")
    print("makes money. The differentials test the trend filter and the rejection")
    print("pattern on one frozen crypto universe over one frozen span.")


if __name__ == "__main__":
    main()
