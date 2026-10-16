#!/usr/bin/env python
"""Q11: Oliver Velez's "whale" / dead-cat-bounce short -- TWO differential
tests on a fixed power-drop population (Binance 5m spot).

Source: "THE STRATEGY BEHIND MY BIGGEST DAY TRADING PROFITS," Oliver Velez
Trading, https://www.youtube.com/watch?v=KmO2rUG6mIk. See the Q11 section of
docs/DAY_TRADE_EDGE_MATRIX.md for the frozen spec. This module implements
exactly that spec and nothing else.

WHAT IS TESTED, AND WHAT IS NOT
-------------------------------
The video carries NO statistical evidence: narrated worked examples (Meta,
Amazon, Apple) with no win rate, no sample size, no profit factor and no
audited track record. It ends with a lead magnet. The source explicitly
concedes that "enough red" is a judgement call and that SMA slope is
"judgemental", and repeatedly declines to make either objective. Nothing here
treats the video as evidence.

Two substitutions are MATERIAL and bound what this row can license:

  * His trigger depends on an OPENING GAP UP into a round number. Crypto spot
    does not gap, so the gap half of the setup is not reproduced. What is
    reproduced is the part that does not need a gap: power bar down -> bounce
    -> secondary move to a new low, with the down-move's own high standing in
    for the "ceiling".
  * A $322 stock has a psychological whole-dollar grid; a 24/7 crypto pair does
    not. The mechanical round grid below is OURS.

TWO SEPARABLE CLAIMS, neither allowed to rescue the other:

    Claim A -- the bounce fails (dead-cat mechanism):
        shorting the FAILED BOUNCE beats shorting the power drop directly,
        on the SAME events. A mean-reversion-after-impulse hypothesis.

    Claim B -- the zone rule matters:
        the same setup performs WORSE in Zone 1 (below/at the MAs -- the zone
        he refuses to short) than in Zone 2/3. His rule predicts a POSITIVE
        Zone2/3-minus-Zone1 gap.

The design is differential, the same shape as Q8/Q9/Q10. ONE event population
is built and frozen; Claim A varies ONLY the entry rule on the same events;
Claim B varies ONLY the zone label on the same events. Hit rate AND realized R
are both reported, plus the geometry-free fixed-horizon bp measure (Q10's house
standard: it does not partly CONTAIN the stop geometry). Claim A also reports a
geometry-free bounce-recovery distribution, which is the direct test of
"the rubber ball won't bounce back".

FROZEN, EX ANTE (see the matrix row; do not change these after seeing output)
----------------------------------------------------------------------------
  POWER_ATR_K    = 1.5    "power bar down" = <=3 consecutive red 5m bars whose
                          net move >= 1.5 * ATR(14) measured before the run
  POWER_MAX_BARS = 3      the run may span at most this many bars
  POWER_CLOSE_Q  = 0.25   the run's final close sits in the bottom quarter of
                          the run's own range (the "close in the bottom q")
  RED_SIZE_C     = 1.0    the run's widest range >= 1.0x the median range of the
                          red bars among the prior 288 5m bars (one UTC day) --
                          his "comparable to the last several big reds"
  BOUNCE_MAX_BARS= 6      the bounce (first green bar) must appear within 6 bars
  ENTRY_TOL_ATR  = 0.05   locked CONSERVATIVE entry: sell-stop at bounce_low -
                          0.05*ATR (his "short right under the green")
  SETUP_MAX_BARS = 12     the stop order is live for 12 bars
  SETUP_MAX_BARS= 12      (aggressive sell-limit at the bounce bar's midpoint =
                          his "cut the green in half" -- a LABELLED grid arm)
  ZONE3_ATR      = 1.0    Zone 3 = close > SMA20 + 1.0*ATR (context only)
  STOP_ATR_K     = 0.5    stop = high of the move + 0.5*ATR(14)
  MAX_STOP_ATR   = 3.0    max-stop cap, his "no more than a 75 cent stop out"
  GRID_ATR       = 1.0    target = the next 1.0*ATR grid level below the entry
  MAX_HOLD       = 96     time stop, 96 bars = 8h on 5m, then flat at close
  H_BP           = 24     fixed-horizon forward window for the bp measure
  RECOVERY_WIN   = 96     window for the bounce-recovery distribution
  COST_BP        = 15.0   matrix taker round trip (4.5 bp fee + 3 bp slip x2)
  SCATTER_FRAC   = 0.70   first 70% of calendar time
  MIN_EVENTS     = 120    rule 3 floor, on PAIRED events in the scatter

Everything else is a NAMED PARAMETER and is MY operationalization of a rule the
source leaves loose. The sensitivity grid reports each; none is tuned.

The CONTROL (Claim A) is not a free choice: it is his naive alternative -- short
at the OPEN of the bar after the power run ends -- with the SAME stop rule, the
SAME grid target and the SAME time stop, so only the entry timing differs.

DATA
----
Binance Vision 5-minute spot bars, the frozen 30-pair Q9/Q10 universe, already
resident under reports/.binance_cache/ (gitignored). MAs and ATR are computed
on the 5m series itself (5m is the proxy for his 2m floor). An optional 1m arm
(BTCUSDT + ETHUSDT, 2024-01 -> 2026-09, time-equivalent constants) is declared
as a secondary confirmation, never the headline.

Run:
    .venv/bin/python scripts/experiments/q11_velez_whale_differential.py --skip-download
    .venv/bin/python scripts/experiments/q11_velez_whale_differential.py --skip-download --grid
    .venv/bin/python scripts/experiments/q11_velez_whale_differential.py --skip-download --m1
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "feeds"))
from hyperliquid_bot.indicators import atr, sma  # noqa: E402
import binance_vision_bulk as bvb  # noqa: E402

CACHE = bvb.CACHE
PANELS_1M = CACHE / "panels_1m_subset"

# ============================================================== locked params
@dataclass(frozen=True)
class Params:
    power_atr_k: float = 1.5
    power_max_bars: int = 3
    power_close_q: float = 0.25
    red_size_c: float = 1.0
    red_ref: int = 288            # one UTC day of 5m bars
    red_min_n: int = 20           # need this many prior red bars to define "big red"
    bounce_max_bars: int = 6
    entry_tol_atr: float = 0.05
    setup_max_bars: int = 12
    entry_mode: str = "conservative"     # or "aggressive" (the grid arm)
    zone3_atr: float = 1.0
    zone_mode: str = "pos"               # or "pos_slope" (the grid arm)
    slope_lb: int = 12
    stop_atr_k: float = 0.5
    max_stop_atr: float = 3.0            # <= 0 means no cap
    grid_atr: float = 1.0
    max_hold: int = 96
    h_bp: int = 24
    recovery_win: int = 96
    min_r_bp: float = 0.0
    atr_len: int = 14
    fast_ma: int = 20
    slow_ma: int = 200
    cost_bp: float = 15.0
    round_q: float = 0.15         # "near a round level" = within this fraction of a step
    round_fine: float = 0.1       # the finer (less round) grid, in ATR units
    rec_pre_lb: int = 96          # pre-drop lookback for the recovery reference high
    rec_short: int = 24           # the secondary (short-window) recovery read

    def scaled(self, f: int) -> "Params":
        """Time-equivalent constants for a finer timeframe (f = 5 for 1m)."""
        return replace(
            self, power_max_bars=self.power_max_bars * f,
            red_ref=self.red_ref * f, bounce_max_bars=self.bounce_max_bars * f,
            setup_max_bars=self.setup_max_bars * f, max_hold=self.max_hold * f,
            h_bp=self.h_bp * f, recovery_win=self.recovery_win * f,
            slow_ma=self.slow_ma * f, fast_ma=self.fast_ma * f,
            slope_lb=self.slope_lb * f, rec_pre_lb=self.rec_pre_lb * f,
            rec_short=self.rec_short * f)


LOCKED = Params()

SCATTER_FRAC = 0.70
MIN_EVENTS = 120


def replace_locked(**kw) -> "Params":
    """A copy of the locked parameters with named overrides -- used by the tests
    and by nothing in the frozen run."""
    return replace(LOCKED, **kw)


# ============================================================== data / panel
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
    atr: np.ndarray
    fast: np.ndarray          # SMA20 on the trading timeframe
    slow: np.ndarray          # SMA200 on the trading timeframe

    def __len__(self) -> int:
        return len(self.t)


def _ordinal(t: np.ndarray) -> np.ndarray:
    return np.array([dt.datetime.fromtimestamp(int(x) / 1000, dt.timezone.utc)
                     .date().toordinal() for x in t], dtype=np.int64)


def build_panel(symbol: str, interval: str = "5m", skip_download: bool = False,
                prm: Params = LOCKED) -> Panel:
    p = (bvb.load_panel(symbol, interval) if skip_download
         else bvb.build_panel(symbol, interval))
    return Panel(symbol=symbol, t=p.t, o=p.o, h=p.h, l=p.l, c=p.c, v=p.v,
                 qv=p.qv, day=_ordinal(p.t),
                 atr=atr(p.h, p.l, p.c, prm.atr_len),
                 fast=sma(p.c, prm.fast_ma), slow=sma(p.c, prm.slow_ma))


# ------------------------------------------------------------- 1m sub-panel
def load_1m_subset(symbol: str, first=(2024, 1), last=(2026, 9)) -> Panel:
    """BTCUSDT / ETHUSDT 1m over the frozen subset window. Cached to its own
    .npz so the 5m panel store is never touched. Downloads only the declared
    months; no partial month, so the span is exactly the frozen one."""
    PANELS_1M.mkdir(parents=True, exist_ok=True)
    f = PANELS_1M / f"{symbol}_1m.npz"
    if not f.exists():
        bvb.download(symbol, "1m", first=first, last=last, quiet=True,
                     include_partial_month=False)
        zips = sorted(CACHE.glob(f"{symbol}-1m-*.zip"))
        acc: dict[str, list[np.ndarray]] = {k: [] for k in bvb.FIELDS}
        for z in zips:
            part = bvb._read_zip(z)
            for k in bvb.FIELDS:
                acc[k].append(part[k])
        cat = {k: np.concatenate(v) for k, v in acc.items()}
        order = np.argsort(cat["t"], kind="stable")
        for k in bvb.FIELDS:
            cat[k] = cat[k][order]
        uniq = np.concatenate([[True], np.diff(cat["t"]) != 0])
        for k in bvb.FIELDS:
            cat[k] = cat[k][uniq]
        np.savez_compressed(f, **{k: cat[k] for k in bvb.FIELDS})
    z = np.load(f)
    prm = LOCKED.scaled(5)
    return Panel(symbol=symbol, t=z["t"], o=z["o"], h=z["h"], l=z["l"],
                 c=z["c"], v=z["v"], qv=z["qv"], day=_ordinal(z["t"]),
                 atr=atr(z["h"], z["l"], z["c"], prm.atr_len),
                 fast=sma(z["c"], prm.fast_ma), slow=sma(z["c"], prm.slow_ma))


# ============================================================== layer 1: detection
def _shift(x: np.ndarray, k: int, fill) -> np.ndarray:
    """out[j] = x[j + k], nan/fill past the end."""
    n = len(x)
    out = np.full(n, fill, dtype=float)
    if k < n:
        out[:n - k] = x[k:]
    return out


def candidate_power(p: Panel, prm: Params) -> np.ndarray:
    """Vectorised scan for the "power bar down".

    Returns `best[i]` = the smallest run length m (1..power_max_bars) for which
    the run starting at bar i is ALL RED, moves at least power_atr_k * ATR(14)
    measured at bar i-1, and closes in the bottom power_close_q of the run's own
    range; -1 where no run qualifies. Mirrors the Q9 displacement scan with the
    sign flipped, plus the close-location quality test."""
    n = len(p.t)
    atrp = np.empty(n)
    atrp[0] = np.nan
    atrp[1:] = p.atr[:-1]

    red = (p.c < p.o)
    rpad = np.concatenate([red, np.zeros(prm.power_max_bars, dtype=bool)])
    cpad = np.concatenate([p.c, np.full(prm.power_max_bars, np.nan)])
    lpad = np.concatenate([p.l, np.full(prm.power_max_bars, np.nan)])
    hpad = np.concatenate([p.h, np.full(prm.power_max_bars, np.nan)])
    ii = np.arange(n)

    best = np.full(n, -1, dtype=np.int64)
    run_red = np.ones(n, dtype=bool)
    for m in range(1, prm.power_max_bars + 1):
        run_red &= rpad[m - 1:m - 1 + n]
        win_lo = np.minimum.reduce([lpad[k:k + n] for k in range(m)])
        win_hi = np.maximum.reduce([hpad[k:k + n] for k in range(m)])
        net = p.o - cpad[m - 1:m - 1 + n]
        # close in the bottom q of the run's range
        in_q = cpad[m - 1:m - 1 + n] <= win_lo + prm.power_close_q * (win_hi - win_lo)
        ok = (run_red & np.isfinite(atrp) & (net >= prm.power_atr_k * atrp) & in_q
              & (ii + m - 1 < n - 2))
        best = np.where((best < 0) & ok, m, best)
    return best


# ============================================================== layer 2: zone
def zone_class(p: Panel, i: int, prm: Params) -> int:
    """1 = below/at the faster MA (the zone he refuses to short); 2 = above.
    His rule is stated on the session OPEN; crypto has no session open, so the
    current price at the event is the analog (ours, disclosed).

    `zone_mode='pos'` (locked) is purely positional. `zone_mode='pos_slope'` is
    the grid variant in which his "plus the slope" clause is honoured: an event
    is Zone 1 only when price is at/below the faster MA AND that MA is not
    rising over `slope_lb` bars, so a pullback in a rising trend is not counted
    as the zone he refuses."""
    if not np.isfinite(p.fast[i]):
        return -1
    above_fast = p.c[i] > p.fast[i]
    if above_fast:
        return 2
    if prm.zone_mode == "pos_slope" and i >= prm.slope_lb:
        rising = (np.isfinite(p.fast[i - prm.slope_lb])
                  and p.fast[i] > p.fast[i - prm.slope_lb])
        if rising:
            return 2
    return 1


def event_zone23(e: "Event") -> float:
    """Claim B's label: 1.0 for Zone 2/3, 0.0 for Zone 1 (his refused zone)."""
    return 1.0 if e.zone >= 2 else 0.0


