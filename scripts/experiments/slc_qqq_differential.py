#!/usr/bin/env python
"""Q8: SLC (Structure / Level / Confirmation) -- a DIFFERENTIAL test on QQQ 5m.

Source: "The BEST Day Trading Strategy For Beginners in 2026 (Simple and
Proven)," Data Trader, https://www.youtube.com/watch?v=ExvoIqNglOk. See the
Q8 section of docs/DAY_TRADE_EDGE_MATRIX.md for the frozen spec. This module
implements exactly that spec and nothing else.

WHAT IS TESTED, AND WHAT IS NOT
-------------------------------
The video claims a discretionary day-trading framework with no evidence: one
after-the-fact worked example, n=1. This harness does NOT try to reproduce
the method's profitability, and does NOT claim the source is right or wrong
about the whole method. It tests ONE claim -- that the stochastic
confirmation filter has *selectivity*.

The design is differential. ONE zone set is built and frozen. Every touch of
a zone is recorded. Each touch is then labelled confirmed or unconfirmed by
whether the stochastic trigger fires inside a fixed window, and the forward
returns of the two groups are compared AT THE SAME ZONES. Holding the zone
set fixed removes the usual confound (a different filter also changes which
zones are *found*); only the filter's own discrimination is left in the gap.

If the filter is decoration the two groups' forward-return distributions are
the same and the gap is ~0 and insignificant. If the filter selects, a gap
appears. The video's "the filter saved you from a losing trade" is an
anecdote; this turns it into a two-sample question over a fixed zone set.

FROZEN, EX ANTE (see the matrix row; do not change these after seeing output)
-----------------------------------------------------------------------------
  DISP_ATR_K  = 1.5   "sharp aggressive move" = net move >= 1.5 * ATR(14),
                      over at most DISP_MAX_BARS = 3 consecutive 5m bars.
  Stochastic  = 14, 3, 3, bands 80/20.  (The source's on-screen settings are
                      unrecoverable; this is the industry default, disclosed.)
  STRUCT_P    = 3     fractal swing, 3 bars each side, on the 4H series.

Everything else the source leaves undefined is a NAMED PARAMETER below and is
MY operationalization, not a disclosed rule. The sensitivity grid reports
each; none is tuned to a result. Nothing is changed after the run.

DATA
----
QQQ 5m RTH, category US_ETF, from reports/.webull_cache/QQQ_5m_RTH_frozen.json
(gitignored). Both access routes (SDK credentials, user-webull MCP) were
unavailable this pass, so the deepest reachable span is the already-frozen
panel 2026-01-29 -> 2026-10-14 (179 sessions). The intended ~2010 start was
NOT reachable; that boundary is a caveat, not a convenience. 4H structure is
resampled from this same 5m panel, so the clocks are guaranteed consistent.

Run:
    .venv/bin/python scripts/experiments/slc_qqq_differential.py
    .venv/bin/python scripts/experiments/slc_qqq_differential.py --grid   # add grids
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics as st
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from hyperliquid_bot.indicators import atr, stochastic  # noqa: E402

CACHE = ROOT / "reports" / ".webull_cache"
PANEL = CACHE / "QQQ_5m_RTH_frozen.json"

HOUR_MS = 3_600_000
H4_MS = 4 * HOUR_MS

# ---- locked ex ante (the three numbers the row freezes) --------------------
DISP_ATR_K = 1.5
DISP_MAX_BARS = 3
STOCH_K, STOCH_SMOOTH_K, STOCH_SMOOTH_D = 14, 3, 3
STOCH_UPPER, STOCH_LOWER = 80.0, 20.0
STRUCT_P = 3

# ---- my operationalizations (named, disclosed, not the source's rules) -----
ZONE_TOL_ATR = 0.0      # a touch is an intersection of [low,high] with the band
BREAK_TOL_ATR = 0.25    # far-edge "clean break" needs a close this far past it
MAX_BREAKS = 2          # a level broken twice is "chopped through" -> retired
STOP_TOL_ATR = 0.1      # "slightly beyond the zone"
CONF_WIN = 6            # bars after the touch in which the trigger may fire
CONF_CROSS = "kd"       # trigger = %K/%D cross after the band break ("band" alt)
H_BARS = 12             # headline forward horizon, in 5m bars (~60 min)
TARGET_R = 2.0          # source's stated target
ZONE_LIFETIME = 390     # bars; a level never retested within ~5 RTH sessions dies
SCATTER_FRAC = 0.70     # first 70% of calendar time
MIN_SESSIONS = 120      # rule 3 floor

# ---- cost (equity row convention from the Q6 addendum) ---------------------
COST_BP = 0.6           # 1 tick round trip on QQQ, zero commission
COST_BP_MATRIX = 15.0   # the matrix default, reported beside it


# ============================================================== data
@dataclass
class Panel:
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    day: np.ndarray           # integer session id (UTC date ordinal)
    atr: np.ndarray


def load_panel(path: Path) -> Panel:
    rows = json.loads(path.read_text())
    rows.sort(key=lambda r: r["time"])
    t, o, h, l, c, v, day = [], [], [], [], [], [], []
    for r in rows:
        ts = int(dt.datetime.strptime(r["time"], "%Y-%m-%dT%H:%M:%S.%f%z").timestamp() * 1000)
        t.append(ts)
        o.append(float(r["open"])); h.append(float(r["high"]))
        l.append(float(r["low"])); c.append(float(r["close"]))
        v.append(float(r["volume"]))
        day.append(dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).date().toordinal())
    t = np.array(t, dtype=np.int64)
    h = np.array(h); l = np.array(l); c = np.array(c)
    return Panel(t, np.array(o), h, l, c, np.array(v), np.array(day), atr(h, l, c, 14))


# ============================================================== structure (4H)
def resample_4h(p: Panel) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Fixed 4-hour UTC buckets. Returns (bucket_start_ms, o, h, l, c)."""
    bucket = p.t // H4_MS
    starts, o, h, l, c = [], [], [], [], []
    for b in np.unique(bucket):
        m = bucket == b
        starts.append(int(b * H4_MS))
        o.append(p.o[m][0]); h.append(p.h[m].max())
        l.append(p.l[m].min()); c.append(p.c[m][-1])
    return (np.array(starts, dtype=np.int64), np.array(o), np.array(h),
            np.array(l), np.array(c))


