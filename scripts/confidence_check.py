"""
Weekly pair health check.

For each pair in config/phase3-live.yaml, compute:
  - Engle-Granger cointegration p-value
  - Beta drift vs the earliest recorded entry for that pair
  - Half-life drift vs the same

Write results to state/pair_health.json with a per-pair status.
"""
from __future__ import annotations
import os, sys, json, time
from pathlib import Path
from datetime import datetime, timezone

# Match systemd runtime dir
os.environ.setdefault("HLBOT_RUNTIME_DIR", "/home/hlbot/hlbot-runtime/live")

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
sys.stdout.reconfigure(line_buffering=True)

import numpy as np
import yaml
import pymc as pm
from statsmodels.tsa.stattools import coint

from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
from src.hyperliquid_bot.phase3.paths import STATE_DIR

REPO = Path("/home/hlbot/hyperliquid-stat-arb-bot")
CONFIG = REPO / "config" / "phase3-live.yaml"
STATE_FILE = STATE_DIR / "positions.json"
OUT_FILE = STATE_DIR / "pair_health.json"

FIT_WINDOW_HOURS = 24 * 30
FETCH_LOOKBACK_HOURS = 24 * 200


def fit_coint_map(log_a, log_b):
    lb_mean = float(np.mean(log_b))
    lb_c = log_b - lb_mean
    with pm.Model() as model:
        a_c = pm.Normal("alpha_c", mu=0.0, sigma=5.0)
        beta = pm.Normal("beta", mu=1.0, sigma=1.0)
        sigma = pm.HalfNormal("sigma", sigma=0.5)
        nu = pm.Exponential("nu", lam=1/10.0)
        mu = a_c + beta * lb_c
        pm.StudentT("obs", nu=nu, mu=mu, sigma=sigma, observed=log_a)
        try:
            me = pm.find_MAP(progressbar=False, maxeval=500, method="L-BFGS-B")
        except Exception:
            return None
    alpha = float(me["alpha_c"]) - float(me["beta"]) * lb_mean
    return {"alpha": alpha, "beta": float(me["beta"])}


def half_life(spread):
    y = spread - spread.mean()
    lag = y[:-1]
    diff = np.diff(y)
    if lag.std() < 1e-9:
        return 999.0
    theta = float(np.polyfit(lag, diff, 1)[0])
    if theta >= 0:
        return 999.0
    return float(-np.log(2) / theta)


def eg_p_value(log_a, log_b):
    try:
        _, p, _ = coint(log_a, log_b, maxlag=24, autolag=None)
        return float(p)
    except Exception:
        return 1.0


def classify(eg_p, beta_drift, hl_drift):
    """
    Primary signals: half-life drift and beta drift — these reflect
    what the bot actually trades (mean-reversion of a linear combo).

    eg_p is retained as a secondary warning: it's slow and conservative
    on 30-day windows, so we only flag if it's egregiously bad.
    """
    worst = "green"

    def bump(status, new):
        order = {"green": 0, "yellow": 1, "orange": 2, "red": 3}
        return status if order[status] >= order[new] else new

    # Half-life drift: the most direct signal that reversion behavior changed
    if hl_drift > 3.0: worst = bump(worst, "red")
    elif hl_drift > 2.0: worst = bump(worst, "orange")
    elif hl_drift > 1.5: worst = bump(worst, "yellow")

    # Beta drift: hedge ratio instability
    if beta_drift > 0.30: worst = bump(worst, "red")
    elif beta_drift > 0.20: worst = bump(worst, "orange")
    elif beta_drift > 0.12: worst = bump(worst, "yellow")

    # EG p-value: only flag if dramatically bad (>0.5 is near-definitely
    # random-walk on 30d window; below that, ignore)
    if eg_p > 0.80: worst = bump(worst, "red")
    elif eg_p > 0.50: worst = bump(worst, "orange")

    return worst