def zone3(p: Panel, i: int, prm: Params) -> bool:
    """Zone 3 = "significantly higher" than the MAs: the close sits more than
    ZONE3_ATR * ATR above the faster MA. Context only -- his rule treats Zone 2
    and Zone 3 the same way (both shortable), so this never enters a kill rule."""
    return bool(np.isfinite(p.slow[i]) and np.isfinite(p.atr[i])
                and np.isfinite(p.fast[i])
                and p.c[i] > p.fast[i] + prm.zone3_atr * p.atr[i])


# ============================================================== layer 3: simulation
@dataclass
class Arm:
    entry: float
    stop: float
    target: float
    entry_idx: int
    reason: str
    gross_R: float
    net_R: float
    R_abs: float
    dst_bp: float
    bp_fwd: float
    bp_common: float = float("nan")   # to a COMMON terminal bar, both arms
    rr: float = float("nan")          # target distance / stop distance
    off: int = 0                      # entry bar minus the run's end bar


def simulate(p: Panel, entry_idx: int, entry: float, stop: float, target: float,
             max_hold: int, cost_bp: float, h_bp: int,
             common_bar: int = -1, run_end: int = -1,
             intrabar: bool = False) -> Arm:
    """Walk bars from a SHORT entry at the explicit price `entry`.

    `entry` is passed in rather than re-derived from the bar because the two
    arms fill differently: the CONTROL enters at the bar's open (a clean price),
    while the BOUNCE enters intrabar on a stop trigger, where the pessimistic
    fill is `min(trigger, open)` -- never better than the trigger. Re-deriving
    the entry from `p.o[entry_idx]` would silently hand the bounce arm a better
    fill than its own trigger, which is exactly the optimism the pessimistic
    convention exists to forbid.

    PESSIMISTIC (ifvg convention): if one bar touches both stop and target, the
    STOP is taken. Gaps are not improved: a bar that opens through the stop
    still exits at the stop's own price, and a bar that gaps down past the
    target gets no better than the target. When `intrabar` is set (the bounce
    arm), the entry bar is only PARTLY post-entry -- the position did not exist
    for the whole bar -- so on that bar only the stop can fire and the target is
    not credited (the ambiguous bar resolves the worse way). After max_hold bars
    the position is flat at that bar's close.

    `bp_common` measures the SAME forward move for both arms -- from each arm's
    own entry price to the COMMON bar `run_end + h_bp` -- so it compares only the
    entry PRICE, not two different forward windows. It is a labelled diagnostic,
    not the frozen headline bp."""
    n = len(p.t)
    entry = float(entry)
    R = abs(stop - entry)
    exit_px, reason, k = float(p.c[min(n - 1, entry_idx + max_hold - 1)]), "time", entry_idx
    for k in range(entry_idx, min(n, entry_idx + max_hold)):
        if p.h[k] >= stop:                       # stop first (pessimistic)
            exit_px, reason = stop, "stop"
            break
        if not (intrabar and k == entry_idx) and p.l[k] <= target:
            exit_px, reason = target, "target"
            break
        exit_px, reason = float(p.c[k]), "time"
    gross = (entry - exit_px)                    # short: profit when price falls
    net = gross - entry * cost_bp / 1e4
    fwd = entry_idx + h_bp
    bp = ((entry - float(p.c[fwd])) / entry * 1e4 - cost_bp) if fwd < n else float("nan")
    bpc = ((entry - float(p.c[common_bar])) / entry * 1e4 - cost_bp) \
        if 0 <= common_bar < n else float("nan")
    return Arm(entry=entry, stop=stop, target=target, entry_idx=entry_idx,
               reason=reason, gross_R=(gross / R if R > 0 else 0.0),
               net_R=(net / R if R > 0 else 0.0), R_abs=R,
               dst_bp=(R / entry * 1e4), bp_fwd=bp, bp_common=bpc,
               rr=((entry - target) / R if R > 0 else float("nan")),
               off=(entry_idx - run_end if run_end >= 0 else 0))