def structure_states(p: Panel, p_len: int) -> np.ndarray:
    """One trend state per 5m bar: 'up', 'down', or 'cons' (consolidation).

    Fractal swings of ``p_len`` bars each side on the resampled 4H series. A
    swing is only used once its ``p_len`` right-hand bars have closed, so the
    state never sees a future bar. Uptrend = last two swing highs rising AND
    last two swing lows rising; downtrend is the mirror; else consolidation.
    """
    bs, _o, bh, bl, _bc = resample_4h(p)
    n4 = len(bs)
    sh: list[tuple[int, float]] = []   # (confirm_index, price)
    sl: list[tuple[int, float]] = []
    for j in range(p_len, n4 - p_len):
        if bh[j] == bh[j - p_len:j + p_len + 1].max():
            sh.append((j + p_len, bh[j]))
        if bl[j] == bl[j - p_len:j + p_len + 1].min():
            sl.append((j + p_len, bl[j]))

    def state_at(k: int) -> str:
        highs = [v for ci, v in sh if ci <= k][-2:]
        lows = [v for ci, v in sl if ci <= k][-2:]
        if len(highs) == 2 and len(lows) == 2:
            if highs[1] > highs[0] and lows[1] > lows[0]:
                return "up"
            if highs[1] < highs[0] and lows[1] < lows[0]:
                return "down"
        return "cons"

    bucket_end = bs + H4_MS
    state4 = [state_at(k) for k in range(n4)]
    # map each 5m bar to the last 4H bucket that had fully closed by then
    idx = np.searchsorted(bucket_end, p.t, side="right") - 1
    out = np.empty(len(p.t), dtype=object)
    for i in range(len(p.t)):
        out[i] = state4[idx[i]] if idx[i] >= 0 else "cons"
    return out


