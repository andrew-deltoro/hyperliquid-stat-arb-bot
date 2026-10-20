#!/usr/bin/env python
"""Multi-regime IFVG test: OKX PAXG-USDT 15m (gold-backed token, ~7 months).

Why this exists
---------------
xyz:GOLD on Hyperliquid only has ~52 days of 15m history, which is one regime
and too little to decide anything. PAXG (Paxos Gold) is a physically-backed
gold token whose price tracks XAU/USD. OKX serves ~7 months of its 15m bars,
which lets the SAME strategy be tested across several regimes.

Caveats that matter when reading the output
-------------------------------------------
 * PAXG is a proxy for gold, not gold itself, and not xyz:GOLD. During Asia
   hours a tokenised-gold book can carry a basis and thinner volume than the
   real XAU/USD or CME GC book. Treat it as evidence about the *pattern*, not
   as proof of tradeable edge on any specific venue.
 * Fees are applied per the venue named on the run: 'hl' uses Hyperliquid's
   taker fee, 'okx' uses OKX spot taker, 'zero' ignores them. None is the fee
   the source post's CME account paid.

Everything is reported gross AND net, and across chronological folds, so a
single lucky stretch cannot masquerade as an edge.
"""
from __future__ import annotations

import json
import statistics as st
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ifvg_gold_backtest import C, find_fvgs, session_id  # noqa: E402

OUT = Path(__file__).resolve().parent.parent.parent / "reports" / ".ifvg_cache"
OUT.mkdir(parents=True, exist_ok=True)
OKX = "https://www.okx.com/api/v5/market/history-candles"

FEES = {"hl": 0.00045, "okx": 0.00080, "zero": 0.0}


# ------------------------------------------------------------------ data
def fetch_okx(inst: str = "PAXG-USDT", max_bars: int = 24000) -> list[C]:
    f = OUT / f"okx_{inst.replace('-', '_')}_15m.json"
    if f.exists():
        rows = json.loads(f.read_text())
    else:
        rows: list[list] = []
        cursor = None
        while len(rows) < max_bars:
            p = {"instId": inst, "bar": "15m", "limit": "100"}
            if cursor:
                p["after"] = str(cursor)
            r = requests.get(OKX, params=p, timeout=25).json()
            data = r.get("data") or []
            if not data:
                break
            rows.extend(data)
            cursor = data[-1][0]
            time.sleep(0.15)
        f.write_text(json.dumps(rows))

    uniq = {int(x[0]): x for x in rows}
    out = []
    for ts in sorted(uniq):
        x = uniq[ts]
        out.append(C(t=ts, o=float(x[1]), h=float(x[2]), l=float(x[3]),
                     c=float(x[4]), v=float(x[5])))
    return out


# --------------------------------------------------------------- signals
@dataclass
class T:
    session: str
    direction: str
    entry: float
    stop: float
    target: float
    be: float
    entry_idx: int
    gross: float = 0.0
    net: float = 0.0
    reason: str = ""
    R: float = 0.0
    ambiguous: int = 0
    skip: bool = False


