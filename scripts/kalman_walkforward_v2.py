"""
Walk-forward PnL comparison v2: Kalman beta vs static OLS beta.

Matches live bot exit rules:
  Entry:      |z| > threshold (asymmetric: wide=1.2, tight=0.9)
  Cross exit: spread reaches -cross_frac * entry_spread on opposite side
  Div stop:   spread reaches  stop_mult * entry_spread on same side
  Time stop:  half_life_hours * horizon_mult

Fees modeled as round-trip bps subtracted from each trade's log-spread PnL.

Usage:
  # Crypto pair (maker fees, short half-life)
  python scripts/kalman_walkforward_v2.py SNX DYDX --fee-bps 6 --half-life-hours 24

  # HIP-3 semis pair (taker only, longer half-life)
  python scripts/kalman_walkforward_v2.py xyz:MU xyz:TSM --fee-bps 30 --half-life-hours 100
"""
from __future__ import annotations
import argparse
import sys, time
sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")

import numpy as np
import statsmodels.api as sm

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
from src.hyperliquid_bot.pairs.kalman import KalmanBeta


def fit_ols(log_a, log_b):
    X = sm.add_constant(log_b, has_constant="add")
    try:
        m = sm.OLS(log_a, X).fit()
        return {"alpha": float(m.params[0]), "beta": float(m.params[1])}
    except Exception:
        return None


def rolling_z(spread, window):
    z = np.full(len(spread), np.nan)
    for i in range(window, len(spread)):
        w = spread[i-window:i]
        m = w.mean()
        s = w.std() + 1e-9
        z[i] = (spread[i] - m) / s
    return z


def simulate_trades(spread, z, entry_wide, entry_tight,
                    cross_frac, stop_mult, max_hold, fee_bps_rt):
    """Entry/exit rules matching live cross-exit mode."""
    trades = []
    pos = None
    fee_cost = fee_bps_rt / 10000.0
    for i in range(len(z)):
        zi = z[i]
        si = spread[i]
        if np.isnan(zi):
            continue
        if pos is None:
            thresh = entry_wide if zi > 0 else entry_tight
            if abs(zi) >= thresh:
                side = -1 if zi > 0 else 1   # SHORT_A_LONG_B if z>0, else LONG_A_SHORT_B
                pos = {
                    "entry_i": i,
                    "entry_z": zi,
                    "entry_spread": si,
                    "side": side,
                    "cross_target": -cross_frac * si,
                    "stop_target": stop_mult * si,
                }
        else:
            hold = i - pos["entry_i"]
            cross_t = pos["cross_target"]
            stop_t = pos["stop_target"]
            if pos["side"] == 1:      # LONG_A_SHORT_B (entry z<0, entry spread<0)
                hit_cross = si >= cross_t
                hit_stop = si <= stop_t
            else:                     # SHORT_A_LONG_B (entry z>0, entry spread>0)
                hit_cross = si <= cross_t
                hit_stop = si >= stop_t
            hit_time = hold >= max_hold
            if hit_cross or hit_stop or hit_time:
                pnl_gross = pos["side"] * (si - pos["entry_spread"])
                pnl_net = pnl_gross - fee_cost
                trades.append({
                    "entry_i": pos["entry_i"],
                    "exit_i": i,
                    "hold": hold,
                    "entry_z": pos["entry_z"],
                    "exit_z": zi,
                    "pnl_gross": pnl_gross,
                    "pnl_net": pnl_net,
                    "reason": "cross" if hit_cross else
                              ("divergence" if hit_stop else "time"),
                })
                pos = None
    return trades