# ============================================================== zones
@dataclass
class Zone:
    zid: int
    kind: str                 # 'demand' (up leg) or 'supply' (down leg)
    direction: int            # +1 long at demand, -1 short at supply
    lo: float
    hi: float
    born: int                 # first bar at which the zone may be traded
    breaks: int = 0
    tested_first: bool = False
    tested_retest: bool = False
    prev_inside: bool = False
    dead: bool = False


def build_zones(p: Panel, states: np.ndarray, disp_k: float, disp_max: int,
                max_breaks: int, lifetime: int) -> list[Zone]:
    """Detect displacement zones, one per aggressive move, structure-matched.

    The zone candle is the last candle BEFORE the move; its band is that
    candle's [low, high]. An up move makes a demand zone, a down move a supply
    zone. A zone survives only if its type matches the structure state at
    creation (demand in an uptrend, supply in a downtrend), per the source.
    """
    n = len(p.t)
    zones: list[Zone] = []
    zid = 0
    i = 1
    while i < n - 1:
        base = p.atr[i - 1]
        if not np.isfinite(base) or base <= 0:
            i += 1
            continue
        made = False
        for m in range(1, disp_max + 1):
            if i + m - 1 >= n:
                break
            net = p.c[i + m - 1] - p.o[i]
            if abs(net) >= disp_k * base:
                direction = 1 if net > 0 else -1
                kind = "demand" if direction == 1 else "supply"
                want = "up" if direction == 1 else "down"
                if states[i - 1] == want:
                    z = Zone(zid, kind, direction, p.l[i - 1], p.h[i - 1],
                             born=i + m)
                    zones.append(z)
                    zid += 1
                i += m                     # skip past the move
                made = True
                break
        if not made:
            i += 1
    return zones


# ============================================================== touches
@dataclass
class Touch:
    day: int
    bar: int
    zid: int
    kind: str
    direction: int
    subcase: str              # 'first_touch' or 'retest'
    confirmed: bool
    entry: float
    fwd_bp: float             # signed forward return in bp, cost subtracted
    # simulated 2R trade fields (context only)
    stop: float = 0.0
    target: float = 0.0
    exit_reason: str = ""
    net_R: float = 0.0
    R_abs: float = 0.0
    extras: dict = field(default_factory=dict)


def confirm_trigger(k_arr: np.ndarray, d: np.ndarray, bar: int, direction: int,
                    win: int, cross: str) -> bool:
    """Did the stochastic trigger fire within ``win`` bars of ``bar``?

    supply (short): %K breaks ABOVE the upper band, then crosses back down.
    demand (long):  %K breaks BELOW the lower band, then crosses back up.
    `cross='kd'`: the return leg is a %K/%D cross; `cross='band'`: the return
    leg is %K crossing back through the band. Both are plausible readings of
    the source's spoken rule; `kd` is the locked default.
    """
    n = len(k_arr)
    hi_b = min(n, bar + win)
    up = direction < 0
    broke = False
    for a in range(bar, hi_b):
        if not np.isfinite(k_arr[a]):
            continue
        if up and k_arr[a] > STOCH_UPPER:
            broke = True
        elif (not up) and k_arr[a] < STOCH_LOWER:
            broke = True
        if not broke or a + 1 >= hi_b:
            continue
        for b in range(a + 1, hi_b):
            if not (np.isfinite(k_arr[b]) and np.isfinite(k_arr[b - 1])
                    and np.isfinite(d[b]) and np.isfinite(d[b - 1])):
                continue
            if up and k_arr[b] < d[b] and k_arr[b - 1] >= d[b - 1]:
                return True                      # crossed back down
            if up and cross == "band" and k_arr[b] < STOCH_UPPER <= k_arr[b - 1]:
                return True
            if (not up) and k_arr[b] > d[b] and k_arr[b - 1] <= d[b - 1]:
                return True
            if (not up) and cross == "band" and k_arr[b] > STOCH_LOWER >= k_arr[b - 1]:
                return True
    return False


