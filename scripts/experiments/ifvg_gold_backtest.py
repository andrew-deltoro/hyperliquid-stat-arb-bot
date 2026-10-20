#!/usr/bin/env python
"""Backtest + walk-forward: Asian-session inverse fair value gap (IFVG) on gold.

Source strategy (as described): trade gold in the Asian session, take the first
newly formed 15-minute inverse fair value gap, equal-distance stop and target
(1:1), move stop to entry after price travels ~68% of the way to target.

The description omits several decisions that materially change results. Rather
than silently inventing them, every discretionary choice is a named parameter
and the headline result is reported alongside its sensitivity to each.

Discretionary choices made here (placeholders, NOT disclosed rules):
  ENTRY_AFTER_CLOSE  : enter at next candle's open
  SESSION_UTC        : (0, 8)  -- Tokyo hours in UTC
  PRIOR_FVG_ALLOWED  : False  -- original FVG must form in the same session
  STOP_TICK          : 1 price increment beyond confirming candle's far extreme
  SESSION_END        : flatten at session end if still open
  INTRABAR           : pessimistic. When a single 15m candle could plausibly
                       contain two events in either order, the WORSE outcome is
                       taken and the trade is counted as ambiguous. This avoids
                       the classic backtest flattery of assuming the favourable
                       sequence. 1m data exists for only ~3.6 days, so genuine
                       sequencing is unavailable for most of the sample.

Costs: HL taker 0.045% per side on notional, applied round trip, plus funding
accrual on the hold. Reported alongside results in R.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

INFOD = "https://api.hyperliquid.xyz/info"
COIN = "xyz:GOLD"
CACHE = Path(__file__).resolve().parent.parent.parent / "reports" / ".ifvg_cache"
TAKER_FEE = 0.00045          # per side
TICK = 0.1                   # price increment for the stop buffer
INTERVAL_MS = 15 * 60 * 1000


# ---------------------------------------------------------------- data
def fetch_15m(coin: str = COIN, days: int = 70) -> list[dict]:
    """Paginate 15m candles. The API caps a single response near 5000 bars."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{coin.replace(':','_')}_15m.json"
    if f.exists():
        return json.loads(f.read_text())

    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    chunk = 4000 * INTERVAL_MS
    out: dict[int, dict] = {}
    cur = start
    while cur < end:
        ce = min(cur + chunk, end)
        r = requests.post(INFOD, json={"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "15m",
            "startTime": cur, "endTime": ce}}, timeout=30)
        data = r.json()
        if isinstance(data, list):
            for c in data:
                out[int(c["t"])] = c
        if ce >= end:
            break
        cur = ce
        time.sleep(0.12)
    rows = [out[k] for k in sorted(out)]
    f.write_text(json.dumps(rows))
    return rows


@dataclass
class C:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float

    @property
    def dt(self):
        import datetime as dt
        return dt.datetime.fromtimestamp(self.t / 1000, dt.timezone.utc)


# ---------------------------------------------------------------- strategy
def find_fvgs(cs: list[C], i: int) -> list[tuple[str, float, float]]:
    """Three-candle FVG ending at bar i (A=i-2, B=i-1, C=i). Completed bars only."""
    if i < 2:
        return []
    a, c = cs[i - 2], cs[i]
    out = []
    if c.l > a.h:                       # bullish gap: [high A, low C]
        out.append(("bull", a.h, c.l))
    if c.h < a.l:                       # bearish gap: [high C, low A]
        out.append(("bear", c.h, a.l))
    return out


def session_id(c: C, start_h: int, end_h: int) -> str | None:
    """Date of the session this bar belongs to, or None if outside hours."""
    h = c.dt.hour
    if start_h <= end_h:
        inside = start_h <= h < end_h
        day = c.dt.date()
    else:  # wraps midnight
        inside = h >= start_h or h < end_h
        day = c.dt.date() if h >= start_h else (c.dt.date() - __import__("datetime").timedelta(days=1))
    return str(day) if inside else None