def summarize(trades, label):
    if not trades:
        print(f"  {label}: 0 trades")
        return
    pnls_g = np.array([t["pnl_gross"] for t in trades])
    pnls_n = np.array([t["pnl_net"] for t in trades])
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    print(f"  {label}:")
    print(f"    n={len(trades)}  gross_total={pnls_g.sum():+.5f}  "
          f"gross_mean={pnls_g.mean():+.5f}")
    print(f"    net_total={pnls_n.sum():+.5f}  net_mean={pnls_n.mean():+.5f}  "
          f"win%={100*(pnls_n > 0).mean():.1f}")
    if pnls_n.std() > 0:
        print(f"    net_sharpe={pnls_n.mean()/pnls_n.std():+.3f}  "
              f"avg_hold={np.mean([t['hold'] for t in trades]):.1f}h")
    print(f"    reasons: {reasons}")
    wide = [t for t in trades if t["entry_z"] > 0]
    tight = [t for t in trades if t["entry_z"] < 0]
    if wide:
        w = np.array([t["pnl_net"] for t in wide])
        print(f"    wide(z>0):  n={len(wide):>3}  mean={w.mean():+.5f}  "
              f"win%={100*(w > 0).mean():.1f}")
    if tight:
        tg = np.array([t["pnl_net"] for t in tight])
        print(f"    tight(z<0): n={len(tight):>3}  mean={tg.mean():+.5f}  "
              f"win%={100*(tg > 0).mean():.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("coin_a", nargs="?", default="SNX")
    ap.add_argument("coin_b", nargs="?", default="DYDX")
    ap.add_argument("--half-life-hours", type=float, default=24.0)
    ap.add_argument("--horizon-mult", type=float, default=3.33)
    ap.add_argument("--fee-bps", type=float, default=6.0)
    ap.add_argument("--entry-wide", type=float, default=1.2)
    ap.add_argument("--entry-tight", type=float, default=0.9)
    ap.add_argument("--cross-frac", type=float, default=0.75)
    ap.add_argument("--stop-mult", type=float, default=3.0)
    ap.add_argument("--lookback", type=int, default=720)
    ap.add_argument("--train", type=int, default=60 * 24)
    ap.add_argument("--refit-every", type=int, default=4)
    ap.add_argument("--kalman-q", type=float, default=1e-5)
    ap.add_argument("--kalman-r", type=float, default=1e-4)
    args = ap.parse_args()

    max_hold = int(args.half_life_hours * args.horizon_mult)

    print(f"=== Kalman walk-forward v2: {args.coin_a}/{args.coin_b} ===")
    print(f"  half-life={args.half_life_hours}h  ->  max_hold={max_hold} bars")
    print(f"  entry: wide={args.entry_wide} tight={args.entry_tight}")
    print(f"  exit:  cross={args.cross_frac}  stop={args.stop_mult}")
    print(f"  fees:  {args.fee_bps} bps round-trip")
    print()

    ts, ca, cb = aligned_close_prices(
        args.coin_a, args.coin_b, interval="1h", lookback_hours=24 * 180,
    )
    log_a = np.log(np.array(ca))
    log_b = np.log(np.array(cb))
    n = len(log_a)
    print(f"Data: {n} bars (~{n//24} days)")

    TRAIN = args.train
    LOOKBACK = args.lookback
    REFIT = args.refit_every

    if n < TRAIN + LOOKBACK + 200:
        print(f"Not enough data. Need {TRAIN + LOOKBACK + 200} bars.")
        return

    # --- Static OLS: refit every REFIT bars, freeze between ---
    print(f"Fitting static OLS (refit every {REFIT}h)...", flush=True)
    t0 = time.time()
    static_beta = np.full(n, np.nan)
    static_alpha = np.full(n, np.nan)
    for i in range(TRAIN, n, REFIT):
        if i < LOOKBACK:
            continue
        s = fit_ols(log_a[i - LOOKBACK:i], log_b[i - LOOKBACK:i])
        if s is None:
            continue
        end = min(i + REFIT, n)
        static_beta[i:end] = s["beta"]
        static_alpha[i:end] = s["alpha"]
    print(f"  done in {time.time()-t0:.1f}s")

    # --- Kalman: recursive every bar ---
    print("Running Kalman...", flush=True)
    t0 = time.time()
    init = fit_ols(log_a[:TRAIN], log_b[:TRAIN])
    kf = KalmanBeta(beta_init=init["beta"], alpha_init=init["alpha"],
                    q=args.kalman_q, r=args.kalman_r)
    for i in range(TRAIN):
        kf.update(float(log_a[i]), float(log_b[i]))
    kalman_beta = np.full(n, np.nan)
    kalman_alpha = np.full(n, np.nan)
    for i in range(TRAIN, n):
        st = kf.update(float(log_a[i]), float(log_b[i]))
        kalman_beta[i] = st.beta
        kalman_alpha[i] = st.alpha
    print(f"  done in {time.time()-t0:.1f}s\n")

    # --- Spread series ---
    spread_static = log_a - static_beta * log_b - static_alpha
    spread_kalman = log_a - kalman_beta * log_b - kalman_alpha

    # --- Rolling z over test window ---
    z_static = rolling_z(spread_static, LOOKBACK)
    z_kalman = rolling_z(spread_kalman, LOOKBACK)

    valid_start = max(TRAIN, LOOKBACK + 1)
    idx = np.arange(valid_start, n)

    z_static_test = z_static[idx]
    z_kalman_test = z_kalman[idx]
    sp_static_test = spread_static[idx]
    sp_kalman_test = spread_kalman[idx]

    # --- Simulate ---
    print("=== Trade simulation ===")
    static_trades = simulate_trades(
        sp_static_test, z_static_test,
        args.entry_wide, args.entry_tight,
        args.cross_frac, args.stop_mult,
        max_hold, args.fee_bps,
    )
    kalman_trades = simulate_trades(
        sp_kalman_test, z_kalman_test,
        args.entry_wide, args.entry_tight,
        args.cross_frac, args.stop_mult,
        max_hold, args.fee_bps,
    )

    summarize(static_trades, "Static OLS")
    print()
    summarize(kalman_trades, "Kalman")
    print()

    # --- Verdict ---
    if not static_trades or not kalman_trades:
        print("Insufficient trades for verdict")
        return
    sp = np.array([t["pnl_net"] for t in static_trades])
    kp = np.array([t["pnl_net"] for t in kalman_trades])
    print("=== Verdict ===")
    s_sharpe = sp.mean() / sp.std() if sp.std() > 0 else 0
    k_sharpe = kp.mean() / kp.std() if kp.std() > 0 else 0
    print(f"  static: n={len(sp):>4}  net_total={sp.sum():+.5f}  "
          f"net_mean={sp.mean():+.5f}  sharpe={s_sharpe:+.3f}")
    print(f"  kalman: n={len(kp):>4}  net_total={kp.sum():+.5f}  "
          f"net_mean={kp.mean():+.5f}  sharpe={k_sharpe:+.3f}")
    if sp.sum() != 0:
        print(f"  kalman/static total ratio: {kp.sum()/sp.sum():.2f}x")
    print()
    if kp.sum() > 0 and sp.sum() > 0:
        if kp.sum() > 1.5 * sp.sum() and k_sharpe > 1.2 * s_sharpe:
            print("  -> Kalman substantially better")
        elif kp.sum() > sp.sum():
            print("  -> Kalman marginally better")
        elif kp.sum() > 0:
            print("  -> Kalman loses on total but is profitable")
        else:
            print("  -> Kalman net-unprofitable at this fee level")


if __name__ == "__main__":
    main()