def simulate_trade(p: Panel, entry_idx: int, direction: int, stop: float,
                   target: float, cost_bp: float) -> tuple[str, float, float]:
    """Walk bars from entry; ambiguous bars resolve to the stop. Returns
    (exit_reason, net_R, R_abs)."""
    n = len(p.t)
    entry = p.o[entry_idx]
    R = abs(entry - stop)
    if R <= 0:
        return ("bad_geometry", 0.0, 0.0)
    k = entry_idx
    while k < n:
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
        k += 1
    else:
        exit_px, reason = p.c[n - 1], "eod"
    gross = (exit_px - entry) * direction
    net = gross - entry * cost_bp / 1e4
    return (reason, net / R, R)


def build_touches(p: Panel, states: np.ndarray, k_line: np.ndarray,
                  d_line: np.ndarray, *, disp_k: float, disp_max: int,
                  max_breaks: int, lifetime: int, conf_win: int,
                  cross: str, h_bars: int, cost_bp: float) -> list[Touch]:
    """Build the fixed zone set, then record every touch and its forward
    return. Zone detection, confirmation and the return measurement are kept
    separate so the differential logic is auditable."""
    n = len(p.t)
    zones = build_zones(p, states, disp_k, disp_max, max_breaks, lifetime)
    zones.sort(key=lambda z: z.born)
    touches: list[Touch] = []
    active: list[Zone] = []
    zptr = 0

    for i in range(n):
        while zptr < len(zones) and zones[zptr].born <= i:
            active.append(zones[zptr])
            zptr += 1
        still: list[Zone] = []
        for z in active:
            if z.dead or i > z.born + lifetime:
                continue
            inside = not (p.l[i] > z.hi or p.h[i] < z.lo)
            # --- far-edge break (a close beyond the band by tolerance) ---
            tol = BREAK_TOL_ATR * p.atr[i] if np.isfinite(p.atr[i]) else 0.0
            if z.direction == 1:
                broke = p.c[i] < z.lo - tol
            else:
                broke = p.c[i] > z.hi + tol
            if broke:
                z.breaks += 1
                if z.breaks >= max_breaks:
                    z.prev_inside = inside
                    continue                     # chopped through -> retired
            # --- touch (new excursion only) ---
            new_touch = inside and not z.prev_inside
            if new_touch:
                if (not z.tested_first):
                    sub = "first_touch"
                    z.tested_first = True
                    record = True
                elif z.breaks >= 1 and not z.tested_retest:
                    sub = "retest"
                    z.tested_retest = True
                    record = True
                else:
                    record = False
                if record:
                    t = measure_touch(p, i, z, sub, k_line, d_line,
                                      conf_win, cross, h_bars, cost_bp)
                    if t is not None:
                        touches.append(t)
            z.prev_inside = inside
            still.append(z)
        active = still
    return touches


def measure_touch(p: Panel, bar: int, z: Zone, sub: str, k_line: np.ndarray,
                  d_line: np.ndarray, conf_win: int, cross: str, h_bars: int,
                  cost_bp: float) -> Touch | None:
    entry_idx = bar + 1
    fwd_idx = entry_idx + h_bars
    if entry_idx >= len(p.t) or fwd_idx >= len(p.t):
        return None
    entry = p.o[entry_idx]
    fwd = p.c[fwd_idx]
    signed_bp = z.direction * (fwd - entry) / entry * 1e4 - cost_bp
    confirmed = confirm_trigger(k_line, d_line, bar, z.direction, conf_win, cross)
    tol = STOP_TOL_ATR * p.atr[bar] if np.isfinite(p.atr[bar]) else 0.0
    stop = (z.lo - tol) if z.direction == 1 else (z.hi + tol)
    R = abs(entry - stop)
    target = entry + TARGET_R * R * z.direction
    reason, net_R, R_abs = simulate_trade(p, entry_idx, z.direction, stop, target,
                                          cost_bp)
    return Touch(p.day[bar], bar, z.zid, z.kind, z.direction, sub, confirmed,
                 entry, signed_bp, stop, target, reason, net_R, R_abs)