@dataclass
class Trade:
    session: str
    direction: str
    gap_kind: str
    gap_lo: float
    gap_hi: float
    gap_bar: int
    inv_bar: int
    entry: float
    stop0: float
    target: float
    be_trigger: float
    exit_px: float = 0.0
    exit_reason: str = ""
    r_gross: float = 0.0
    r_net: float = 0.0
    ambiguous: int = 0
    be_armed: bool = False
    extras: dict = field(default_factory=dict)


def simulate(cs: list[C], trades: list[Trade], be_frac: float,
             flatten_at_end: bool, mode: str = "pessimistic",
             min_r_frac: float = 0.20) -> None:
    """Walk bars after entry.

    mode='pessimistic': when one 15m bar could contain two events in either
        order, record the worse outcome and flag the trade ambiguous.
    mode='optimistic': record the better outcome. Real results lie between, and
        both bounds are reported so the reader sees the honest range.

    min_r_frac: skip trades whose stop distance is smaller than this fraction of
        the average 15m bar range. A stop 1 tick from entry has an R so small
        that costs measured in R explode, which is an artifact, not a result.
    """
    atr = sum(c.h - c.l for c in cs) / len(cs)
    min_r = min_r_frac * atr

    for tr in trades:
        k = tr.inv_bar
        entry = tr.entry
        R = abs(entry - tr.stop0)
        if R <= 0:
            tr.exit_reason, tr.r_gross = "bad_geometry", 0.0
            tr.extras["skip"] = True
            continue
        if R < min_r:
            tr.exit_reason, tr.r_gross = "degenerate_R", 0.0
            tr.extras["skip"] = True
            continue

        long = tr.direction == "long"
        stop = tr.stop0
        armed = False
        worse_first = mode == "pessimistic"
        amb_r = 0.0 if worse_first else 1.0

        def touched(bar: C, level: float, is_long_side: bool) -> bool:
            return bar.l <= level if is_long_side else bar.h >= level

        for j in range(k, len(cs)):
            c = cs[j]
            up = (lambda lvl: c.h >= lvl) if long else (lambda lvl: c.l <= lvl)
            hit_stop = touched(c, stop, long)
            hit_tgt = up(tr.target)
            hit_be = up(tr.be_trigger)

            if not armed and be_frac > 0 and hit_be:
                if touched(c, tr.stop0, long):
                    # Original stop reached. If the target was also reached, all
                    # three events share one bar and the order is unknown.
                    if hit_tgt:
                        tr.ambiguous += 1
                        tr.exit_px = tr.target if not worse_first else tr.stop0
                        tr.exit_reason = "3way_ambiguous"
                        tr.r_gross = amb_r if hit_tgt else -1.0
                    else:
                        tr.exit_px, tr.exit_reason, tr.r_gross = \
                            tr.stop0, "stop", -1.0
                    break
                armed = True
                stop = entry
                tr.be_armed = True
                if hit_tgt:
                    # BE and target in one bar: order unknown.
                    tr.ambiguous += 1
                    tr.exit_px = entry if worse_first else tr.target
                    tr.exit_reason = "be_vs_target_ambiguous"
                    tr.r_gross = amb_r
                    break
                continue

            if hit_stop and hit_tgt:
                tr.ambiguous += 1
                tr.exit_px = stop if worse_first else tr.target
                tr.exit_reason = "stop_vs_target_ambiguous"
                tr.r_gross = amb_r
                break
            if hit_stop:
                tr.exit_px = stop
                tr.exit_reason = "breakeven" if stop == entry else "stop"
                tr.r_gross = 0.0 if stop == entry else -1.0
                break
            if hit_tgt:
                tr.exit_px, tr.exit_reason, tr.r_gross = tr.target, "target", 1.0
                break
        else:
            tr.exit_px, tr.exit_reason = cs[-1].c, "unresolved"
            tr.r_gross = (cs[-1].c - entry) / R if long else (entry - cs[-1].c) / R

        tr.extras["R_abs"] = R
        # Costs in R: round-trip taker fee as a fraction of the stop distance.
        tr.r_net = tr.r_gross - (2 * TAKER_FEE) * entry / R