def make_trades(cs: list[C], stop_mode: str = "candle", k: float = 1.0,
                start_h: int = 0, end_h: int = 8,
                be_frac: float = 0.0) -> list[T]:
    """First qualifying IFVG per session. stop_mode 'candle' = source rule;
    'atr' = k * average 15m range (the wider-stop rescue variant)."""
    atr = sum(c.h - c.l for c in cs) / len(cs)
    by_sess: dict[str, list[int]] = {}
    for i, c in enumerate(cs):
        s = session_id(c, start_h, end_h)
        if s:
            by_sess.setdefault(s, []).append(i)

    trades: list[T] = []
    for s in sorted(by_sess):
        gaps: list[tuple[str, float, float]] = []
        taken = False
        for i in by_sess[s]:
            c = cs[i]
            if not taken:
                for kind, lo, hi in gaps:
                    if kind == "bear" and c.c > hi:
                        long = True
                    elif kind == "bull" and c.c < lo:
                        long = False
                    else:
                        continue
                    if i + 1 >= len(cs):
                        break
                    entry = cs[i + 1].o
                    R = (abs(entry - (c.l - 0.1)) if long
                         else abs((c.h + 0.1) - entry)) if stop_mode == "candle" \
                        else k * atr
                    if R <= 0:
                        break
                    trades.append(T(
                        session=s,
                        direction="long" if long else "short",
                        entry=entry,
                        stop=entry - R if long else entry + R,
                        target=entry + R if long else entry - R,
                        be=entry + be_frac * R if long else entry - be_frac * R,
                        entry_idx=i + 1, R=R))
                    taken = True
                    break
            for kind, lo, hi in find_fvgs(cs, i):
                gaps.append((kind, lo, hi))
    return trades


def simulate(cs: list[C], trades: list[T], fee_key: str,
             mode: str = "pessimistic") -> None:
    fee = FEES[fee_key]
    worse = mode == "pessimistic"
    for tr in trades:
        long = tr.direction == "long"
        stop, armed, hit = tr.stop, False, False
        for j in range(tr.entry_idx, len(cs)):
            c = cs[j]
            up = (lambda lv: c.h >= lv) if long else (lambda lv: c.l <= lv)
            dn = (lambda lv: c.l <= lv) if long else (lambda lv: c.h >= lv)
            hit_stop, hit_tgt = dn(stop), up(tr.target)
            hit_be = up(tr.be) if tr.be != tr.entry else False

            if not armed and hit_be:
                if hit_stop:
                    tr.gross = 1.0 if (hit_tgt and not worse) else -1.0
                    tr.ambiguous += 1
                    tr.reason = "ambig"
                    hit = True
                    break
                armed, stop = True, tr.entry
                if hit_tgt:
                    tr.gross = 0.0 if worse else 1.0
                    tr.ambiguous += 1
                    tr.reason = "ambig"
                    hit = True
                    break
                continue
            if hit_stop and hit_tgt:
                tr.ambiguous += 1
                tr.gross = -1.0 if worse else 1.0
                tr.reason = "ambig"
                hit = True
                break
            if hit_stop:
                tr.gross = 0.0 if stop == tr.entry else -1.0
                tr.reason = "breakeven" if stop == tr.entry else "stop"
                hit = True
                break
            if hit_tgt:
                tr.gross, tr.reason, hit = 1.0, "target", True
                break
        if not hit:
            last = cs[-1].c
            tr.gross = (last - tr.entry) / tr.R if long else (tr.entry - last) / tr.R
            tr.reason = "open_at_end"
        cost_r = (2 * fee) * tr.entry / tr.R if tr.R else float("inf")
        tr.net = tr.gross - cost_r
        tr.skip = cost_r > 1.0   # fee alone exceeds one full R: not tradable


def summ(trades: list[T], fee_key: str, label: str) -> None:
    skips = [t for t in trades if t.skip]
    keep = [t for t in trades if not t.skip]
    if not keep:
        print(f"  {label:<30} all {len(trades)} signals skipped (fee > 1R)")
        return
    g = [t.gross for t in keep]
    n = len(g)
    nn = [t.net for t in keep]
    w = sum(1 for x in g if x > 0.01)
    l = sum(1 for x in g if x < -0.01)
    print(f"  {label:<30} n={n:>3} gross={sum(g)/n:>+6.3f}R  "
          f"net={sum(nn)/n:>+6.3f}R  W/L/BE={w}/{l}/{n-w-l}  "
          f"win={100*w/n:>4.0f}%  totNet={sum(nn):>+7.2f}R"
          + (f"  ambig={sum(t.ambiguous for t in keep)}"
             if any(t.ambiguous for t in keep) else "")
          + (f"  SKIP={len(skips)}" if skips else ""))


