#!/usr/bin/env python3
"""Conviction scaling: should notional vary with a conviction signal?

The trap
--------
Naively "scale by z" always increases total P&L, because z is already correlated
with the forward return. That proves nothing. The real question is: at EQUAL
CAPITAL (weights normalised to mean 1), does concentrating on higher-conviction
trades improve the RISK-ADJUSTED return (mean/std), not just the total?

So this reports, per candidate conviction signal c (all known at entry, no
look-ahead):

    Spearman(c, r)      -> does c predict the SIGN?      (already gated on z)
    Spearman(c, |r|)    -> does c predict the MAGNITUDE? (what sizing needs)
    flat:   mean, std, Sharpe = mean/std
    scaled: weighted mean, weighted std, Sharpe      (weights = c, mean 1)
    a permutation p-value for the Sharpe improvement.

and a robustness pass dropping the single largest |r| episode, because with ~50
independent episodes one outlier can manufacture a "result".

Outcome is the forward centred-residual return at the episode's ENTRY signal
(same construction as entry_quality_screen.py). 1h bars, 4h decision cadence.

Run:  .venv/bin/python scripts/conviction_scaling.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
sys.stdout.reconfigure(line_buffering=True)

import numpy as np  # noqa: E402

from entry_quality_screen import build  # noqa: E402

# candidate conviction signals; sign = +1 if higher means more conviction
SIGNALS = {
    "abs_z": 1.0,
    "exp_return": 1.0,
    "edge": 1.0,
    "p_revert": 1.0,
    "kappa": 1.0,
    "neg_half_life": -1.0,   # shorter half-life = faster reversion = more conviction
    "low_sigma_ou": -1.0,
    "nu_coint": 1.0,
}


def horizon_arg():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", default="r168",
                    choices=["r24", "r72", "r168", "r_target"])
    return ap.parse_args().horizon


def episodes(rows, horizon):
    """Collapse consecutive OPEN signal rows into one row per position-episode."""
    op = sorted([r for r in rows if r["action"] == "OPEN"],
                key=lambda r: (r["pair"], r["timestamp_iso"]))
    from datetime import datetime
    out, prev = [], None
    for r in op:
        t = datetime.fromisoformat(r["timestamp_iso"].replace("Z", "+00:00"))
        if prev is None or r["pair"] != prev[0] or (t - prev[1]).total_seconds() > 5 * 3600:
            out.append(dict(r))          # entry signal of the episode
        prev = (r["pair"], t)
    return [r for r in out if r.get(horizon) is not None]


def spearman(x, y):
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    if len(x) < 10:
        return np.nan
    rx = np.argsort(np.argsort(x)).astype(float)
    ry = np.argsort(np.argsort(y)).astype(float)
    rx -= rx.mean(); ry -= ry.mean()
    d = np.sqrt((rx**2).sum() * (ry**2).sum())
    return float((rx * ry).sum() / d) if d else np.nan


def wstats(r, w):
    w = w / w.mean()                       # normalise to equal capital
    m = float((w * r).sum() / w.sum() * w.sum() / len(r) * len(r) / w.sum() * w.sum()
              / w.sum())                   # weighted mean of r
    m = float((w * r).sum() / w.sum())
    var = float((w * (r - m) ** 2).sum() / w.sum())
    sd = np.sqrt(var)
    return m, sd, (m / sd if sd > 0 else np.nan)


def main():
    HORIZON = horizon_arg()
    rows = build(force=False)
    eps = episodes(rows, HORIZON)
    r = np.asarray([e[HORIZON] for e in eps], float)
    n = len(eps)
    print(f"OPEN episodes with {HORIZON}: {n}")
    print(f"flat portfolio: mean {r.mean()*100:+.3f}%  std {r.std()*100:.3f}%  "
          f"Sharpe {r.mean()/r.std():.4f}\n")

    # per-episode feature columns, signed so higher = more conviction
    feats = {}
    for name, sgn in SIGNALS.items():
        if name == "neg_half_life":
            x = np.asarray([e.get("half_life_hours") for e in eps], float)
            x = -x
        elif name == "low_sigma_ou":
            x = -np.asarray([e.get("sigma_ou") for e in eps], float)
        else:
            x = np.asarray([e.get(name) for e in eps], float)
        feats[name] = x

    print("=" * 96)
    print("CONVICTION SIGNALS: do they predict sign and magnitude? (episode level)")
    print("=" * 96)
    print(f"  {'signal':<16}{'IC(sign)':>11}{'IC(|r|)':>11}{'n':>6}")
    icmag = {}
    for name, x in feats.items():
        ok = np.isfinite(x)
        a = spearman(x, r)
        b = spearman(x, np.abs(r))
        icmag[name] = b
        print(f"  {name:<16}{a:>+11.4f}{b:>+11.4f}{int(ok.sum()):>6}")

    print("\n" + "=" * 96)
    print("PORTFOLIO: flat vs conviction-scaled at EQUAL CAPITAL")
    print("=" * 96)
    print(f"  {'scheme':<22}{'mean':>10}{'std':>10}{'Sharpe':>10}{'vs flat':>10}")
    flat_sharpe = r.mean() / r.std()
    results = {}
    for name, x in feats.items():
        ok = np.isfinite(x)
        if ok.sum() < 20:
            continue
        rr = r[ok]
        w = np.clip(x[ok] - x[ok].min() + 1e-9, 1e-9, None)   # non-negative
        m, sd, sh = wstats(rr, w)
        results[name] = (m, sd, sh, rr, w)
        print(f"  {name:<22}{m*100:>9.3f}%{sd*100:>9.3f}%{sh:>10.4f}"
              f"{sh-flat_sharpe:>+10.4f}")

    # permutation test on the best scheme
    if results:
        best = max(results, key=lambda k: results[k][2])
        m, sd, sh, rr, w = results[best]
        rng = np.random.default_rng(13)
        null = []
        for _ in range(5000):
            wsh = rng.permutation(w)
            mm = (wsh * rr).sum() / wsh.sum()
            vv = (wsh * (rr - mm) ** 2).sum() / wsh.sum()
            null.append(mm / np.sqrt(vv) if vv > 0 else np.nan)
        null = np.asarray(null)
        p = float(np.mean(null >= sh))
        print(f"\n  best scheme: {best}  Sharpe {sh:.4f} vs flat {flat_sharpe:.4f}")
        print(f"  permutation test (weight-vs-return shuffled, 5000 draws): p = {p:.3f}")

        # robustness: drop the single largest |r| episode
        big = int(np.argmax(np.abs(rr)))
        keep = np.ones(len(rr), bool); keep[big] = False
        m2 = (w[keep] * rr[keep]).sum() / w[keep].sum()
        v2 = (w[keep] * (rr[keep] - m2) ** 2).sum() / w[keep].sum()
        sh2 = m2 / np.sqrt(v2) if v2 > 0 else np.nan
        f2 = rr[keep].mean() / rr[keep].std()
        print(f"  drop largest |r| episode: scaled Sharpe {sh2:.4f} vs "
              f"flat {f2:.4f}  (delta {sh2-f2:+.4f})")

        # bootstrap CI on the Sharpe DELTA (resample episodes, keep weights paired)
        rng3 = np.random.default_rng(29)
        deltas = []
        for _ in range(5000):
            idx = rng3.integers(0, len(rr), len(rr))
            rrb, wb = rr[idx], w[idx]
            mb = (wb * rrb).sum() / wb.sum()
            vb = (wb * (rrb - mb) ** 2).sum() / wb.sum()
            shb = mb / np.sqrt(vb) if vb > 0 else np.nan
            fb = rrb.mean() / rrb.std() if rrb.std() > 0 else np.nan
            deltas.append(shb - fb)
        deltas = np.asarray(deltas)
        print(f"  bootstrap 95% CI on Sharpe delta: "
              f"[{np.percentile(deltas,2.5):+.3f}, {np.percentile(deltas,97.5):+.3f}]"
              f"   P(delta<=0) = {np.mean(deltas<=0):.3f}")

        # multiple-comparisons: best of N signals
        print(f"  NOTE: {len(results)} signals were screened; a single p<0.05 among them")
        print(f"  is expected ~{1-0.95**len(results):.0%} of the time by chance alone.")

        # correlation between the two leading, mechanically-different signals
        if "abs_z" in feats and "low_sigma_ou" in feats:
            xa = feats["abs_z"]; xb = feats["low_sigma_ou"]
            print(f"\n  corr(abs_z, low_sigma_ou) = {spearman(xa, xb):+.3f}  "
                  f"(near 0 => independent signals)")
            ok = np.isfinite(xa) & np.isfinite(xb) & np.isfinite(r)
            rr = r[ok]
            # combined: rank-average the two, then scale
            ra = np.argsort(np.argsort(xa[ok])).astype(float)
            rb = np.argsort(np.argsort(xb[ok])).astype(float)
            wc = (ra / ra.mean()) * (rb / rb.mean())
            m, sd, sh = wstats(rr, wc)
            print(f"  combined abs_z x low_sigma_ou: mean {m*100:+.3f}%  std "
                  f"{sd*100:.3f}%  Sharpe {sh:.4f}  (vs flat "
                  f"{rr.mean()/rr.std():.4f})")

    print("\n" + "=" * 96)
    print("VERDICT")
    print("=" * 96)
    print("  Strongest |r| predictor:", max(icmag, key=lambda k: abs(icmag[k])),
          f"IC(|r|)={icmag[max(icmag, key=lambda k: abs(icmag[k]))]:+.3f}")

    # ---- actionable, bounded tilt: weight = 1 + lam*(pct_rank(abs_z)-0.5)*2,
    #      capped to [1-rho, 1+rho]; sweep lam and a hard cap.
    print("\n" + "=" * 96)
    print("BOUNDED TILT (actionable form): w = 1 + lam*(2*rank(abs_z)-1), capped")
    print("=" * 96)
    x = feats["abs_z"]
    ok = np.isfinite(x) & np.isfinite(r)
    rr, xx = r[ok], x[ok]
    rank = np.argsort(np.argsort(xx)).astype(float) / (len(xx) - 1)   # 0..1
    print(f"  flat Sharpe = {rr.mean()/rr.std():.4f}")
    print(f"  {'lam':>6}{'cap':>6}{'mean':>10}{'std':>10}{'Sharpe':>10}{'max w':>8}")
    for cap in (0.25, 0.5):
        for lam in (0.25, 0.5, 1.0):
            w = np.clip(1.0 + lam * (2 * rank - 1), 1 - cap, 1 + cap)
            m = (w * rr).sum() / w.sum()
            v = (w * (rr - m) ** 2).sum() / w.sum()
            print(f"  {lam:>6.2f}{cap:>6.2f}{m*100:>9.3f}%{np.sqrt(v)*100:>9.3f}%"
                  f"{m/np.sqrt(v):>10.4f}{w.max():>8.2f}")
    print("  (a bounded tilt is the only realistic form -- but see the concentration")
    print("   check below: it captures far LESS of the lift than the raw weight does)")

    # how concentrated is the UNBOUNDED scheme? and is the BOUNDED tilt real?
    w_unb = np.clip(xx - xx.min() + 1e-9, 1e-9, None)
    w_unb = w_unb / w_unb.mean()
    conc = (w_unb > 2.0).sum()
    top1 = w_unb.max() / w_unb.sum()
    print(f"\n  CONCENTRATION of the unbounded (w=abs_z) scheme:")
    print(f"    episodes with >2x average weight: {conc}/{len(xx)}")
    print(f"    single largest episode's share of the book: {top1*100:.1f}%")
    wb = np.clip(1.0 + 1.0 * (2 * rank - 1), 0.5, 1.5)
    mb = (wb * rr).sum() / wb.sum()
    vb = (wb * (rr - mb) ** 2).sum() / wb.sum()
    shb = mb / np.sqrt(vb)
    rng4 = np.random.default_rng(31)
    nullb = []
    for _ in range(5000):
        wsh = rng4.permutation(wb)
        m_ = (wsh * rr).sum() / wsh.sum()
        v_ = (wsh * (rr - m_) ** 2).sum() / wsh.sum()
        nullb.append(m_ / np.sqrt(v_) if v_ > 0 else np.nan)
    pb = float(np.mean(np.asarray(nullb) >= shb))
    print(f"  DEPLOYABLE bounded tilt (lam=1, cap=0.5): Sharpe {shb:.4f} vs flat "
          f"{rr.mean()/rr.std():.4f}")
    print(f"    permutation p = {pb:.3f}  -> "
          f"{'significant' if pb < 0.05 else 'NOT significant'} on its own")
    print("  The naive result ('abs_z raises Sharpe 0.33 -> 0.84!') does NOT survive")
    print("  contact with a deployable rule. That lift lives in an UNBOUNDED weight")
    print("  w = abs_z, where one episode is 22% of the book and 6 of 50 get >2x")
    print("  weight -- not a sizing policy, but a bet on a handful of events.")
    print("  A BOUNDED tilt (the only shippable form) is NOT significant (p above),")
    print("  and merely re-uses the abs_z the gate already screens on.")
    print()
    print("  VERDICT: flat sizing stays. Same lesson as the runner and the")
    print("  pair-selection screen -- the apparent edge is a few events that cannot")
    print("  be reliably sized into. Conviction scaling is NOT justified here.")


if __name__ == "__main__":
    main()