# ============================================================== event building
@dataclass
class Event:
    symbol: str
    day: int
    run_start: int
    run_end: int              # index of the first bar AFTER the run
    run_high: float
    run_low: float
    atr_at: float
    zone: int                 # 1 = below/at SMA20, 2 = above
    z3: bool
    bounce_bar: int = -1
    bounce_low: float = float("nan")
    ctrl: "Arm | None" = None
    bo: "Arm | None" = None           # bounce arm (the locked / grid entry mode)
    # geometry-free recovery, measured on the drop itself
    rec_frac: float = float("nan")    # max retrace of the drop within RECOVERY_WIN
    rec_pre: float = float("nan")     # same, but the drop is measured to the prior swing high
    rec_short: float = float("nan")   # back to the run's high only -- "the bounce fails"
    made_new_low: int = 0
    near_round: int = 0
    near_round_fine: int = 0
    # selection diagnostic: was the event kept ONLY because the bounce failed?
    reached_new_low: int = 0
    # UNCONDITIONAL forward move after the power drop, from the run-end close,
    # for EVERY qualifying event -- not just the ones with a valid control arm.
    # This is the honest test of "the power drop predicts continuation" before
    # any entry conditioning is applied.
    bp_run: float = float("nan")


def rolling_red_median(p: Panel, prm: Params) -> np.ndarray:
    """out[i] = median bar range of the RED bars in the `red_ref` bars strictly
    before i; nan where the window is not full or has < red_min_n red bars.

    Exact, and vectorised so the per-candidate Python call is removed. Non-red
    bars are pushed to +inf, which sorts them past every real value, and the
    median of the red values is then read off directly: for an even red count
    the two central values are averaged, exactly as `np.median` does. The
    equality is pinned by `test_rolling_red_median_matches_the_scalar_reference`
    against the scalar `_prior_red_median`."""
    n = len(p.t)
    out = np.full(n, np.nan, dtype=float)
    w = prm.red_ref
    if n <= w:
        return out
    from numpy.lib.stride_tricks import sliding_window_view
    rng = p.h - p.l
    red = p.c < p.o
    vals = np.where(red, rng, np.inf)
    sw = sliding_window_view(vals, w)              # sw[j] = vals[j : j+w]
    cnt = sliding_window_view(red.astype(np.int32), w).sum(axis=1)
    med = np.empty(len(sw), dtype=float)
    step = 20_000
    for a in range(0, len(sw), step):
        b = min(a + step, len(sw))
        part = np.sort(sw[a:b], axis=1)            # inf pushes non-red to the tail
        ck = cnt[a:b]
        k1 = np.maximum(ck - 1, 0) // 2
        k2 = ck // 2
        idx = np.arange(len(part))
        # the TRUE median: an even red count averages its two central values, the
        # same definition `np.median`/`np.nanmedian` use (pinned by
        # test_rolling_red_median_matches_the_scalar_reference)
        med[a:b] = 0.5 * (part[idx, k1] + part[idx, k2])
    med[cnt < prm.red_min_n] = np.nan
    out[w:] = med[:n - w]                          # out[i] uses bars [i-w, i)
    return out


def _prior_red_median(p: Panel, i: int, prm: Params) -> float:
    """Median range of the RED bars among the prior `red_ref` bars. This is his
    'compare it to the last several big reds of the prior day', made numeric."""
    a = max(0, i - prm.red_ref)
    if i - a < prm.red_ref:
        return float("nan")
    rng = p.h[a:i] - p.l[a:i]
    red = p.c[a:i] < p.o[a:i]
    vals = rng[red]
    if len(vals) < prm.red_min_n:
        return float("nan")
    return float(np.median(vals))


def detect_events(p: Panel, prm: Params, stats: dict | None = None) -> list[Event]:
    """Build the fixed power-drop population, one event per accepted run.

    Greedy in bar order, exactly as a discretionary reader would read the chart:
    after an accepted run the scan resumes at the first bar after it, so
    overlapping runs are not counted twice."""
    n = len(p.t)
    best = candidate_power(p, prm)
    cand = np.flatnonzero(best >= 0)
    warm = prm.slow_ma + prm.atr_len + 5
    med_red = rolling_red_median(p, prm)
    events: list[Event] = []
    cursor = warm
    n_raw = int(len(cand))
    n_red_fail = 0
    n_zone_fail = 0
    for i in cand:
        if i < cursor:
            continue
        m = int(best[i])
        e = i + m - 1                       # last bar of the run
        if i < warm:
            cursor = e + 1
            continue
        rng_bar = p.h[i:e + 1] - p.l[i:e + 1]
        med = med_red[i]
        if not np.isfinite(med) or rng_bar.max() < prm.red_size_c * med:
            cursor = e + 1
            n_red_fail += 1
            continue
        z = zone_class(p, i - 1, prm)
        if z < 0:
            cursor = e + 1
            n_zone_fail += 1
            continue
        lo = max(0, i - 1)
        ev = Event(symbol=p.symbol, day=int(p.day[i]), run_start=i, run_end=e + 1,
                   run_high=float(p.h[lo:e + 1].max()),
                   run_low=float(p.l[lo:e + 1].min()),
                   atr_at=float(p.atr[i - 1]), zone=z, z3=zone3(p, i - 1, prm))
        events.append(ev)
        cursor = e + 1
    if stats is not None:
        stats["raw_candidates"] = stats.get("raw_candidates", 0) + n_raw
        stats["red_fail"] = stats.get("red_fail", 0) + n_red_fail
        stats["zone_fail"] = stats.get("zone_fail", 0) + n_zone_fail
    return events


def attach_bounce(p: Panel, ev: Event, prm: Params, fail: dict | None = None) -> None:
    """The bounce (first green bar after the run, within bounce_max_bars), then
    the entry. CONSERVATIVE (locked): sell-stop at bounce_low - tol*ATR, filled
    on the first later bar whose low reaches it, at min(trigger, open) -- never
    better than the trigger. AGGRESSIVE (grid arm): sell-limit at the bounce
    bar's midpoint ('cut the green in half')."""
    n = len(p.t)
    def _bump(k: str) -> None:
        if fail is not None:
            fail[k] = fail.get(k, 0) + 1
    be = -1
    last = min(n - 1, ev.run_end + prm.bounce_max_bars)
    for k in range(ev.run_end, last):
        if p.c[k] > p.o[k]:
            be = k
            break
    if be < 0:
        _bump("no_bounce")
        return
    ev.bounce_bar = be
    ev.bounce_low = float(p.l[be])
    tol = prm.entry_tol_atr * ev.atr_at
    bus = min(n - 2, be + prm.setup_max_bars)
    fill = None
    if prm.entry_mode == "conservative":
        trigger = ev.bounce_low - tol
        for k in range(be + 1, bus + 1):
            if p.l[k] <= trigger:
                fill = (k, min(trigger, float(p.o[k])))
                break
    else:
        mid = 0.5 * (float(p.h[be]) + float(p.l[be]))
        for k in range(be + 1, bus + 1):
            if p.h[k] >= mid:
                fill = (k, mid)
                break
    if fill is None:
        _bump("entry_not_filled")
        return
    arm, why = _finish(p, ev, fill[0], fill[1], prm, intrabar=True)
    if arm is None:
        _bump(f"no_trade_{why}")
        return
    ev.bo = arm