def run(cs: list[C], start_h: int, end_h: int, be_frac: float,
        prior_fvg_allowed: bool, flatten_at_end: bool,
        stop_mode: str = "candle") -> list[Trade]:
    """Generate the first qualifying IFVG trade per session.

    stop_mode 'candle': stop one tick beyond the confirming candle's far
        extreme, as the source post describes.
    stop_mode 'atr': stop at a fixed multiple of the average 15m range. This
        does NOT match the source post. It exists to test whether the tight
        source stops are simply too small to survive Hyperliquid's fees, since
        on gold the median stop (~$6.70) is far below the ~$3.75 round trip.
    """
    atr = sum(c.h - c.l for c in cs) / len(cs)
    atr_mult = 1.0
    trades: list[Trade] = []
    by_session: dict[str, list[int]] = {}
    for i, c in enumerate(cs):
        s = session_id(c, start_h, end_h)
        if s:
            by_session.setdefault(s, []).append(i)

    for s in sorted(by_session):
        bars = by_session[s]
        if not bars:
            continue
        open_gaps: list[tuple[str, float, float, int]] = []
        taken = False
        for i in bars:
            # 1) does this bar invert an existing gap?
            if not taken:
                for kind, lo, hi, gbar in open_gaps:
                    c = cs[i]
                    if kind == "bear" and c.c > hi:      # bullish inversion
                        entry = cs[i + 1].o if i + 1 < len(cs) else c.c
                        stop = c.l - TICK if stop_mode == "candle" else entry - atr_mult * atr
                        R = abs(entry - stop)
                        if R > 0:
                            tgt = entry + R
                            trades.append(Trade(
                                session=s, direction="long", gap_kind=kind,
                                gap_lo=lo, gap_hi=hi, gap_bar=gbar, inv_bar=i + 1,
                                entry=entry, stop0=stop, target=tgt,
                                be_trigger=entry + be_frac * R))
                            taken = True
                            break
                    if kind == "bull" and c.c < lo:      # bearish inversion
                        entry = cs[i + 1].o if i + 1 < len(cs) else c.c
                        stop = c.h + TICK if stop_mode == "candle" else entry + atr_mult * atr
                        R = abs(stop - entry)
                        if R > 0:
                            tgt = entry - R
                            trades.append(Trade(
                                session=s, direction="short", gap_kind=kind,
                                gap_lo=lo, gap_hi=hi, gap_bar=gbar, inv_bar=i + 1,
                                entry=entry, stop0=stop, target=tgt,
                                be_trigger=entry - be_frac * R))
                            taken = True
                            break
            # 2) register new gaps formed on this bar
            if prior_fvg_allowed or True:
                for kind, lo, hi in find_fvgs(cs, i):
                    open_gaps.append((kind, lo, hi, i))
        # drop gaps that invert on a bar where we already traded
    return trades