def main():
    with open(CONFIG) as f:
        cfg = yaml.safe_load(f)
    pairs = [tuple(p) for p in cfg["pairs"]]

    # Load entry data from state (earliest recorded open for each pair)
    entry_ref = {}
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            state = json.load(f)
        for p in state.get("positions", []):
            pair = p.get("pair")
            if not pair: continue
            ts = p.get("entry_time_iso", "")
            if pair not in entry_ref or ts < entry_ref[pair]["entry_time_iso"]:
                entry_ref[pair] = {
                    "beta_entry": p.get("beta_entry"),
                    "half_life_hours": p.get("half_life_hours"),
                    "entry_time_iso": ts,
                }

    results = {}
    import time
    for idx, (a, b) in enumerate(pairs):
        pair = f"{a}/{b}"
        # Sleep between pairs to avoid HL rate limits
        if idx > 0:
            time.sleep(2.5)
        print(f"--- {pair} ---", flush=True)
        try:
            ts, ca, cb = aligned_close_prices(a, b, interval="1h",
                                              lookback_hours=FETCH_LOOKBACK_HOURS)
        except Exception as e:
            print(f"  fetch failed: {e}", flush=True)
            continue
        if len(ts) < FIT_WINDOW_HOURS:
            print(f"  insufficient: {len(ts)} bars", flush=True)
            continue

        log_a = np.log(np.array(ca))[-FIT_WINDOW_HOURS:]
        log_b = np.log(np.array(cb))[-FIT_WINDOW_HOURS:]

        coint_result = fit_coint_map(log_a, log_b)
        if coint_result is None:
            print(f"  fit failed", flush=True)
            continue

        spread = log_a - coint_result["alpha"] - coint_result["beta"] * log_b
        current_hl = half_life(spread)
        current_beta = coint_result["beta"]

        eg_p = eg_p_value(log_a, log_b)

        ref = entry_ref.get(pair)
        if ref and ref["beta_entry"] and ref["half_life_hours"]:
            beta_drift = abs(current_beta - ref["beta_entry"]) / abs(ref["beta_entry"])
            hl_drift = current_hl / ref["half_life_hours"] if ref["half_life_hours"] > 0 else 999
            status = classify(eg_p, beta_drift, hl_drift)
            verified = True
        else:
            # No live entry reference — we cannot compute drift, and eg_p
            # alone is too conservative to flag. Mark unverified.
            beta_drift = None
            hl_drift = None
            status = "unverified"
            verified = False
        results[pair] = {
            "status": status,
            "verified": verified,
            "eg_p": round(eg_p, 4),
            "beta_current": round(current_beta, 4),
            "beta_entry_ref": round(ref["beta_entry"], 4) if ref and ref.get("beta_entry") else None,
            "beta_drift": round(beta_drift, 4) if beta_drift is not None else None,
            "current_hl": round(current_hl, 2),
            "entry_hl_ref": round(ref["half_life_hours"], 2) if ref and ref.get("half_life_hours") else None,
            "hl_drift": round(hl_drift, 3) if hl_drift is not None else None,
        }
        if verified:
            print(f"  status={status:>7}  eg_p={eg_p:.3f}  beta={current_beta:.3f}  drift={beta_drift:.2%}  hl={current_hl:.1f}h  hl_drift={hl_drift:.2f}x", flush=True)
        else:
            print(f"  status={status:>7}  eg_p={eg_p:.3f}  beta={current_beta:.3f}  hl={current_hl:.1f}h  (no live reference)", flush=True)

    out = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "pairs": results,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print()
    print(f"Wrote {OUT_FILE}")

    # Summary
    print()
    print("=== Summary ===")
    by_status = {}
    for pair, r in results.items():
        by_status.setdefault(r["status"], []).append(pair)
    for st in ["red", "orange", "yellow", "green"]:
        if st in by_status:
            print(f"  {st:>7}: {', '.join(by_status[st])}")


if __name__ == "__main__":
    main()