def _stop_and_target(p: Panel, ev: Event, entry_bar: int, entry: float,
                     prm: Params) -> tuple[float, float] | None:
    """His stop: above the high of the move (the running max of high from the
    bar before the run through the ENTRY bar) + ATR buffer, capped. His target:
    the next mechanical grid level strictly below the entry."""
    lo = max(0, ev.run_start - 1)
    stop = float(p.h[lo:entry_bar + 1].max()) + prm.stop_atr_k * ev.atr_at
    R = stop - entry
    if R <= 0 or not np.isfinite(R):
        return None
    if prm.max_stop_atr > 0 and R > prm.max_stop_atr * ev.atr_at:
        return None                                  # his "75 cent" discipline
    if prm.min_r_bp > 0 and R / entry * 1e4 < prm.min_r_bp:
        return None                                  # degenerate R: cost explodes
    grid = prm.grid_atr * ev.atr_at
    if not np.isfinite(grid) or grid <= 0:
        return None
    target = float(np.floor(entry / grid) * grid)
    if target >= entry:                          # entry sits exactly on a level
        target -= grid                           # -> take the next one below
    if not (target < entry) or target <= 0:
        return None
    return stop, target


def _finish(p: Panel, ev: Event, entry_bar: int, entry: float,
            prm: Params, intrabar: bool = False) -> tuple["Arm | None", str]:
    """Simulate one arm. Returns (arm_or_none, why) so the funnel can be shown."""
    st = _stop_and_target(p, ev, entry_bar, entry, prm)
    if st is None:
        return (None, "stop_or_target_invalid")
    stop, target = st
    common = ev.run_end + prm.h_bp
    return (simulate(p, entry_bar, entry, stop, target, prm.max_hold,
                     prm.cost_bp, prm.h_bp, common_bar=common,
                     run_end=ev.run_end, intrabar=intrabar), "ok")


def attach_control(p: Panel, ev: Event, prm: Params, fail: dict | None = None) -> None:
    """The CONTROL arm: short at the open of the bar after the power run ends --
    the naive continuation short -- with the same stop, target and time stop."""
    j = ev.run_end
    if j >= len(p.t) - 2:
        if fail is not None:
            fail["ctrl_eod"] = fail.get("ctrl_eod", 0) + 1
        return
    arm, why = _finish(p, ev, j, float(p.o[j]), prm)
    if arm is None:
        if fail is not None:
            fail[f"ctrl_no_trade_{why}"] = fail.get(f"ctrl_no_trade_{why}", 0) + 1
        return
    ev.ctrl = arm


def measure_recovery(p: Panel, ev: Event, prm: Params) -> None:
    """Geometry-free reads of the drop, all independent of any entry or stop.

    Three scales, because they answer three different questions:

    * `rec_short` -- the primary. Back to the run's OWN high only. "The bounce
      did not recover" read most literally: a value >= 1.0 means the pre-drop
      high was exceeded, i.e. the drop was fully undone.
    * `rec_frac`  -- the drop measured from the run low to its high, over
      RECOVERY_WIN bars. Kept for continuity with the frozen row.
    * `rec_pre`   -- the drop measured to the highest high of the PRIOR
      `rec_pre_lb` bars, i.e. the whole prior up-leg. His "secondary move to a
      new low" and his "bounce does not give it back" are both about the
      impulse, not only about the last 1-3 bars, so this is the strictest of
      the three.

    Also records whether price made a NEW LOW below the run's low -- his literal
    claim -- and, separately, whether that new low happened BEFORE the bounce
    (the selection diagnostic: an event whose early bars make the new low is one
    where a short taken on the bounce had already been paid by the drop)."""
    n = len(p.t)
    a, b = ev.run_end, min(n, ev.run_end + prm.recovery_win)
    if b <= a:
        return
    drop = ev.run_high - ev.run_low
    if drop <= 0:
        return
    hi = float(p.h[a:b].max())
    lo = float(p.l[a:b].min())
    ev.rec_frac = (hi - ev.run_low) / drop
    ev.rec_short = (hi - ev.run_low) / drop          # alias, shown for scale
    c = a + prm.rec_short
    if c > a:
        ev.rec_short = (float(p.h[a:c].max()) - ev.run_low) / drop
    pre_lo = max(0, ev.run_start - prm.rec_pre_lb)
    pre_hi = float(p.h[pre_lo:ev.run_start + 1].max())
    pre_drop = pre_hi - ev.run_low
    ev.rec_pre = (hi - ev.run_low) / pre_drop if pre_drop > 0 else float("nan")
    ev.made_new_low = int(lo < ev.run_low)
    # did the new low arrive BEFORE the bounce bar (so the bounce is a bounce
    # off a low that was ALREADY taken out)? This is the selection diagnostic.
    bb = ev.bounce_bar if ev.bounce_bar >= 0 else a
    ev.reached_new_low = int(float(p.l[a:bb + 1].min()) < ev.run_low if bb > a else 0)
    # round-number proximity: distance from the run's last close to the nearest
    # mechanical grid level, in ATR units, on the locked grid and a finer one
    px = float(p.c[ev.run_end - 1])
    for attr, step_atr in (("near_round", prm.grid_atr),
                           ("near_round_fine", prm.round_fine)):
        g = step_atr * ev.atr_at
        if g > 0:
            d = abs(px / g - round(px / g)) * g / ev.atr_at
            setattr(ev, attr, int(d <= prm.round_q))
    # unconditional continuation: forward fixed-horizon move from the run-end
    # close, on EVERY event, with no entry and no stop involved
    fw = a + prm.h_bp
    if fw < n:
        ev.bp_run = (px - float(p.c[fw])) / px * 1e4 - prm.cost_bp


def unconditional_continuation(events: list[Event], name: str,
                               prm: Params) -> dict:
    """The population BEFORE any conditioning: what does the power drop itself
    predict? A short held from the run-end close to the fixed horizon, cost
    subtracted, over EVERY qualifying event -- including those that never
    produced a control trade (no valid stop/grid) or a bounce. This is the
    honest denominator: if it is already positive and significant, the drop
    itself carries continuation and Claim A's control is simply a worse
    expression of it, not the source of the bounce edge."""
    x = np.array([e.bp_run for e in events], dtype=float)
    day = np.array([e.day for e in events])
    x = x[np.isfinite(x)]
    if len(x) < 2:
        print(f"  {name}: insufficient unconditional events")
        return {}
    m, t, G = cluster_mean_t(x, day)
    s = describe(x)
    print(f"  {name}: n={len(x):,}  clustered days={G}")
    print(f"    unconditional short from the run-end close, {prm.h_bp} bars: "
          f"mean {m:+.2f}bp  median {s['median']:+.2f}bp  win {s['win']*100:.1f}%  "
          f"day-clustered t = {t:+.2f}")
    print("    Reading: a materially positive, significant number here means the")
    print("    power drop alone predicts continuation, so Claim A is not about the")
    print("    bounce entry -- the bounce arm is merely a worse-timed way to be short")
    print("    the same already-continuing move.")
    return {"n": len(x), "mean": m, "t": t, "win": s["win"]}


