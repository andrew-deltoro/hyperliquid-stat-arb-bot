"""
Reconcile state/positions.json against exchange positions.

For each config pair not in state but present on exchange, reconstruct
entry data (coint fit at entry time) and add to state.
"""
from __future__ import annotations
import os, sys, json, time, datetime
from pathlib import Path

import numpy as np
import yaml
from dotenv import load_dotenv

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
load_dotenv("/home/hlbot/hyperliquid-stat-arb-bot/.env")

from hyperliquid.info import Info
from hyperliquid.utils import constants
from src.hyperliquid_bot.data.ohlcv import aligned_close_prices
from src.hyperliquid_bot.phase3.paths import STATE_DIR

os.chdir("/home/hlbot/hyperliquid-stat-arb-bot")

with open("config/phase3-live.yaml") as f:
    cfg = yaml.safe_load(f)

PAIRS = [tuple(p) for p in cfg["pairs"]]
POS = cfg["position"]
FIT_WINDOW_HOURS = 30 * 24
STOP_MULT = float(POS["stop_mult"])
TARGET_FRAC = float(POS["target_frac"])
EXIT_MULT = float(POS["exit_horizon_mult"])

DRY_RUN = "--apply" not in sys.argv


def fit_coint_map(log_a, log_b):
    import pymc as pm
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


def reconstruct(a, b, entry_time_sec):
    end_ms = int(entry_time_sec * 1000)
    ts, ca, cb = aligned_close_prices(a, b, interval="1h", lookback_hours=200*24)
    ts_arr = np.array(ts)
    end_idx = int(np.searchsorted(ts_arr, end_ms, side="right"))
    if end_idx < FIT_WINDOW_HOURS:
        return None
    ca = np.array(ca)[:end_idx]
    cb = np.array(cb)[:end_idx]
    log_a = np.log(ca)
    log_b = np.log(cb)
    fit_start = end_idx - FIT_WINDOW_HOURS
    coint = fit_coint_map(log_a[fit_start:], log_b[fit_start:])
    if coint is None:
        return None
    spread = log_a[fit_start:] - coint["alpha"] - coint["beta"] * log_b[fit_start:]
    mean_raw = float(spread.mean())
    sigma_raw = float(spread.std())
    spread_c = spread - mean_raw
    entry_spread = float(spread_c[-1])
    hl = half_life(spread)
    z = entry_spread / sigma_raw if sigma_raw > 0 else 0.0
    return {
        "entry_spread": entry_spread,
        "entry_mean_raw": mean_raw,
        "entry_sigma_raw": sigma_raw,
        "entry_alpha": coint["alpha"],
        "beta_entry": coint["beta"],
        "half_life_hours": hl,
        "entry_z": z,
    }