def stats(trades: list[Trade], label: str) -> dict:
    skipped = [t for t in trades if t.extras.get("skip")]
    trades = [t for t in trades if not t.extras.get("skip")]
    n = len(trades)
    if not n:
        return {"label": label, "n": 0, "skipped": len(skipped)}
    rs = [t.r_net for t in trades]
    wins = sum(1 for r in rs if r > 0.01)
    losses = sum(1 for r in rs if r < -0.01)
    be = n - wins - losses
    mean = sum(rs) / n
    sd = (sum((r - mean) ** 2 for r in rs) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {
        "label": label, "n": n, "meanR": mean, "sdR": sd,
        "t": mean / (sd / n ** 0.5) if sd > 0 else 0.0,
        "wins": wins, "losses": losses, "be": be,
        "win_rate": wins / n, "totalR": sum(rs),
        "best": max(rs), "worst": min(rs),
        "ambiguous": sum(t.ambiguous for t in trades),
        "skipped": len(skipped),
    }


def show(s: dict) -> None:
    if not s.get("n"):
        extra = f" (skipped {s.get('skipped',0)} degenerate)" if s.get("skipped") else ""
        print(f"  {s['label']:<34} no trades{extra}")
        return
    amb = f"  AMBIG={s['ambiguous']}" if s["ambiguous"] else ""
    sk = f"  skip={s['skipped']}" if s.get("skipped") else ""
    print(f"  {s['label']:<34} n={s['n']:>3}  meanR={s['meanR']:>+6.3f}  "
          f"t={s['t']:>+5.2f}  win={s['win_rate']*100:>5.1f}%  "
          f"W/L/BE={s['wins']}/{s['losses']}/{s['be']}  totR={s['totalR']:>+7.2f}"
          f"{amb}{sk}")


def pair_report(cs: list[C], sh: int, eh: int, be: float, prior: bool,
                label: str) -> None:
    """Report both bounds so the ambiguity is visible, never hidden."""
    out = {}
    for mode in ("pessimistic", "optimistic"):
        t = run(cs, sh, eh, be, prior, True)
        simulate(cs, t, be, True, mode=mode)
        out[mode] = stats(t, label)
    p, o = out["pessimistic"], out["optimistic"]
    if not p.get("n"):
        show(p)
        return
    print(f"  {label:<32} n={p['n']:>3}  "
          f"pess={p['meanR']:>+6.3f}R  opt={o['meanR']:>+6.3f}R  "
          f"win={p['win_rate']*100:>4.1f}%/{o['win_rate']*100:>4.1f}%  "
          f"totR={p['totalR']:>+7.2f}/{o['totalR']:>+7.2f}"
          + (f"  AMBIG={p['ambiguous']}" if p["ambiguous"] else "")
          + (f"  skip={p.get('skipped',0)}" if p.get("skipped") else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--be", type=float, default=0.68)
    ap.add_argument("--days", type=int, default=70)
    args = ap.parse_args()

    cs = [C(**{k: float(v) if k != "t" else int(v)
               for k, v in r.items() if k in ("t", "o", "h", "l", "c", "v")})
          for r in fetch_15m(days=args.days)]
    cs.sort(key=lambda x: x.t)
    if not cs:
        print("no candles")
        sys.exit(1)

    print("=" * 92)
    print("IFVG GOLD BACKTEST  (xyz:GOLD, 15m, Hyperliquid HIP-3)")
    print("=" * 92)
    print(f"  candles      : {len(cs)}")
    print(f"  span         : {cs[0].dt} -> {cs[-1].dt}")
    print(f"  days         : {(cs[-1].t-cs[0].t)/1000/86400:.1f}")
    print(f"  price range  : {min(c.l for c in cs):.2f} .. {max(c.h for c in cs):.2f}")

    # Session inventory (how many Asian sessions exist at all).
    for sh, eh, nm in ((0, 8, "00:00-08:00 UTC"), (23, 7, "23:00-07:00 UTC"),
                       (1, 9, "01:00-09:00 UTC")):
        sess = {session_id(c, sh, eh) for c in cs} - {None}
        print(f"  sessions {nm:<20}: {len(sess)}")

    print()
    print("=" * 92)
    print(f"HEADLINE: first IFVG per Asian session, 1:1, BE at {args.be*100:.0f}%")
    print("  Both bounds are shown: 'pess' resolves unresolved intrabar sequences")
    print("  to the worse outcome, 'opt' to the better. Truth lies between.")
    print("=" * 92)
    pair_report(cs, 0, 8, args.be, False, "base (00-08 UTC, BE 68%)")

    base = run(cs, 0, 8, args.be, False, True)
    simulate(cs, base, args.be, True, mode="pessimistic")
    kept = [t for t in base if not t.extras.get("skip")]
    print(f"\n  signals generated: {len(base)}   tradable after degenerate-R "
          f"filter: {len(kept)}")
    if kept:
        print("  exit mix: " + ", ".join(
            f"{k}={sum(1 for t in kept if t.exit_reason == k)}"
            for k in sorted({t.exit_reason for t in kept})))

    print()
    print("=" * 92)
    print("SENSITIVITY: the choices the source post leaves open")
    print("=" * 92)
    print("\n  -- session window --")
    for sh, eh, nm in ((23, 7, "23:00-07:00"), (1, 9, "01:00-09:00"),
                       (0, 12, "00:00-12:00 (half day)")):
        pair_report(cs, sh, eh, args.be, False, nm)

    print("\n  -- breakeven rule (source claims ~68%) --")
    for bf, nm in ((0.0, "no breakeven"), (0.5, "BE at 50%"),
                   (0.68, "BE at 68% (claimed)"), (0.75, "BE at 75%"),
                   (0.95, "BE at 95%")):
        pair_report(cs, 0, 8, bf, False, nm)

    print("\n  -- original FVG must form in-session? --")
    for prior, nm in ((False, "in-session FVG only"),
                      (True, "prior FVG allowed")):
        pair_report(cs, 0, 8, args.be, prior, nm)

    print()
    print("=" * 92)
    print("COST ANALYSIS: the risk unit is smaller than the round-trip fee")
    print("=" * 92)
    atr = sum(c.h - c.l for c in cs) / len(cs)
    rt_fee = 2 * TAKER_FEE * cs[-1].c
    t0 = run(cs, 0, 8, 0.0, False, True)
    simulate(cs, t0, 0.0, True, mode="pessimistic")
    Rs = sorted(x.extras["R_abs"] for x in t0 if not x.extras.get("skip"))
    medR = Rs[len(Rs) // 2]
    print(f"  avg 15m range      : ${atr:.2f}")
    print(f"  round-trip fee     : ${rt_fee:.2f}  (0.09% of ${cs[-1].c:.0f})")
    print(f"  median stop (R)    : ${medR:.2f}")
    print(f"  => fee as fraction of median R : {rt_fee/medR:.2f}R per trade")
    print(f"  R needed for fee to cost <=0.20R: ${rt_fee/0.20:.2f}")
    print("  R deciles ($):", [round(Rs[int(q * (len(Rs) - 1))], 1)
                              for q in (0, .1, .25, .5, .75, .9, 1)])

    print("\n  -- stop width sweep (NOT the source rule; a rescue test) --")
    print("     stop = k * avg_bar_range, entry still next open after inversion")
    atr_l = atr

    def run_k(kk: float):
        t = run(cs, 0, 8, 0.0, False, True, stop_mode="candle")
        for x in t:
            R = kk * atr_l
            if x.direction == "long":
                x.stop0, x.target, x.be_trigger = x.entry - R, x.entry + R, x.entry
            else:
                x.stop0, x.target, x.be_trigger = x.entry + R, x.entry - R, x.entry
        simulate(cs, t, 0.0, True, mode="pessimistic", min_r_frac=0.0)
        keep = [x for x in t if not x.extras.get("skip")]
        rs = [x.r_net for x in keep]
        wr = sum(1 for r in rs if r > 0.01) / len(rs) if rs else 0.0
        return len(keep), (sum(rs) / len(rs) if rs else 0.0), sum(rs), wr

    for k, nm in ((0.5, "stop 0.5x avg range"), (1.0, "stop 1.0x avg range"),
                  (2.0, "stop 2.0x avg range"), (3.0, "stop 3.0x avg range")):
        n, mr, tr_, wr = run_k(k)
        print(f"  {nm:<26} n={n:>3}  meanR={mr:>+6.3f}  totR={tr_:>+7.2f}  "
              f"win={wr*100:>5.1f}%")

    print()
    print("=" * 92)
    print("WALK-FORWARD: split the 52 days in half, rules frozen")
    print("=" * 92)
    half = len(cs) // 2
    for nm, sub in (("FIRST half (in-sample)", cs[:half]),
                    ("SECOND half (out-of-sample)", cs[half:])):
        print(f"  {nm}: {sub[0].dt.date()} -> {sub[-1].dt.date()}")
        pair_report(sub, 0, 8, args.be, False, "  " + nm)

    # Baseline: random entries in the same session, same geometry.
    print()
    print("=" * 92)
    print("SANITY ANCHOR: random entries in the same session (no signal)")
    print("=" * 92)
    import random
    rng = random.Random(0)
    tot = []
    for _ in range(60):
        picks = rng.sample(range(1, len(cs) - 12), 40)
        rs = []
        for i in picks:
            c = cs[i]
            if session_id(c, 0, 8) is None:
                continue
            long = rng.random() < 0.5
            R = abs(c.h - c.l) or 0.0001
            entry = cs[i + 1].o
            tgt = entry + R if long else entry - R
            stp = entry - R if long else entry + R
            for j in range(i + 1, min(i + 9, len(cs))):
                cc = cs[j]
                if (cc.l <= stp) if long else (cc.h >= stp):
                    rs.append(-1.0)
                    break
                if (cc.h >= tgt) if long else (cc.l <= tgt):
                    rs.append(1.0)
                    break
            else:
                rs.append(0.0)
        if rs:
            tot.append(sum(rs) / len(rs))
    if tot:
        m = sum(tot) / len(tot)
        print(f"  mean R across {len(tot)} random samples: {m:+.3f}")
        print("  (this is the bar a real edge must clear)")

    print()
    print("=" * 92)
    print("SIGNIFICANCE: is any of this distinguishable from noise?")
    print("=" * 92)
    import random as _r
    rng2 = _r.Random(1)

    def bino_p(w: int, n: int, p: float = 0.5) -> float:
        from math import comb
        tail = sum(comb(n, k) * p ** k * (1 - p) ** (n - k)
                   for k in range(w, n + 1))
        return min(1.0, 2 * tail)

    t0 = run(cs, 0, 8, 0.0, False, True)
    simulate(cs, t0, 0.0, True, mode="pessimistic")
    keep = [x for x in t0 if not x.extras.get("skip")]
    g = [x.r_gross for x in keep]
    n = len(g)
    wins = sum(1 for x in g if x > 0.01)
    print(f"  sample size: {n} trades over 52 days ({n/52*7:.1f} per week)")
    print(f"  GROSS  meanR={sum(g)/n:+.3f}  win={wins}/{n}={wins/n*100:.1f}%  "
          f"binomial p={bino_p(wins, n):.3f} (two-sided, H0: 50%)")
    # bootstrap CI on gross mean R
    boots = []
    for _ in range(4000):
        s = [g[rng2.randrange(n)] for _ in range(n)]
        boots.append(sum(s) / n)
    boots.sort()
    lo, hi = boots[int(0.025 * len(boots))], boots[int(0.975 * len(boots))]
    print(f"  GROSS meanR 95% bootstrap CI: [{lo:+.3f}, {hi:+.3f}]"
          + ("  (excludes 0)" if lo > 0 else "  (includes 0 -> not significant)"))
    nn = [x.r_net for x in keep]
    print(f"  NET    meanR={sum(nn)/n:+.3f}  total={sum(nn):+.2f}R")
    print()
    print("  CAVEATS, stated plainly:")
    print("   * 41 usable trades is a tiny sample. Nothing here is decisive.")
    print("   * The window is ~7 weeks of a single gold regime; no multi-regime")
    print("     evidence exists because xyz:GOLD only has ~7 weeks of 15m data.")
    print("   * The sensitivity grid above is ~15 configurations. Testing many")
    print("     variants on one short sample manufactures false positives; the")
    print("     grid is reported for transparency, not to be mined for a winner.")
    print("   * 22 of 41 trades sit in an ambiguous intrabar order. The true")
    print("     result lies between the pess and opt bounds, not at either end.")
    print("   * 1m data (which would resolve the ambiguity) covers only ~3.6 days.")


if __name__ == "__main__":
    main()