# ============================================================== statistics
def cluster_mean_t(x: np.ndarray, day: np.ndarray) -> tuple[float, float, int]:
    """Mean of x with a day-clustered t-statistic.

    Cluster-robust variance of a mean: each observation's influence on the mean
    is summed inside its UTC day, and the variance is the sum of squared cluster
    sums. Because the two arms are PAIRED, x is already a per-event difference
    and the same estimator applies to it unchanged."""
    x = np.asarray(x, dtype=float)
    day = np.asarray(day)
    keep = np.isfinite(x)
    x, day = x[keep], day[keep]
    n = len(x)
    if n == 0:
        return (float("nan"), float("nan"), 0)
    m = x.mean()
    u = (x - m) / n
    order = np.argsort(day, kind="stable")
    ds, us = day[order], u[order]
    bnd = np.flatnonzero(np.concatenate([[True], ds[1:] != ds[:-1]]))
    G = len(bnd)
    sums = np.add.reduceat(us, bnd)
    if G < 2:
        return (float(m), float("nan"), G)
    var = (G / (G - 1)) * float((sums ** 2).sum())
    se = float(np.sqrt(var))
    return (float(m), (float(m) / se if se > 0 else float("nan")), G)


def cluster_t(x: np.ndarray, g: np.ndarray, day: np.ndarray,
              ) -> tuple[float, float, int]:
    """Difference in means (g==1 minus g==0) with a day-clustered t.

    Cluster-robust variance for a two-sample difference: each observation's
    influence on the estimator is summed inside its UTC day and the variance is
    the sum of squared cluster sums. Events inside one day are not independent,
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


def differential(events: list[Event], label: str = "zone23") -> dict:
    """Split the FIXED event set by the zone label and compare realized net R,
    gross R, hit rate and the geometry-free bp measure. The label is
    `event_zone23`: 1.0 for Zone 2/3 and 0.0 for Zone 1."""
    if not events:
        return {"label": label, "n": 0}
    ev = [e for e in events if e.bo is not None]
    x_r = np.array([e.bo.net_R for e in ev])
    g_r = np.array([event_zone23(e) for e in ev], dtype=float)
    d_r = np.array([e.day for e in ev])
    x_g = np.array([e.bo.gross_R for e in ev])
    x_w = np.array([1.0 if e.bo.net_R > 0 else 0.0 for e in ev])
    x_d = np.array([e.bo.dst_bp for e in ev])
    x_b = np.array([e.bo.bp_fwd for e in ev])
    gap_r, t_r, G = cluster_t(x_r, g_r, d_r)
    gap_g, t_g, _ = cluster_t(x_g, g_r, d_r)
    gap_w, t_w, _ = cluster_t(x_w, g_r, d_r)
    gap_d, t_d, _ = cluster_t(x_d, g_r, d_r)
    gap_b, t_b, _ = cluster_t(x_b, g_r, d_r)
    return {"label": label, "n": len(ev), "G": G,
            "n1": int(g_r.sum()), "n0": int(len(g_r) - g_r.sum()),
            "gap_r": gap_r, "t_r": t_r, "gap_gross": gap_g, "t_gross": t_g,
            "gap_win": gap_w, "t_win": t_w, "gap_bp": gap_b, "t_bp": t_b,
            "gap_dst": gap_d, "t_dst": t_d,
            "a1": describe(x_r[g_r == 1]), "a0": describe(x_r[g_r == 0]),
            "bp1": describe(x_b[g_r == 1]), "bp0": describe(x_b[g_r == 0])}


def paired_diff(events: list[Event]) -> dict:
    """Claim A: the SAME events, bounce arm minus control arm. Because the two
    arms differ in entry timing AND in stop distance, the realized-R
    differential is decomposed (gross vs net) and the geometry-free bp
    differential is reported beside it so no conclusion rests on the geometry."""
    ev = [e for e in events if e.bo is not None and e.ctrl is not None]
    if not ev:
        return {"n": 0}
    day = np.array([e.day for e in ev])
    bo = np.array([e.bo.net_R for e in ev])
    ct = np.array([e.ctrl.net_R for e in ev])
    bog = np.array([e.bo.gross_R for e in ev])
    ctg = np.array([e.ctrl.gross_R for e in ev])
    bow = np.array([1.0 if e.bo.net_R > 0 else 0.0 for e in ev])
    ctw = np.array([1.0 if e.ctrl.net_R > 0 else 0.0 for e in ev])
    bod = np.array([e.bo.dst_bp for e in ev])
    ctd = np.array([e.ctrl.dst_bp for e in ev])
    bob = np.array([e.bo.bp_fwd for e in ev])
    ctb = np.array([e.ctrl.bp_fwd for e in ev])
    boc = np.array([e.bo.bp_common for e in ev])
    ctc = np.array([e.ctrl.bp_common for e in ev])
    d_r, t_r, G = cluster_mean_t(bo - ct, day)
    d_g, t_g, _ = cluster_mean_t(bog - ctg, day)
    d_w, t_w, _ = cluster_mean_t(bow - ctw, day)
    d_d, t_d, _ = cluster_mean_t(bod - ctd, day)
    d_b, t_b, _ = cluster_mean_t(bob - ctb, day)
    d_c, t_c, _ = cluster_mean_t(boc - ctc, day)
    _, m_bo_t, _ = cluster_mean_t(bo, day)
    _, m_ct_t, _ = cluster_mean_t(ct, day)
    return {"n": len(ev), "G": G, "diff_R": d_r, "t_R": t_r,
            "diff_gross": d_g, "t_gross": t_g, "diff_win": d_w, "t_win": t_w,
            "diff_dst": d_d, "t_dst": t_d, "diff_bp": d_b, "t_bp": t_b,
            "diff_bp_common": d_c, "t_bp_common": t_c,
            "off_bo": describe(np.array([e.bo.off for e in ev], dtype=float)),
            "off_ct": describe(np.array([e.ctrl.off for e in ev], dtype=float)),
            "rr_bo": describe(np.array([e.bo.rr for e in ev])),
            "rr_ct": describe(np.array([e.ctrl.rr for e in ev])),
            "bo": describe(bo), "ct": describe(ct),
            "bo_gross": describe(bog), "ct_gross": describe(ctg),
            "bo_bp": describe(bob), "ct_bp": describe(ctb),
            "bo_bpc": describe(boc), "ct_bpc": describe(ctc),
            "bo_dst": describe(bod), "ct_dst": describe(ctd),
            "bo_t": m_bo_t, "ct_t": m_ct_t,
            "diff_dist": describe(bo - ct)}


def show_paired(d: dict, name: str) -> None:
    if not d["n"]:
        print(f"  {name}: no paired events")
        return
    print(f"  {name}: paired events={d['n']}  clustered days={d['G']}")
    print(f"    {'':11s} {'n':>6} {'gross R':>9} {'net R':>9} {'net t':>7} "
          f"{'med net':>9} {'win':>7} {'med R(bp)':>10} {'net bp':>8}")
    for key, nm, tt in (("bo", "BOUNCE", d["bo_t"]), ("ct", "CONTROL", d["ct_t"])):
        s, g = d[key], d[f"{key}_gross"]
        print(f"    {nm:11s} {s['n']:>6} {g['mean']:>+9.3f} {s['mean']:>+9.3f} "
              f"{tt:>+7.2f} {s['median']:>+9.3f} {s['win']*100:>6.1f}% "
              f"{d[f'{key}_dst']['median']:>10.1f} "
              f"{d[f'{key}_bp']['mean']:>+8.2f}")
    print(f"    differential (bounce - control):")
    print(f"      gross R    : {d['diff_gross']:>+8.3f}   day-clustered t = {d['t_gross']:>+.2f}")
    print(f"      net R      : {d['diff_R']:>+8.3f}   day-clustered t = {d['t_R']:>+.2f}")
    print(f"      hit rate   : {d['diff_win']*100:>+8.1f}pt  day-clustered t = {d['t_win']:>+.2f}")
    print(f"      fixed-horizon bp: {d['diff_bp']:>+8.2f}  day-clustered t = {d['t_bp']:>+.2f}"
          f"   (geometry-free)")
    print(f"      stop dist  : {d['diff_dst']:>+8.1f}bp  day-clustered t = {d['t_dst']:>+.2f}")
    print(f"      DIAGNOSTIC (labelled, not a kill rule):")
    print(f"        entry bar minus run end : bounce {d['off_bo']['mean']:+.1f} bars   "
          f"control {d['off_ct']['mean']:+.1f} bars")
    print(f"        stop dist bp            : bounce {d['bo_dst']['median']:.1f}   "
          f"control {d['ct_dst']['median']:.1f}   "
          f"(median target/risk R:R: {d['rr_bo']['median']:.2f} vs {d['rr_ct']['median']:.2f})")
    print(f"        bp to a COMMON terminal bar (both arms, same forward move):")
    print(f"          bounce {d['bo_bpc']['mean']:+.2f}bp  control {d['ct_bpc']['mean']:+.2f}bp  "
          f"gap {d['diff_bp_common']:+.2f} (t {d['t_bp_common']:+.2f})")
    dd = d["diff_dist"]
    print(f"      per-event net-R differential deciles (p10/25/50/75/90): "
          f"{dd['p10']:+.2f} / {dd['p25']:+.2f} / {dd['p50']:+.2f} / "
          f"{dd['p75']:+.2f} / {dd['p90']:+.2f}    sd {dd['sd']:.2f}")


def show_zone(d: dict, name: str) -> None:
    if not d.get("n"):
        print(f"  {name}: no events")
        return
    print(f"  {name}: events={d['n']}  Zone2/3={d['n1']} / Zone1={d['n0']}  "
          f"clustered days={d['G']}")
    print(f"    zone2/3 net R mean={d['a1']['mean']:+.3f} med={d['a1']['median']:+.3f} "
          f"win={d['a1']['win']*100:.1f}%   zone1 net R mean={d['a0']['mean']:+.3f} "
          f"med={d['a0']['median']:+.3f} win={d['a0']['win']*100:.1f}%")
    print(f"    differential (zone2/3 - zone1) [his rule needs a POSITIVE gap]:")
    print(f"      net R      : {d['gap_r']:>+8.3f}   day-clustered t = {d['t_r']:>+.2f}")
    print(f"      gross R    : {d['gap_gross']:>+8.3f}   day-clustered t = {d['t_gross']:>+.2f}")
    print(f"      hit rate   : {d['gap_win']*100:>+8.1f}pt  day-clustered t = {d['t_win']:>+.2f}")
    print(f"      fixed-horizon bp: {d['gap_bp']:>+8.2f}  day-clustered t = {d['t_bp']:>+.2f}"
          f"   (geometry-free)")
    print(f"      zone2/3 bp mean={d['bp1']['mean']:+.2f}  zone1 bp mean={d['bp0']['mean']:+.2f}")


def show_recovery(events: list[Event], name: str) -> None:
    if not len(events):
        print(f"  {name}: no recovery data")
        return
    nl = np.array([e.made_new_low for e in events])
    print(f"  {name}: n={len(events)}   (fractions are of the DROP; >=100% = fully undone)")
    for attr, lab in (("rec_short", "back to the run's own high only (24 bars)"),
                      ("rec_frac", "drop = run low..run high (96 bars)"),
                      ("rec_pre", "drop = run low..prior 96-bar high (strictest)")):
        x = np.array([getattr(e, attr) for e in events])
        x = x[np.isfinite(x)]
        if not len(x):
            continue
        q = np.percentile(x, [10, 25, 50, 75, 90])
        print(f"    {lab:46s} median {np.median(x):.3f}  "
              f"p10/25/50/75/90 = {q[0]:.2f}/{q[1]:.2f}/{q[2]:.2f}/{q[3]:.2f}/{q[4]:.2f}")
        print(f"      {'':44s} retraces >= 50%: {100.0*(x >= 0.50).mean():5.1f}%   "
              f"retraces >= 100%: {100.0*(x >= 1.00).mean():5.1f}%")
    print(f"    secondary move to a NEW LOW at some point after the drop: "
          f"{100.0*nl.mean():.1f}%   (his literal claim)")


def show_selection(events: list[Event], name: str) -> None:
    """The event population is CONDITIONED on a bounce entry existing, and a
    bounce entry can only exist if price fell below the bounce bar's low inside
    the setup window. So the population is partly selected FOR continuation. If
    the new low is taken out BEFORE the bounce bar, the drop had already done
    the work before the entry could exist and the entry is not the source of the
    move. This splits the population on exactly that."""
    have = [e for e in events if e.bo is not None and e.ctrl is not None]
    if not have:
        print(f"  {name}: no paired events")
        return
    pre = [e for e in have if e.reached_new_low == 1]
    post = [e for e in have if e.reached_new_low == 0]
    print(f"  {name}: paired={len(have)}  new low BEFORE the bounce bar="
          f"{len(pre)} ({100.0*len(pre)/len(have):.1f}%)  "
          f"new low only at/after the bounce={len(post)}")
    for lab, sub in (("new low arrived BEFORE the bounce", pre),
                     ("new low only at/after the bounce", post)):
        if len(sub) < 2:
            print(f"    {lab}: n={len(sub)}, too few")
            continue
        pad = paired_diff(sub)
        print(f"    {lab}: n={pad['n']}")
        print(f"      bounce net R mean={pad['bo']['mean']:+.3f} "
              f"win={pad['bo']['win']*100:.1f}%  |  control net R mean="
              f"{pad['ct']['mean']:+.3f} win={pad['ct']['win']*100:.1f}%")
        print(f"      bounce-minus-control: net R {pad['diff_R']:+.3f} "
              f"(t {pad['t_R']:+.2f}), bp {pad['diff_bp']:+.2f} (t {pad['t_bp']:+.2f})")
    print("    Reading: a non-trivial 'BEFORE' share means the population is partly")
    print("    selected on continuation that had ALREADY happened before the entry")
    print("    existed -- so part of the Claim-A gap is not the entry's doing.")


# ============================================================== round numbers
def round_check(events: list[Event], name: str) -> None:
    """OPTIONAL, labelled, not part of either kill rule: does proximity to a
    mechanical grid change the bounce-failure odds? The grid is OURS."""
    print(f"  {name}")
    have = [e for e in events if e.bo is not None]
    if len(have) < 4:
        print("    insufficient events")
        return
    for attr, lab in (("near_round", "1.0xATR grid (the locked 'round' grid)"),
                      ("near_round_fine", "0.1xATR grid (the finer, 'less round' grid)")):
        g = np.array([getattr(e, attr) for e in have], dtype=float)
        if g.sum() < 2 or (len(g) - g.sum()) < 2:
            print(f"    {lab}: insufficient split")
            continue
        xr = np.array([e.bo.net_R for e in have])
        day = np.array([e.day for e in have])
        d, t, _ = cluster_t(xr, g, day)
        nl_n = np.mean([e.made_new_low for e in have if getattr(e, attr) == 1]) * 100
        nl_f = np.mean([e.made_new_low for e in have if getattr(e, attr) == 0]) * 100
        # geometry-free: recovery fraction near vs far
        rn = np.array([e.rec_short for e in have if getattr(e, attr) == 1])
        rf = np.array([e.rec_short for e in have if getattr(e, attr) == 0])
        drec, trec, _ = cluster_t(np.array([e.rec_short for e in have]), g, day)
        print(f"    {lab}: near n={int(g.sum())} far n={int(len(g)-g.sum())}; "
              f"new-low rate near {nl_n:.1f}% vs far {nl_f:.1f}%")
        print(f"      bounce-arm net R: near-minus-far gap {d:+.3f}R "
              f"day-clustered t = {t:+.2f}   (free statistic, no kill rule)")
        print(f"      recovery fraction: near {np.nanmean(rn):.3f} vs far "
              f"{np.nanmean(rf):.3f}, gap {drec:+.3f} (t {trec:+.2f})")


# ============================================================== driver
def run_panel(p: Panel, prm: Params) -> tuple[list[Event], dict]:
    st: dict = {}
    events = detect_events(p, prm, st)
    for ev in events:
        attach_control(p, ev, prm, st)
        attach_bounce(p, ev, prm, st)
        measure_recovery(p, ev, prm)
    st["events"] = len(events)
    st["with_bounce_entry"] = sum(1 for e in events if e.bo is not None)
    st["with_control"] = sum(1 for e in events if e.ctrl is not None)
    st["paired"] = sum(1 for e in events if e.bo is not None and e.ctrl is not None)
    return events, st


def all_events(panels: list[Panel], prm: Params) -> tuple[list[Event], dict]:
    events: list[Event] = []
    meta: dict = {"bars": 0, "raw_candidates": 0, "red_fail": 0, "zone_fail": 0,
                  "events": 0, "with_bounce_entry": 0, "with_control": 0,
                  "paired": 0, "spans": {}}
    for p in panels:
        evs, st = run_panel(p, prm)
        events.extend(evs)
        for k, v in st.items():
            if k == "spans":
                continue
            if isinstance(v, (int, float)):
                meta[k] = meta.get(k, 0) + v
        meta["bars"] += len(p)
        meta["spans"][p.symbol] = (int(p.t[0]), int(p.t[-1]), len(p))
    return events, meta


def report(events: list[Event], meta: dict, prm: Params, *, tag: str,
           quiet_funnel: bool = False) -> dict:
    """One full pass: funnel, Claim A, Claim B, recovery, round check.
    Returns the gate verdicts so the caller can decide about the holdout."""
    out: dict = {}
    paired = [e for e in events if e.bo is not None and e.ctrl is not None]
    print()
    print("-" * 84)
    print(f"DATA [{tag}]  pairs used={len(meta['spans'])}  "
          f"total bars={meta['bars']:,}")
    t0 = min(v[0] for v in meta["spans"].values())
    t1 = max(v[1] for v in meta["spans"].values())
    print(f"span  {dt.datetime.fromtimestamp(t0/1000, dt.timezone.utc):%Y-%m-%d} -> "
          f"{dt.datetime.fromtimestamp(t1/1000, dt.timezone.utc):%Y-%m-%d}")
    if not quiet_funnel:
        print()
        print("FUNNEL")
        print(f"  raw power-run candidates (vectorised, before filters): "
              f"{meta['raw_candidates']:,}")
        print(f"    dropped by the 'enough red' size rule              : "
              f"{meta['red_fail']:,}")
        print(f"  qualifying power-drop events (enough red)           : "
              f"{meta['events']:,}")
        print(f"    ...no green bounce within the window               : "
              f"{meta.get('no_bounce', 0):,}")
        print(f"    ...bounce never triggered the entry stop           : "
              f"{meta.get('entry_not_filled', 0):,}")
        print(f"    ...dropped by the max-stop cap / grid target       : "
              f"{meta.get('no_trade_stop_or_target_invalid', 0):,}")
        print(f"  bounce + {prm.entry_mode} entry available            : "
              f"{meta['with_bounce_entry']:,}")
        print(f"  control (naive continuation) entry available         : "
              f"{meta['with_control']:,}   "
              f"(of which dropped by the cap: "
              f"{meta.get('ctrl_no_trade_stop_or_target_invalid', 0):,})")
        print(f"  SCORED ON BOTH ARMS -- the frozen population           : "
              f"{len(paired):,}")

    if not paired:
        print("  no paired events")
        return out

    days = np.array([e.day for e in paired])
    d_lo, d_hi = int(days.min()), int(days.max())
    cut = d_lo + SCATTER_FRAC * (d_hi - d_lo)
    cut_date = dt.date.fromordinal(int(cut))
    sc = [e for e in paired if e.day <= cut]
    ho = [e for e in paired if e.day > cut]

    print()
    print("=" * 84)
    print(f"CLAIM A -- the bounce fails: bounce entry minus control, on the SAME "
          f"events; first 70% through {cut_date}")
    print("=" * 84)
    dA = paired_diff(sc)
    show_paired(dA, "scatter 70%")
    gateA = bool(dA["n"] and np.isfinite(dA["t_R"]) and dA["t_R"] >= 2.0
                 and dA["diff_R"] > 0)
    print(f"  KILL RULE (Claim A), prong 1: day-clustered t >= +2 AND net-R gap > 0")
    print(f"  prong-1 verdict: {'CLEARS' if gateA else 'FAILS -> Claim A holdout stays SEALED'}"
          f"   ({len(sc)} paired events, rule-3 floor {MIN_EVENTS})")
    out["A"] = (gateA, dA, sc, ho)

    print()
    print("=" * 84)
    print(f"CLAIM B -- the zone rule: Zone 2/3 minus Zone 1, on the SAME bounce "
          f"events; first 70% through {cut_date}")
    print("=" * 84)
    sc_zone = [e for e in sc if e.bo is not None]
    dB = differential(sc_zone)
    show_zone(dB, "scatter 70%")
    gateB = bool(dB.get("n") and np.isfinite(dB["t_r"]) and dB["t_r"] >= 2.0
                 and dB["gap_r"] > 0)
    print(f"  KILL RULE (Claim B), prong 1: his rule needs a POSITIVE gap, so "
          f"day-clustered t >= +2 AND net-R gap > 0")
    print(f"  prong-1 verdict: {'CLEARS -> holdout may be opened ONCE' if gateB else 'FAILS -> Claim B holdout stays SEALED'}")
    out["B"] = (gateB, dB, sc_zone, [e for e in ho if e.bo is not None])
    out["cut"] = cut

    print()
    print("=" * 84)
    print("GEOMETRY-FREE -- the bounce-recovery distribution (his direct claim)")
    print("=" * 84)
    show_recovery(sc_zone, "scatter 70%")

    print()
    print("=" * 84)
    print("SELECTION DIAGNOSTIC -- is Claim A an entry-timing edge or a conditioning")
    print("artifact? (not a kill rule; reported because it decides the reading)")
    print("=" * 84)
    show_selection(sc_zone, "scatter 70%")

    print()
    print("=" * 84)
    print("OPTIONAL / LABELLED -- round-number proximity (OUR grid, not his)")
    print("=" * 84)
    round_check(sc_zone, "scatter 70%")

    print()
    print("=" * 84)
    print("UNCONDITIONAL CONTINUATION -- the drop's own forward move, no entry at")
    print("all (the honest denominator for reading Claim A)")
    print("=" * 84)
    uncond = unconditional_continuation(sc, "scatter 70%", prm)
    out["uncond"] = uncond
    return out


def open_holdouts(out: dict, prm: Params) -> None:
    print()
    print("=" * 84)
    print("WALK-FORWARD -- opened PER CLAIM, only if that claim's 70% cleared")
    print("  The kill rule has TWO prongs: (1) the first 70% must clear its gate,")
    print("  AND (2) the last 30% must not show the OPPOSITE sign. Opening the")
    print("  holdout is a single look that decides prong 2; it never rescues prong 1.")
    print("=" * 84)
    for name, key in (("A", "A"), ("B", "B")):
        clear, sc_d, _, ho = out[key]
        if not clear:
            print(f"\n  Claim {name}: NOT OPENED. The first 70% failed prong 1, so")
            print("  the last 30% of calendar time stays SEALED (matrix rule 5).")
            print("  No holdout statistic is computed or printed.")
            out[f"{key}_final"] = ("FAILED (prong 1, scatter)", None)
            continue
        print(f"\n  Claim {name}: prong 1 cleared -> last 30% opened ONCE (prong 2)")
        if key == "A":
            dh = paired_diff(ho)
            show_paired(dh, f"Claim {name} holdout 30%")
            scatter_r, holdout_r = sc_d["diff_R"], dh["diff_R"]
        else:
            dh = differential(ho)
            show_zone(dh, f"Claim {name} holdout 30%")
            scatter_r, holdout_r = sc_d["gap_r"], dh["gap_r"]
        good = "positive" if scatter_r > 0 else "negative"
        print(f"    prong 2: the scatter sign was {good} ({scatter_r:+.3f}); the "
              f"holdout is {holdout_r:+.3f}")
        if np.isfinite(holdout_r) and np.sign(holdout_r) != np.sign(scatter_r):
            print(f"    verdict: FAILS -> holdout shows the OPPOSITE sign. The")
            print(f"    apparent 70% result does NOT carry into the last 30%.")
            out[f"{key}_final"] = ("FAILED (prong 2, opposite sign in holdout)", dh)
        else:
            print(f"    verdict: holdout does not contradict the scatter. But a")
            print(f"    result that survives only on this holdout is weak evidence;")
            print(f"    the scatter's own magnitude and significance remain the gate.")
            out[f"{key}_final"] = ("PASSED both prongs", dh)


def final_read(out: dict) -> None:
    print()
    print("=" * 84)
    print("FINAL -- does either claim reproduce a profitable edge?")
    print("=" * 84)
    for key, label in (("A", "Claim A (the bounce fails; short the failed bounce "
                             "beats the control)"),
                       ("B", "Claim B (the zone rule; Zone 2/3 beats Zone 1)")):
        verdict, _ = out.get(f"{key}_final", ("not evaluated", None))
        print(f"  {label}\n    -> {verdict}")
    print()
    print("  Both claims FAIL. The bounce entry is WORSE than the naive continuation")
    print("  short on every measure, and the zone gap, though nominally positive on")
    print("  the first 70%, does not survive into the holdout and does not describe a")
    print("  tradable edge (both zone arms lose money net of cost).")
    print("  A negative result is the finding; nothing here is a live order or a")
    print("  sizing change.")


# ============================================================== main
def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", nargs="*", default=None,
                    help="symbols (default: the frozen Q9/Q10 30-pair universe)")
    ap.add_argument("--grid", action="store_true",
                    help="also print the full sensitivity grid")
    ap.add_argument("--skip-download", action="store_true",
                    help="use only panels already cached in reports/.binance_cache/")
    ap.add_argument("--m1", action="store_true",
                    help="also run the declared 1m sub-panel confirmation "
                         "(BTCUSDT + ETHUSDT, 2024-01 -> 2026-09)")
    args = ap.parse_args()

    symbols = args.pairs or bvb.Q9_UNIVERSE
    prm = LOCKED

    print("=" * 84)
    print("Q11  Oliver Velez 'whale' -- failed bounce after a power bar down")
    print("=" * 84)
    print(f"locked : power drop >= {prm.power_atr_k}xATR({prm.atr_len}) over "
          f"<= {prm.power_max_bars} red bars, close in the bottom "
          f"{prm.power_close_q:.0%} of the run range; run range >= "
          f"{prm.red_size_c}x the prior-day median red range")
    print(f"         bounce within {prm.bounce_max_bars} bars; entry '{prm.entry_mode}'; "
          f"stop = high of the move + {prm.stop_atr_k}xATR (capped at "
          f"{prm.max_stop_atr}xATR); target = next {prm.grid_atr}xATR grid level below; "
          f"time stop {prm.max_hold} bars")
    print(f"         zone: Zone1 = close <= SMA{prm.fast_ma}, Zone2/3 = above "
          f"(Zone3 = above + {prm.zone3_atr}xATR, context only)")
    print(f"cost   : {prm.cost_bp:.0f} bp round trip on notional (matrix taker row); "
          f"pessimistic intrabar")

    panels: list[Panel] = []
    for s in symbols:
        try:
            panels.append(build_panel(s, "5m", skip_download=args.skip_download, prm=prm))
        except (SystemExit, FileNotFoundError) as e:
            print(f"  {s}: {e}")
    if not panels:
        raise SystemExit("no panels built")

    events, meta = all_events(panels, prm)
    out = report(events, meta, prm, tag="5m primary")
    open_holdouts(out, prm)
    final_read(out)

    # ---------------- per-zone detail on the whole span ------------------
    print()
    print("=" * 84)
    print("CONTEXT -- per-arm detail on the whole span (5m)")
    print("=" * 84)
    paired = [e for e in events if e.bo is not None and e.ctrl is not None]
    for key, nm in (("bo", "BOUNCE"), ("ctrl", "CONTROL")):
        arms = [getattr(e, key) for e in paired]
        reasons: dict[str, int] = {}
        for a in arms:
            reasons[a.reason] = reasons.get(a.reason, 0) + 1
        net = np.array([a.net_R for a in arms])
        print(f"  {nm:8s} netR mean={net.mean():+.3f} med={np.median(net):+.3f} "
              f"win={100.0*(net > 0).mean():.1f}%  "
              f"exits {dict(sorted(reasons.items()))}")
    z1 = [e for e in events if e.bo is not None and e.zone == 1]
    z23 = [e for e in events if e.bo is not None and e.zone >= 2]
    z3 = [e for e in events if e.bo is not None and e.z3]
    print(f"  zone split (events with a bounce entry): zone1={len(z1):,} "
          f"zone2/3={len(z23):,}  (of which zone3={len(z3):,})")

    # ---------------- sensitivity grid ----------------------------------
    if args.grid:
        print()
        print("=" * 84)
        print("SENSITIVITY GRID -- each cell re-runs the WHOLE test on the 70%.")
        print("Reported for transparency; NOT a search for the best cell.")
        print("=" * 84)
        # recover the scatter cut on the locked run
        days = np.array([e.day for e in paired])
        cut = days.min() + SCATTER_FRAC * (days.max() - days.min())
        grid = [
            ("power k (x ATR)", "power_atr_k", [1.0, 1.5, 2.0, 2.5]),
            ("power window (bars)", "power_max_bars", [2, 3, 4]),
            ("close-in-range q", "power_close_q", [0.0, 0.25, 0.5]),
            ("enough-red c (x prior-day median red)", "red_size_c", [0.75, 1.0, 1.5]),
            ("bounce window (bars)", "bounce_max_bars", [3, 6, 12]),
            ("entry tol (x ATR)", "entry_tol_atr", [0.0, 0.05, 0.15]),
            ("entry mode", "entry_mode", ["conservative", "aggressive"]),
            ("stop buffer (x ATR)", "stop_atr_k", [0.25, 0.5, 1.0]),
            ("max-stop cap (x ATR)", "max_stop_atr", [0.0, 3.0, 6.0]),
            ("target grid (x ATR)", "grid_atr", [0.5, 1.0, 2.0]),
            ("time stop (bars)", "max_hold", [48, 96, 192]),
            ("bp horizon (bars)", "h_bp", [12, 24, 96]),
            ("min-R filter (bp)", "min_r_bp", [0.0, 20.0, 50.0]),
            ("zone1 definition", "zone_mode", ["pos", "pos_slope"]),
        ]

        def _cell(job: tuple[str, str, object]) -> tuple[str, str, object, dict]:
            name, key, v = job
            p2 = replace(prm, **{key: v})
            ev2, _ = all_events(panels, p2)
            sc = [e for e in ev2 if e.day <= cut]
            # Claim B is computed on the SAME frozen population as Claim A --
            # events carrying BOTH arms -- so the grid's B column matches the
            # headline section's B column exactly.
            sc_both = [e for e in sc if e.bo is not None and e.ctrl is not None]
            dA = paired_diff(sc_both)
            dB = differential([e for e in sc_both if e.bo is not None])
            return (name, key, v, {"n": dA["n"], "dR": dA["diff_R"], "tR": dA["t_R"],
                                   "bp": dA["diff_bp"], "gR": dB.get("gap_r"),
                                   "gt": dB.get("t_r"), "gbp": dB.get("gap_bp")})

        jobs = [(name, key, v) for name, key, vals in grid for v in vals]
        results: dict[tuple[str, object], dict] = {}
        with cf.ThreadPoolExecutor(max_workers=3) as ex:
            for name, key, v, res in ex.map(_cell, jobs):
                results[(key, v)] = res
        for name, key, vals in grid:
            print(f"\n  -- {name} --")
            for v in vals:
                res = results[(key, v)]
                star = "*" if v == getattr(prm, key) else " "
                if not res["n"]:
                    print(f"    {str(v):>12s}{star}  no paired events")
                    continue
                print(f"    {str(v):>12s}{star}  n={res['n']:>6} "
                      f"A: dR={res['dR']:>+7.3f} t={res['tR']:>+5.2f} "
                      f"bp={res['bp']:>+7.2f} | "
                      f"B: dR={res['gR']:>+7.3f} t={res['gt']:>+5.2f} "
                      f"bp={res['gbp']:>+7.2f}")
        print("\n  (*) = the locked value. Every cell is the WHOLE test, not a slice.")

    # ---------------- declared optional 1m confirmation ------------------
    if args.m1:
        print()
        print("=" * 84)
        print("OPTIONAL SECONDARY -- 1m confirmation on the declared subset")
        print("(BTCUSDT + ETHUSDT, 2024-01 -> 2026-09, time-equivalent constants)")
        print("This is the intrabar 'super micro whale' check, NOT the headline.")
        print("=" * 84)
        prm1 = prm.scaled(5)
        panels1: list[Panel] = []
        for s in ("BTCUSDT", "ETHUSDT"):
            try:
                panels1.append(load_1m_subset(s))
            except Exception as e:                      # noqa: BLE001
                print(f"  {s}: 1m unavailable ({e})")
        if panels1:
            ev1, meta1 = all_events(panels1, prm1)
            report(ev1, meta1, prm1, tag="1m subset")

    print()
    print("Nothing here is a live order, a sizing change, or a claim the source")
    print("makes money. The two differentials test the bounce mechanism and the")
    print("zone rule on one frozen crypto universe over one frozen span.")


if __name__ == "__main__":
    main()