def main():
    wallet = os.getenv("HL_ACCOUNT_ADDRESS_MAINNET")
    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    exch = info.user_state(wallet)
    exch_pos = {}
    for p in exch.get("assetPositions", []):
        pos = p["position"]
        szi = float(pos.get("szi", 0))
        if szi != 0:
            exch_pos[pos["coin"]] = {
                "szi": szi,
                "entry_px": float(pos.get("entryPx", 0)),
                "pos_value": float(pos.get("positionValue", 0)),
            }

    print(f"Exchange: {len(exch_pos)} legs")
    for c, p in exch_pos.items():
        print(f"  {c:>8} | szi={p['szi']:>12} | px={p['entry_px']:.4f} | val=${p['pos_value']:.2f}")
    print()

    state_path = STATE_DIR / "positions.json"
    with open(state_path) as f:
        state = json.load(f)
    open_pairs = {p["pair"] for p in state["positions"] if p.get("status") == "open"}
    print(f"State open pairs: {open_pairs}")
    print()

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (10 * 24 * 3600 * 1000)
    fills = info.user_fills_by_time(wallet, start_ms, end_ms)

    orphans = []
    for pair in PAIRS:
        a, b = pair[0], pair[1]
        key = f"{a}/{b}"
        if key in open_pairs:
            continue
        if a not in exch_pos or b not in exch_pos:
            continue
        fa = [f for f in fills if f.get("coin") == a]
        fb = [f for f in fills if f.get("coin") == b]
        if not fa or not fb:
            print(f"  {key}: no fill data — skip")
            continue
        t_a = min(int(f["time"]) for f in fa) / 1000.0
        t_b = min(int(f["time"]) for f in fb) / 1000.0
        entry_time = max(t_a, t_b)
        orphans.append({
            "pair": key, "a": a, "b": b,
            "entry_time": entry_time,
            "exch_a": exch_pos[a], "exch_b": exch_pos[b],
        })

    print(f"Orphans: {len(orphans)}")
    print()

    new_positions = []
    for o in orphans:
        a, b = o["a"], o["b"]
        dt = datetime.datetime.fromtimestamp(o["entry_time"], tz=datetime.timezone.utc)
        print(f"--- {o['pair']} (entry ~{dt.strftime('%Y-%m-%d %H:%M')} UTC) ---")
        recon = reconstruct(a, b, o["entry_time"])
        if recon is None:
            print(f"  reconstruction FAILED — skip")
            continue

        szi_a = o["exch_a"]["szi"]
        szi_b = o["exch_b"]["szi"]
        px_a = o["exch_a"]["entry_px"]
        px_b = o["exch_b"]["entry_px"]

        if szi_a > 0 and szi_b < 0:
            direction = "LONG_A_SHORT_B"
        elif szi_a < 0 and szi_b > 0:
            direction = "SHORT_A_LONG_B"
        else:
            print(f"  unclear direction — skip")
            continue

        leg_a_usd = abs(szi_a) * px_a
        leg_b_usd = abs(szi_b) * px_b

        # Balance filter: real pairs have leg notionals within 2.5x of each other.
        # Ratios outside [0.4, 2.5] mean this is either a false-positive pair
        # (legs belonging to other pairs) or a partial-fill orphan.
        if leg_a_usd == 0 or leg_b_usd == 0:
            print(f"  SKIP — zero notional on a leg")
            continue
        ratio = leg_a_usd / leg_b_usd
        if ratio < 0.4 or ratio > 2.5:
            print(f"  SKIP — unbalanced (leg_a=${leg_a_usd:.2f} leg_b=${leg_b_usd:.2f} ratio={ratio:.2f})")
            continue

        notional = max(leg_a_usd, leg_b_usd)

        ts_natural = dt + datetime.timedelta(hours=recon["half_life_hours"] * EXIT_MULT)
        ts_floor = datetime.datetime.now(tz=datetime.timezone.utc) + datetime.timedelta(hours=48)
        if ts_natural < ts_floor:
            ts_iso = ts_floor.isoformat()
            print(f"  note: natural time_stop was in the past; extending to now+48h")
        else:
            ts_iso = ts_natural.isoformat()

        new_positions.append({
            "pair": o["pair"],
            "coin_a": a,
            "coin_b": b,
            "direction": direction,
            "beta_entry": recon["beta_entry"],
            "entry_alpha": recon["entry_alpha"],
            "entry_mean_raw": recon["entry_mean_raw"],
            "entry_sigma_raw": recon["entry_sigma_raw"],
            "entry_spread": recon["entry_spread"],
            "entry_price_a": px_a,
            "entry_price_b": px_b,
            "entry_time_iso": dt.isoformat(),
            "half_life_hours": recon["half_life_hours"],
            "target_spread": recon["entry_spread"] * TARGET_FRAC,
            "stop_spread": recon["entry_spread"] * STOP_MULT,
            "time_stop_iso": ts_iso,
            "notional_usd": notional,
            "leg_a_usd": leg_a_usd,
            "leg_b_usd": leg_b_usd,
            "status": "open",
        })
        print(f"  dir={direction}  entry_spread={recon['entry_spread']:+.5f}  z={recon['entry_z']:+.2f}")
        print(f"  HL={recon['half_life_hours']:.1f}h  time_stop={ts_iso}")
        print(f"  notional=${notional:.2f}  leg_a=${leg_a_usd:.2f}  leg_b=${leg_b_usd:.2f}")
        print()

    print(f"Would add {len(new_positions)} positions")
    if DRY_RUN:
        print()
        print("=== DRY RUN — rerun with --apply to write ===")
        return

    state["positions"].extend(new_positions)
    # Backup then write
    bak = state_path.with_suffix(f".json.bak-{int(time.time())}")
    with open(bak, "w") as f:
        json.dump(state, f, indent=2)
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)
    print(f"Wrote {len(new_positions)} positions to {state_path}")


if __name__ == "__main__":
    main()
