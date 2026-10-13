"""One resimulation: the live beta-shift exit inside the trade-return model.

The production simulator draws the spread and exits on the cross, the
divergence stop, or the time stop. The live book also flattens when a
later refit of the hedge has moved more than 20% and more than 2
posterior standard deviations from the entry hedge. That path was not
in the expected return.

This script does not retune anything and does not change live sizing.
For each closed trade it:

- redraws the spread from the logged OU parameters (κ, σ, ν) and the
  entry spread, under the live exit policy
- redraws the hedge by block-bootstrapping that pair's own refit betas
  from reports/signals.csv (the overlapping-window estimates the bot
  actually published)
- closes on the first of: cross, divergence stop, time stop, beta-shift,
  in that order, checked once per cycle, matching PositionManager

The bootstrap uses the signallog betas only, not the trades' PnL.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

from hyperliquid_bot.phase3.config import load_config
from hyperliquid_bot.phase3.leverage import exit_policy_from_cfg

ROOT = Path("/home/hlbot/hyperliquid-stat-arb-bot")
LIVE = Path("/home/hlbot/hlbot-runtime/live/reports")
N_PATHS = 4000
SEED = 0


def _load_csv(path: Path) -> list[dict]:
    with path.open() as handle:
        return list(csv.DictReader(handle))


def _match_signal(trade: dict, signals: list[dict]) -> dict | None:
    entry = datetime.fromisoformat(trade["entry_time_iso"])
    best = None
    best_gap = None
    for row in signals:
        if row["pair"] != trade["pair"] or row["action"] != "OPEN":
            continue
        gap = abs((datetime.fromisoformat(row["timestamp_iso"]) - entry).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = row, gap
    if best is None or best_gap is None or best_gap > 6 * 3600:
        return None
    return best


def _simulate(
    s0: float,
    kappa: float,
    sigma: float,
    nu: float,
    beta: float,
    beta_sd: float,
    beta_series: np.ndarray,
    policy,
    n_paths: int,
    rng: np.random.Generator,
) -> dict:
    n_checks = max(1, int(np.ceil(policy.horizon_bars / policy.check_every_bars)))
    starts = rng.integers(0, max(len(beta_series) - 1, 1), size=n_paths)
    # Column 0 is the entry refit. Column k is k refits later.
    steps = np.arange(n_checks + 1)
    idx = np.clip(starts[:, None] + steps[None, :], 0, len(beta_series) - 1)
    # Change in the refit hedge from a random historical entry refit.
    beta_path = beta + (beta_series[idx] - beta_series[starts][:, None])

    k = np.full(n_paths, kappa)
    sig = np.full(n_paths, sigma)
    s = np.full(n_paths, s0)
    direction = 1.0 if s0 < 0 else -1.0
    stop_level = policy.stop_mult * s0
    cross_level = -policy.cross_frac * s0
    open_ = np.ones(n_paths, dtype=bool)
    exit_s = np.full(n_paths, np.nan)
    held = np.full(n_paths, policy.horizon_bars, dtype=float)
    reason = np.full(n_paths, "time", dtype=object)
    # Baseline ignores beta and is updated in parallel on the same spread draws.
    s_base = s.copy()
    open_base = open_.copy()
    exit_base = np.full(n_paths, np.nan)
    held_base = held.copy()
    reason_base = reason.copy()

    check_i = 0
    for t in range(1, policy.horizon_bars + 1):
        shock = rng.standard_t(nu, size=n_paths) * sig
        s = (1.0 - k) * s + shock
        s_base = (1.0 - k) * s_base + shock
        if t % policy.check_every_bars and t != policy.horizon_bars:
            continue
        take = direction * (s - cross_level) >= 0
        stop = direction * (s - stop_level) <= 0
        timed = np.full(n_paths, t == policy.horizon_bars)
        fresh = beta_path[:, min(check_i + 1, n_checks)]
        rel = np.abs(fresh - beta) / (abs(beta) + 1e-9)
        post = np.abs(fresh - beta) / (beta_sd + 1e-9)
        beta_hit = (rel > 0.20) & (post > 2.0)

        done = open_ & (take | stop | timed | beta_hit)
        why = np.where(take, "cross", np.where(stop, "stop", np.where(timed, "time", "beta_shift")))
        exit_s = np.where(done, s, exit_s)
        held = np.where(done, t, held)
        reason = np.where(done & open_, why, reason)
        open_ &= ~done

        take_b = direction * (s_base - cross_level) >= 0
        stop_b = direction * (s_base - stop_level) <= 0
        done_b = open_base & (take_b | stop_b | timed)
        why_b = np.where(take_b, "cross", np.where(stop_b, "stop", "time"))
        exit_base = np.where(done_b, s_base, exit_base)
        held_base = np.where(done_b, t, held_base)
        reason_base = np.where(done_b & open_base, why_b, reason_base)
        open_base &= ~done_b
        check_i += 1
        if not open_.any() and not open_base.any():
            break

    def pack(exit_spread, bars, why) -> dict:
        r = 0.5 * direction * (exit_spread - s0) - policy.cost_frac - policy.carry_frac_per_bar * bars
        counts = {name: float(np.mean(why == name)) for name in ("cross", "stop", "time", "beta_shift")}
        return {
            "exp_return": float(np.nanmean(r)),
            "p_win": float(np.mean(r > 0)),
            "mean_bars": float(np.mean(bars)),
            **counts,
        }

    return {"baseline": pack(exit_base, held_base, reason_base), "beta": pack(exit_s, held, reason)}


def main() -> None:
    cfg = load_config(ROOT / "config" / "phase3-live.yaml")
    signals = _load_csv(LIVE / "signals.csv")
    trades = _load_csv(LIVE / "trades.csv")
    by_pair: dict[str, list[dict]] = defaultdict(list)
    for row in signals:
        by_pair[row["pair"]].append(row)

    rng = np.random.default_rng(SEED)
    print(f"{'pair':22} {'base E[r]':>10} {'beta E[r]':>10} {'base p':>7} {'beta p':>7} "
          f"{'cross':>6} {'stop':>6} {'time':>6} {'beta':>6}")
    base_er, beta_er = [], []
    mixes = []
    for trade in trades:
        sig = _match_signal(trade, signals)
        if sig is None:
            print(f"{trade['pair']:22} no matching OPEN signal")
            continue
        series = np.array([float(row["beta"]) for row in by_pair[trade["pair"]]], dtype=float)
        if len(series) < 5:
            print(f"{trade['pair']:22} beta history too short")
            continue
        s0 = float(sig["current_spread"])
        half_life_bars = float(sig["half_life_hours"])  # 1h bars
        policy = exit_policy_from_cfg(
            cfg, half_life_bars=half_life_bars, beta=float(sig["beta"]), bar_hours=1.0,
        )
        out = _simulate(
            s0=s0,
            kappa=float(sig["kappa"]),
            sigma=float(sig["sigma_ou"]),
            nu=max(float(sig["nu_ou"]), 2.01),
            beta=float(sig["beta"]),
            beta_sd=float(sig["beta_sd"]),
            beta_series=series,
            policy=policy,
            n_paths=N_PATHS,
            rng=rng,
        )
        b, g = out["baseline"], out["beta"]
        base_er.append(b["exp_return"])
        beta_er.append(g["exp_return"])
        mixes.append(g)
        print(
            f"{trade['pair']:22} {b['exp_return']*1e4:9.1f}bp {g['exp_return']*1e4:9.1f}bp "
            f"{b['p_win']:7.2f} {g['p_win']:7.2f} "
            f"{g['cross']:6.2f} {g['stop']:6.2f} {g['time']:6.2f} {g['beta_shift']:6.2f}"
        )

    if not base_er:
        raise SystemExit("no trades resimulated")
    base = float(np.mean(base_er))
    beta = float(np.mean(beta_er))
    print(
        f"\n{len(base_er)} closed trades, {N_PATHS} paths each, seed {SEED}\n"
        f"mean baseline E[r] {base*1e4:+.1f} bp per trade   "
        f"P(r>0) {np.mean([1 if x>0 else 0 for x in base_er]):.2f} of trades still positive\n"
        f"mean with beta-shift {beta*1e4:+.1f} bp per trade   "
        f"P(r>0) {np.mean([1 if x>0 else 0 for x in beta_er]):.2f} of trades still positive\n"
        f"change {((beta-base)*1e4):+.1f} bp"
    )
    print(
        "mean exit mix under the beta-shift rule: "
        + "  ".join(
            f"{name} {np.mean([m[name] for m in mixes]):.2f}"
            for name in ("cross", "stop", "time", "beta_shift")
        )
    )


if __name__ == "__main__":
    main()