# ============================================================== statistics
def cluster_t(x: np.ndarray, g: np.ndarray, day: np.ndarray) -> tuple[float, float]:
    """Difference in means (g==1 minus g==0) with a day-clustered t.

    Cluster-robust variance for a two-sample difference: each observation's
    influence on the estimator is summed inside its session, and the variance
    is the sum of squared cluster sums. Touches inside a session are not
    independent, so the unit is one session, per the matrix protocol.
    """
    x1, x0 = x[g == 1], x[g == 0]
    if len(x1) == 0 or len(x0) == 0:
        return (float("nan"), float("nan"))
    m1, m0 = x1.mean(), x0.mean()
    beta = m1 - m0
    infl = np.where(g == 1, (x - m1) / len(x1), -(x - m0) / len(x0))
    clusters = np.unique(day)
    sums = np.array([infl[day == c].sum() for c in clusters])
    G = len(clusters)
    if G < 2:
        return (beta, float("nan"))
    var = (G / (G - 1)) * float((sums ** 2).sum())
    se = float(np.sqrt(var))
    return (beta, beta / se if se > 0 else float("nan"))


def describe_group(x: np.ndarray, label: str) -> dict:
    if len(x) == 0:
        return {"label": label, "n": 0}
    q = np.percentile(x, [10, 25, 50, 75, 90])
    return {"label": label, "n": len(x), "mean": float(x.mean()),
            "median": float(np.median(x)), "sd": float(x.std(ddof=1)) if len(x) > 1 else 0.0,
            "p10": q[0], "p25": q[1], "p50": q[2], "p75": q[3], "p90": q[4]}


def differential(touches: list[Touch], cost_bp: float) -> dict:
    x = np.array([t.fwd_bp for t in touches])
    g = np.array([1 if t.confirmed else 0 for t in touches])
    day = np.array([t.day for t in touches])
    beta, tcl = cluster_t(x, g, day)
    conf = x[g == 1] if len(x) else np.array([])
    unconf = x[g == 0] if len(x) else np.array([])
    return {"n": len(touches), "n_conf": int(g.sum()), "n_unconf": int(len(g) - g.sum()),
            "gap": beta, "t_cl": tcl,
            "conf": describe_group(conf, "confirmed"),
            "unconf": describe_group(unconf, "unconfirmed")}


# ============================================================== reporting
def pct(x: float) -> str:
    return f"{x*100:.1f}%"