def folds(cs: list[C], nfolds: int = 4):
    step = len(cs) // nfolds
    for i in range(nfolds):
        lo = i * step
        hi = (i + 1) * step if i < nfolds - 1 else len(cs)
        yield f"fold {i+1} {cs[lo].dt.date()}..{cs[hi-1].dt.date()}", cs[lo:hi]


def main() -> None:
    print("=" * 100)
    print("MULTI-REGIME IFVG: OKX PAXG-USDT 15m (gold-backed token)")
    print("=" * 100)
    cs = fetch_okx()
    print(f"  bars={len(cs)}  {cs[0].dt} -> {cs[-1].dt}  "
          f"({(cs[-1].t-cs[0].t)/1000/86400:.0f} days)")
    print(f"  price range: {min(c.l for c in cs):.0f} .. {max(c.h for c in cs):.0f}")
    sess = {session_id(c, 0, 8) for c in cs} - {None}
    print(f"  Asian sessions: {len(sess)}")

    for fee_key in ("zero", "hl", "okx"):
        print()
        print("-" * 100)
        print(f"FEE MODEL: {fee_key} "
              f"({FEES[fee_key]*2*100:.3f}% round trip on notional)")
        print("-" * 100)
        for mode, k, nm in (("candle", 1.0, "source rule: 1-tick stop"),
                            ("atr", 1.0, "wider: stop 1.0x avg range"),
                            ("atr", 2.0, "wider: stop 2.0x avg range"),
                            ("atr", 3.0, "wider: stop 3.0x avg range"),
                            ("atr", 4.0, "wider: stop 4.0x avg range")):
            t = make_trades(cs, stop_mode=mode, k=k, be_frac=0.0)
            simulate(cs, t, fee_key, mode="pessimistic")
            summ(t, fee_key, nm)

    print()
    print("=" * 100)
    print("BREAKEVEN RULE on the wider (2x range) stop, HL fees")
    print("=" * 100)
    for bf, nm in ((0.0, "no breakeven"), (0.5, "BE 50%"),
                   (0.68, "BE 68%"), (0.75, "BE 75%")):
        t = make_trades(cs, stop_mode="atr", k=2.0, be_frac=bf)
        simulate(cs, t, "hl", mode="pessimistic")
        summ(t, "hl", nm)

    print()
    print("=" * 100)
    print("CHRONOLOGICAL FOLDS (rules frozen), wider 2x-range stop, HL fees")
    print("=" * 100)
    for nm, sub in folds(cs, 4):
        t = make_trades(sub, stop_mode="atr", k=2.0, be_frac=0.0)
        simulate(sub, t, "hl", mode="pessimistic")
        summ(t, "hl", nm)

    print()
    print("=" * 100)
    print("SIGNIFICANCE (gross, 2x-range stop, whole sample)")
    print("=" * 100)
    import random
    t = make_trades(cs, stop_mode="atr", k=2.0, be_frac=0.0)
    simulate(cs, t, "hl", mode="pessimistic")
    g = [x.gross for x in t]
    n = len(g)
    mean = sum(g) / n
    sd = st.pstdev(g) if n > 1 else 0
    rng = random.Random(7)
    boots = sorted(sum(g[rng.randrange(n)] for _ in range(n)) / n
                   for _ in range(5000))
    print(f"  n={n}  gross meanR={mean:+.3f}  sd={sd:.3f}  "
          f"t={mean/(sd/n**0.5):+.2f}")
    print(f"  95% bootstrap CI: [{boots[int(.025*len(boots))]:+.3f}, "
          f"{boots[int(.975*len(boots))]:+.3f}]")
    print("  NOTE: PAXG is a proxy; a positive result here is evidence about the")
    print("  pattern in gold, not proof of edge on xyz:GOLD or CME GC.")


if __name__ == "__main__":
    main()
