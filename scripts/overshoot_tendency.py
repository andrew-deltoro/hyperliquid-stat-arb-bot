#!/usr/bin/env python
"""Do some pairs OVERSHOOT through the residual z=0, and is that selectable?

What this measures, and the two traps that make the naive version meaningless
--------------------------------------------------------------------------------
The live bot does NOT trade the raw log-ratio log(A)-log(B) -- that is a random
walk and never reverts. It trades the COINTEGRATION RESIDUAL

        residual_t = log(A_t) - alpha - beta * log(B_t)

with (alpha, beta) fitted on the lookback and the residual mean-subtracted, so
that "spread" is the de-trended residual and z = residual / sigma.

Trap 1: ROLLING Z IS MECHANICAL. z crossing zero is partly guaranteed by the
window sliding forward. Fix: fit (alpha, beta, mean, sigma) ONCE from bars
strictly BEFORE the episode and hold them fixed over the forward path --
exactly what the bot freezes at entry (entry_mean_raw / entry_sigma_raw /
entry_spread). No look-ahead, no window drift.

Trap 2: A RANDOM WALK IS RECURRENT. "Did it pass the centre, ever?" is true with
probability ~1 given enough time, so P(cross) is NOT a discriminator. The real
questions are:
    (a) HOW FAR it overshoots past the centre within a fixed horizon, and
    (b) THE ECONOMICS: does holding for the cross beat exiting at the centre on
        identical episodes, and
    (c) THE DECISIVE ONE: is a pair's overshoot tendency STABLE out of sample?
Anyone can find ex-post overshoot. The only thing that permits pair SELECTION is
persistence. If first-half and second-half tendencies do not agree, there is no
per-pair trait to pick on, and the right answer is to run the cross exit on
every pair (which the live bot already does).

Usage:
    .venv/bin/python scripts/overshoot_tendency.py
    .venv/bin/python scripts/overshoot_tendency.py --surrogates 30 --window 720
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.stdout.reconfigure(line_buffering=True)

import numpy as np  # noqa: E402

CACHE = REPO / "reports" / ".overshoot_cache"
LIVE = Path("/home/hlbot/hlbot-runtime/live")


CONFIG = REPO / "config" / "phase3-live.yaml"


def live_pairs() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not CONFIG.exists():
        return out
    import yaml
    try:
        d = yaml.safe_load(CONFIG.read_text()) or {}
    except Exception:
        return out
    for p in (d.get("pairs") or d.get("universe") or []):
        if isinstance(p, (list, tuple)) and len(p) == 2:
            out.append((str(p[0]), str(p[1])))
        elif isinstance(p, str) and "/" in p:
            a, b = p.split("/", 1)
            out.append((a, b))
    return out


def load_logs(pair: tuple[str, str], force: bool):
    """(ts_ms, log_a, log_b) cached."""
    CACHE.mkdir(parents=True, exist_ok=True)
    a, b = pair
    f = CACHE / (f"{a}_{b}".replace("/", "_").replace(":", "-") + ".csv")
    if f.exists() and not force:
        arr = np.genfromtxt(f, delimiter=",", names=True)
        if arr.size:
            arr = np.atleast_1d(arr)
            return arr["ts"], arr["log_a"], arr["log_b"]
        return None
    try:
        from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
        ts, ca, cb = aligned_close_prices(a, b, interval="1h",
                                          lookback_hours=24 * 300)
    except Exception as e:
        print(f"    fetch failed {a}/{b}: {str(e)[:60]}")
        return None
    if ts is None or len(ts) < 100:
        return None
    la, lb = np.log(np.asarray(ca, float)), np.log(np.asarray(cb, float))
    with open(f, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "log_a", "log_b"])
        for t, x, y in zip(ts, la, lb):
            w.writerow([int(t), repr(float(x)), repr(float(y))])
    return np.asarray(ts, float), la, lb


def fit_residual(la, lb, i, W):
    """OLS beta on the W bars ending at i (exclusive). Returns (resid_path, ...).

    The pre-window is [i-W, i). We fit slope/intercept there, then apply the SAME
    (alpha, beta, mean, sigma) to the window itself and to the forward path, so
    nothing about the forward path leaks into the fit.
    """
    xa = la[i - W:i]
    xb = lb[i - W:i]
    if len(xa) < 30 or np.std(xb) <= 0:
        return None
    beta, alpha = np.polyfit(xb, xa, 1)
    pre = xa - (alpha + beta * xb)
    mean, sd = float(pre.mean()), float(pre.std())
    if sd <= 0:
        return None
    return alpha, beta, mean, sd


def episodes(ts, la, lb, W, Z_ENTRY, H, max_gap_ms=None):
    n = len(la)
    out = []
    i = W
    while i < n - 2:
        fit = fit_residual(la, lb, i, W)
        if fit is None:
            i += 1
            continue
        alpha, beta, mean, sd = fit
        z_prev = ((la[i - 1] - (alpha + beta * lb[i - 1])) - mean) / sd
        z_i = ((la[i] - (alpha + beta * lb[i])) - mean) / sd
        if abs(z_i) >= Z_ENTRY and abs(z_prev) < Z_ENTRY:
            j_end = min(i + H, n - 1)
            resid = (la[i:j_end + 1] - (alpha + beta * lb[i:j_end + 1])) - mean
            zf = resid / sd
            out.append({
                "ts": float(ts[i]), "i": i, "z_entry": float(z_i),
                "sign": -1.0 if z_i > 0 else 1.0,
                "resid_fwd": resid, "zf": zf,
            })
            i += H                       # non-overlapping episodes
        else:
            i += 1
    return out


def measure(e, Z_EXIT, CROSS_FRAC):
    zf, rf = e["zf"], e["resid_fwd"]
    zi, sg = e["z_entry"], e["sign"]
    # overshoot = how far past the centre, on the opposite side, in z units
    opp = float(-zf.min()) if zi > 0 else float(zf.max())
    c_idx = next((k for k in range(1, len(zf)) if abs(zf[k]) <= Z_EXIT), None)
    # cross: opposite side and |z| >= CROSS_FRAC * |z_entry|
    x_idx = next((k for k in range(1, len(zf))
                  if zf[k] * zi < 0 and abs(zf[k]) >= CROSS_FRAC * abs(zi)), None)

    def ret(idx):                        # 0.5 * sign * (resid_exit - resid_0)
        k = idx if idx is not None else len(rf) - 1
        return 0.5 * sg * (rf[k] - rf[0])

    return {
        "z_entry": zi, "overshoot_z": opp,
        "overshoot_frac": opp / abs(zi) if zi else 0.0,
        "bars_to_centre": c_idx if c_idx is not None else len(zf) - 1,
        "centre_triggered": c_idx is not None,
        "cross_triggered": x_idx is not None,
        "ret_centre": ret(c_idx), "ret_cross": ret(x_idx),
        "diff": ret(x_idx) - ret(c_idx),
    }


def run(ts, la, lb, cfg):
    eps = episodes(ts, la, lb, cfg["W"], cfg["Z_ENTRY"], cfg["H"])
    return [measure(e, cfg["Z_EXIT"], cfg["CROSS_FRAC"]) for e in eps]


def summarise(rows):
    if not rows:
        return None
    g = lambda k: np.asarray([r[k] for r in rows], float)
    return {
        "n": len(rows),
        "p_cross": g("cross_triggered").mean(),
        "med_overshoot": float(np.median(g("overshoot_frac"))),
        "med_bars": float(np.median(g("bars_to_centre"))),
        "ret_centre": g("ret_centre").mean(),
        "ret_cross": g("ret_cross").mean(),
        "diff": g("diff").mean(),
        "pct_better": (g("diff") > 0).mean(),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=720, help="fit bars (720=30d@1h)")
    ap.add_argument("--z-entry", type=float, default=2.0)
    ap.add_argument("--z-exit", type=float, default=0.25)
    ap.add_argument("--cross-frac", type=float, default=0.75)
    ap.add_argument("--horizon", type=int, default=168, help="bars (168=7d@1h)")
    ap.add_argument("--surrogates", type=int, default=20)
    ap.add_argument("--force", action="store_true", help="refetch log prices")
    args = ap.parse_args()
    cfg = dict(W=args.window, Z_ENTRY=args.z_entry, Z_EXIT=args.z_exit,
               CROSS_FRAC=args.cross_frac, H=args.horizon)

    pairs = live_pairs()
    if not pairs:
        for f in CACHE.glob("*.csv"):
            a, b = f.stem.split("_", 1)
            pairs.append((a.replace("-", ":"), b.replace("-", ":")))
    data = {}
    for pr in pairs:
        got = load_logs(pr, args.force)
        if got is not None and len(got[0]) > args.window + 200:
            data[pr] = got
    print(f"pairs with data: {len(data)}")
    print(f"residual = logA - alpha - beta*logB, (alpha,beta,mean,sigma) fitted on")
    print(f"the {args.window} bars BEFORE each episode and held fixed forward")
    print(f"entry |z|>={args.z_entry}, horizon {args.horizon} bars, "
          f"cross_frac={args.cross_frac}\n")

    print("=" * 106)
    print("PER-PAIR OVERSHOOT AND EXIT ECONOMICS")
    print("=" * 106)
    print(f"  {'pair':<22}{'n':>5}{'P(cross)':>10}{'ovshoot/|z|':>13}{'bars->0':>9}"
          f"{'retC':>10}{'retX':>10}{'X-C':>9}{'X>btr%':>10}")
    per, allrows = {}, {}
    for p, (ts, la, lb) in sorted(data.items()):
        rows = run(ts, la, lb, cfg)
        allrows[p] = rows
        s = summarise(rows)
        if not s:
            continue
        per[p] = s
        name = "/".join(p)
        print(f"  {name:<22}{s['n']:>5}{s['p_cross']*100:>9.0f}%"
              f"{s['med_overshoot']:>13.2f}{s['med_bars']:>9.0f}"
              f"{s['ret_centre']*100:>9.2f}%{s['ret_cross']*100:>9.2f}%"
              f"{s['diff']*100:>8.2f}%{s['pct_better']*100:>9.0f}%")
    flat = [r for rs in allrows.values() for r in rs]
    tot = summarise(flat)
    if tot is None:
        print("\n  no episodes found -- widen the window or lower --z-entry")
        return
    print(f"\n  {'ALL PAIRS':<22}{tot['n']:>5}{tot['p_cross']*100:>9.0f}%"
          f"{tot['med_overshoot']:>13.2f}{tot['med_bars']:>9.0f}"
          f"{tot['ret_centre']*100:>9.2f}%{tot['ret_cross']*100:>9.2f}%"
          f"{tot['diff']*100:>8.2f}%{tot['pct_better']*100:>9.0f}%")

    # ---------------------------------------------- null: shuffled increments
    print("\n" + "=" * 106)
    print(f"NULL: identical pipeline on SHUFFLED residual increments "
          f"({args.surrogates} draws/pair)")
    print("=" * 106)
    rng = np.random.default_rng(7)
    real_ov = np.asarray([r["overshoot_frac"] for r in flat], float)
    real_bt = np.asarray([r["bars_to_centre"] for r in flat], float)
    real_df = np.asarray([r["diff"] for r in flat], float)
    so, sb, sdf = [], [], []
    for p, (ts, la, lb) in data.items():
        # shuffle the log-B increments to break the cointegrating link
        dlb = np.diff(lb)
        for _ in range(args.surrogates):
            shuf = np.concatenate([[lb[0]], lb[0] + np.cumsum(rng.permutation(dlb))])
            rows = run(ts, la, shuf, cfg)
            if rows:
                so.append(np.median([r["overshoot_frac"] for r in rows]))
                sb.append(np.median([r["bars_to_centre"] for r in rows]))
                sdf.append(np.mean([r["diff"] for r in rows]))
    so, sb, sdf = np.asarray(so), np.asarray(sb), np.asarray(sdf)
    print(f"  {'statistic':<26}{'REAL':>11}{'shuf mean':>12}{'shuf p95':>11}")
    print(f"  {'median overshoot/|z|':<26}{np.median(real_ov):>11.2f}"
          f"{so.mean():>12.2f}{np.percentile(so,95):>11.2f}")
    print(f"  {'median bars to centre':<26}{np.median(real_bt):>11.0f}"
          f"{sb.mean():>12.0f}{np.percentile(sb,95):>11.0f}")
    print(f"  {'mean(X - C) return':<26}{real_df.mean()*100:>10.2f}%"
          f"{sdf.mean()*100:>11.2f}%{np.percentile(sdf,95)*100:>10.2f}%")
    print(f"  real overshoot is deeper than shuffled in "
          f"{(so < np.median(real_ov)).mean()*100:.0f}% of draws")

    # ---------------------------------------------- persistence (decisive test)
    print("\n" + "=" * 106)
    print("PERSISTENCE: does a pair's first-half overshoot tendency predict its")
    print("second half? (selection is only possible if it does)")
    print("=" * 106)
    names, first, second, agree = [], [], [], 0
    for p, rows in sorted(allrows.items()):
        if len(rows) < 12:
            continue
        h = len(rows) // 2
        a = np.mean([r["diff"] for r in rows[:h]])
        b = np.mean([r["diff"] for r in rows[h:]])
        names.append("/".join(p)); first.append(a); second.append(b)
    if len(names) >= 5:
        first, second = np.asarray(first), np.asarray(second)
        c = float(np.corrcoef(first, second)[0, 1])
        agree = float(np.mean((first > 0) == (second > 0)))
        print(f"  pairs with >=12 episodes: {len(names)}")
        print(f"  corr(1st half, 2nd half) = {c:+.3f}")
        print(f"  {'pair':<24}{'1st half':>11}{'2nd half':>11}{'stable':>9}")
        for nm, a, b in sorted(zip(names, first, second), key=lambda z: z[1]):
            print(f"  {nm:<24}{a*100:>10.2f}%{b*100:>10.2f}%"
                  f"{'yes' if (a > 0) == (b > 0) else 'FLIP':>9}")
        print(f"\n  sign agreement = {agree*100:.0f}%  "
              f"(50% = no persistence, 100% = perfectly stable)")

        # permutation test: break the pairing between halves and recompute corr
        rng2 = np.random.default_rng(11)
        perm = []
        for _ in range(2000):
            sh = rng2.permutation(len(first))
            perm.append(np.corrcoef(first, second[sh])[0, 1])
        perm = np.asarray(perm)
        p = float(np.mean(perm >= c))
        print(f"  permutation test (2000 draws): p = {p:.3f}  "
              f"[shuffled corr mean {perm.mean():+.3f}, p95 {np.percentile(perm,95):+.3f}]")
        if p > 0.10:
            print("  => the apparent persistence is NOT distinguishable from chance.")

    # pooled episode-level test on the sign of (cross - centre)
    diffs = np.asarray([r["diff"] for r in flat], float)
    wins = int((diffs > 0).sum())
    n = len(diffs)
    from math import comb
    p_binom = sum(comb(n, k) for k in range(wins, n + 1)) / 2 ** n if n <= 1000 else float("nan")
    print(f"\n  pooled episodes: cross beats centre in {wins}/{n} "
          f"({wins/n*100:.0f}%), one-sided binomial p = {p_binom:.3f}")

    # ---------------------------------------------- verdict
    print("\n" + "=" * 106)
    print("VERDICT")
    print("=" * 106)
    print(f"  Cross triggers on {tot['p_cross']*100:.0f}% of episodes; beats the centre exit in")
    print(f"  {tot['pct_better']*100:.0f}% of them ({tot['diff']*100:+.2f}% mean uplift across n={tot['n']}).")
    if len(names) >= 5:
        print(f"  Out-of-sample persistence: {agree*100:.0f}% sign agreement, corr={c:+.3f},")
        print(f"  permutation p={p:.3f}.")
        if p > 0.10 or agree < 0.7:
            print("  => the per-pair tendency is NOT statistically stable. There is no")
            print("     reliable 'crossing pair' trait to select on. The overshoot is a")
            print("     property of the spread construction + the entry event, not of")
            print("     individual pairs. Correct action: apply the cross exit to every")
            print("     pair (already the live behaviour) instead of whitelisting pairs.")
        else:
            print("  => tendencies are statistically persistent. Per-pair exit modes")
            print("     would be justified (subject to more episodes than available here).")
    print("\n  Limits: 1h bars (live evaluates at 4h); static OLS beta (live uses a")
    print("  hierarchical Bayesian ECM); one regime; horizon-capped so a cross that")
    print("  arrives after the cap is scored as a miss.")


if __name__ == "__main__":
    main()