def show_differential(d: dict, label: str, quiet: bool = False) -> None:
    if not d["n"]:
        print(f"  {label}: no touches")
        return
    if not quiet:
        print(f"  {label}: touches={d['n']}  confirmed={d['n_conf']}  "
              f"unconfirmed={d['n_unconf']}  ({d['n_conf']/d['n']:.1%} confirmed)")
    print(f"    gap (conf - unconf) = {d['gap']:+.2f} bp   day-clustered t = {d['t_cl']:+.2f}")
    for grp in ("conf", "unconf"):
        s = d[grp]
        if s["n"]:
            print(f"      {s['label']:11s} n={s['n']:>4}  mean={s['mean']:+7.2f}  "
                  f"med={s['median']:+7.2f}  sd={s['sd']:6.2f}  "
                  f"deciles[{s['p10']:+.0f},{s['p25']:+.0f},{s['p50']:+.0f},"
                  f"{s['p75']:+.0f},{s['p90']:+.0f}]")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", type=Path, default=PANEL)
    ap.add_argument("--grid", action="store_true",
                    help="also print the full sensitivity grid")
    args = ap.parse_args()

    if not args.panel.exists():
        raise SystemExit(f"panel missing: {args.panel}")
    p = load_panel(args.panel)
    states = structure_states(p, STRUCT_P)
    k_line, d_line = stochastic(p.h, p.l, p.c, STOCH_K, STOCH_SMOOTH_K, STOCH_SMOOTH_D)

    n = len(p.t)
    sessions = len(np.unique(p.day))
    t0 = dt.datetime.fromtimestamp(p.t[0] / 1000, dt.timezone.utc)
    t1 = dt.datetime.fromtimestamp(p.t[-1] / 1000, dt.timezone.utc)

    print("=" * 82)
    print("Q8  SLC differential test  (QQQ 5m RTH, Webull frozen panel)")
    print("=" * 82)
    print(f"panel        : {len(p.t):,} bars  {t0:%Y-%m-%d} -> {t1:%Y-%m-%d}  "
          f"({sessions} sessions)")
    print(f"clears floor : {'YES' if sessions >= MIN_SESSIONS else 'NO'} "
          f"(rule 3 wants >= {MIN_SESSIONS} sessions)")
    print(f"locked       : disp {DISP_ATR_K}xATR over <= {DISP_MAX_BARS} bars  |  "
          f"stoch {STOCH_K},{STOCH_SMOOTH_K},{STOCH_SMOOTH_D} bands {STOCH_UPPER:.0f}/"
          f"{STOCH_LOWER:.0f}  |  structure fractal p={STRUCT_P} on 4H")

    # ---- the fixed zone set and the touch split --------------------------
    touches = build_touches(p, states, k_line, d_line,
                            disp_k=DISP_ATR_K, disp_max=DISP_MAX_BARS,
                            max_breaks=MAX_BREAKS, lifetime=ZONE_LIFETIME,
                            conf_win=CONF_WIN, cross=CONF_CROSS,
                            h_bars=H_BARS, cost_bp=COST_BP)
    zones = build_zones(p, states, DISP_ATR_K, DISP_MAX_BARS, MAX_BREAKS,
                        ZONE_LIFETIME)
    n_first = sum(1 for t in touches if t.subcase == "first_touch")
    n_retest = sum(1 for t in touches if t.subcase == "retest")

    print()
    print("THE FIXED ZONE SET")
    print(f"  displacement zones detected (structure-matched) : {len(zones)}")
    print(f"  touches recorded                                : {len(touches)}"
          f"  (first_touch {n_first} / retest {n_retest})")

    # ---- headline: first 70% of calendar time ----------------------------
    cut_ms = p.t[0] + SCATTER_FRAC * (p.t[-1] - p.t[0])
    sc = [t for t in touches if p.t[t.bar] <= cut_ms]
    ho = [t for t in touches if p.t[t.bar] > cut_ms]
    cut_date = dt.datetime.fromtimestamp(cut_ms / 1000, dt.timezone.utc).date()

    print()
    print("=" * 82)
    print(f"HEADLINE (first 70% of calendar time, up to {cut_date})")
    print(f"  horizon H = {H_BARS} bars (~{H_BARS*5} min), entry next bar open, "
          f"cost {COST_BP} bp")
    print("=" * 82)
    d_sc = differential(sc, COST_BP)
    show_differential(d_sc, "scatter 70%")

    # ---- kill rule -------------------------------------------------------
    passed = (np.isfinite(d_sc["t_cl"]) and d_sc["t_cl"] >= 2.0
              and d_sc["gap"] > 0)
    gap_bp_ratio = d_sc["gap"] / COST_BP if COST_BP else float("nan")
    print()
    print("KILL RULE: pass requires day-clustered t >= 2 AND gap > 0 on the 70%.")
    print(f"  gap {d_sc['gap']:+.2f} bp  = {gap_bp_ratio:+.2f}x the round-trip cost")
    print(f"  verdict: {'CLEARS -> walk-forward may open' if passed else 'FAILS -> last 30% stays SEALED'}")

    # ---- walk-forward, only if the 70% cleared ---------------------------
    print()
    print("=" * 82)
    if passed:
        print(f"WALK-FORWARD (last 30%, after {cut_date}) -- opened once")
        print("=" * 82)
        d_ho = differential(ho, COST_BP)
        show_differential(d_ho, "holdout 30%")
        same = (d_ho["gap"] > 0) == (d_sc["gap"] > 0)
        print(f"  same sign as scatter: {same}")
    else:
        print("WALK-FORWARD: NOT OPENED. The first 70% failed its kill rule,")
        print("so the last 30% of calendar time stays sealed (matrix rule 5).")
        print("No holdout statistic is computed or printed.")

    # ---- simulated 2R trades (context only) ------------------------------
    print()
    print("=" * 82)
    print("CONTEXT ONLY -- simulated 2R/stop trades (not the headline)")
    print("=" * 82)
    for grp, name in ((True, "confirmed"), (False, "unconfirmed")):
        sub = [t for t in sc if t.confirmed == grp and t.exit_reason != "bad_geometry"]
        if not sub:
            print(f"  {name:11s}: none in the 70%")
            continue
        rs = [t.net_R for t in sub]
        wins = sum(1 for r in rs if r > 0)
        reasons = {}
        for t in sub:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        print(f"  {name:11s}: n={len(sub):>4}  meanR={np.mean(rs):+.3f}  "
              f"midR={np.median(rs):+.3f}  win={wins/len(sub):.1%}  "
              f"meanRsize={np.mean([t.R_abs for t in sub]):.2f}")
        print(f"    exits: {dict(sorted(reasons.items()))}")

    # ---- sensitivity -----------------------------------------------------
    if args.grid:
        print()
        print("=" * 82)
        print("SENSITIVITY GRID -- each cell re-runs the whole thing on the 70%.")
        print("Reported for transparency; NOT a search for the best cell.")
        print("=" * 82)

        def cell(**kw):
            base = dict(disp_k=DISP_ATR_K, disp_max=DISP_MAX_BARS,
                        max_breaks=MAX_BREAKS, lifetime=ZONE_LIFETIME,
                        conf_win=CONF_WIN, cross=CONF_CROSS, h_bars=H_BARS,
                        cost_bp=COST_BP)
            base.update(kw)
            tk, dk = stochastic(p.h, p.l, p.c, base.pop("sk", STOCH_K),
                                base.pop("ssk", STOCH_SMOOTH_K),
                                base.pop("ssd", STOCH_SMOOTH_D))
            tt = build_touches(p, states, tk, dk, **base)
            sub = [t for t in tt if p.t[t.bar] <= cut_ms]
            return differential(sub, COST_BP)

        grids = [
            ("displacement k (x ATR)", [{"disp_k": v} for v in (1.0, 1.5, 2.0, 2.5)]),
            ("displacement window (bars)", [{"disp_max": v} for v in (2, 3, 4)]),
            ("structure fractal p", None),     # handled separately (states change)
            ("stochastic settings", [{"sk": a, "ssk": b, "ssd": c}
                                     for (a, b, c) in ((14, 3, 3), (9, 3, 3),
                                                       (21, 3, 3), (14, 1, 3))]),
            ("confirmation window (bars)", [{"conf_win": v} for v in (3, 6, 12)]),
            ("confirmation cross type", [{"cross": v} for v in ("kd", "band")]),
            ("forward horizon H (bars)", [{"h_bars": v} for v in (6, 12, 24)]),
            ("zone lifetime (bars)", [{"lifetime": v} for v in (130, 390, 10**9)]),
            ("max breaks before retired", [{"max_breaks": v} for v in (1, 2, 3)]),
        ]
        for name, variants in grids:
            print(f"\n  -- {name} --")
            if variants is None:
                for pp in (2, 3, 4):
                    st_pp = structure_states(p, pp)
                    tk, dk = stochastic(p.h, p.l, p.c, STOCH_K, STOCH_SMOOTH_K,
                                        STOCH_SMOOTH_D)
                    tt = build_touches(p, st_pp, tk, dk,
                                       disp_k=DISP_ATR_K, disp_max=DISP_MAX_BARS,
                                       max_breaks=MAX_BREAKS, lifetime=ZONE_LIFETIME,
                                       conf_win=CONF_WIN, cross=CONF_CROSS,
                                       h_bars=H_BARS, cost_bp=COST_BP)
                    sub = [t for t in tt if p.t[t.bar] <= cut_ms]
                    dd = differential(sub, COST_BP)
                    print(f"    p={pp}: n={dd['n']:>4} conf={dd['n_conf']:>4} "
                          f"gap={dd['gap']:+7.2f} bp  t={dd['t_cl']:+.2f}")
                continue
            for variant in variants:
                dd = cell(**variant)
                tag = " ".join(f"{k}={v}" for k, v in variant.items())
                print(f"    {tag:28s} n={dd['n']:>4} conf={dd['n_conf']:>4} "
                      f"gap={dd['gap']:+7.2f} bp  t={dd['t_cl']:+.2f}")

    print()
    print("Nothing here is a live order, a sizing change, or a claim the source")
    print("makes money. The differential tests the confirmation filter's")
    print("selectivity on one proxy instrument over one frozen span.")


if __name__ == "__main__":
    main()
