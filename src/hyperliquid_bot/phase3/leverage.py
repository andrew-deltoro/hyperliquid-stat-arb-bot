"""
Posterior-predictive trade returns and leverage sizing.

Notation: a pair position has notional N, legs A = N/2 and B = beta * N/2.
To first order its PnL is (N/2) * dS where S = log A - alpha - beta log B,
so the per-unit-notional return of a trade is r = 0.5 * dir * (S_exit - S_0)
minus round-trip costs and carry.

1. simulate_trade_returns: draws r from the OU posterior under the exit
   rules the bot actually runs (cross / trail / target, divergence stop,
   time stop), checking exits only once per cycle so moves past the stop
   between cycles are included (Statistical Rethinking §3.3: decisions from
   the posterior predictive distribution, not from point estimates).
2. kelly_fraction: f maximizing E_post[log(1 + f r)] (Bayesian Kelly).
3. calibration_factor: partial pooling of the model's predicted edge with
   the realized edge of closed trades, under a skeptical prior (Ch. 13).
4. size_trade: fractional Kelly, split across correlated concurrent
   positions, capped per trade, by portfolio gross leverage, and by a joint
   stress test against maintenance margin per dex.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


@dataclass
class ExitPolicy:
    exit_mode: str = "cross"         # "cross", "trail", or "target"
    cross_frac: float = 0.75
    target_frac: float = 0.0
    trail_frac: float = 0.25
    trail_activation_frac: float = 0.5
    stop_mult: float = 3.0
    horizon_bars: int = 72
    check_every_bars: int = 4
    cost_frac: float = 0.0           # round-trip costs as a fraction of N
    carry_frac_per_bar: float = 0.0  # funding/borrow as a fraction of N per bar


@dataclass
class TradeDistribution:
    returns: np.ndarray
    exp_return: float
    p_win: float
    stress_loss_frac: float          # |quantile| of losses used for stress sizing
    mean_bars_held: float


def exit_policy_from_cfg(cfg, half_life_bars: float, beta: float,
                         bar_hours: float) -> ExitPolicy:
    pos = cfg.position
    fees = cfg.raw.get("fees", {}) or {}
    lev = cfg.raw.get("leverage", {}) or {}
    fee = float(fees.get("default_fee_pct", 0.00045))
    slip = float(fees.get("slippage_bps", 3.0)) / 1e4
    # Round trip on gross legs N/2 * (1 + |beta|), as in journal.compute_pnl.
    cost_frac = 2.0 * (fee + slip) * (1.0 + abs(beta)) / 2.0
    carry = float(lev.get("funding_bps_per_hour", 0.0)) / 1e4 * bar_hours
    carry_frac = carry * (1.0 + abs(beta)) / 2.0

    if pos.get("exit_mode", "trail") == "cross":
        mode = "cross"
    elif pos.get("use_trailing_stop", False):
        mode = "trail"
    else:
        mode = "target"

    cycle_hours = float(lev.get("cycle_hours", 4.0))
    return ExitPolicy(
        exit_mode=mode,
        cross_frac=float(pos.get("cross_exit_frac", 0.75)),
        target_frac=float(pos.get("target_frac", 0.5)),
        trail_frac=float(pos.get("trail_frac", 0.25)),
        trail_activation_frac=float(pos.get("trail_activation_frac", 0.5)),
        stop_mult=float(pos.get("stop_mult", 3.0)),
        horizon_bars=max(1, int(math.ceil(
            half_life_bars * float(pos.get("exit_horizon_mult", 3.0))))),
        check_every_bars=max(1, int(round(cycle_hours / bar_hours))),
        cost_frac=cost_frac,
        carry_frac_per_bar=carry_frac,
    )


def simulate_trade_returns(
    current_spread: float | np.ndarray,
    kappa_post: np.ndarray,
    sigma_post: np.ndarray,
    nu_post: np.ndarray,
    policy: ExitPolicy,
    paths_per_draw: int = 4,
    max_paths: int = 20000,
    stress_quantile: float = 0.01,
    seed: int | None = 0,
) -> TradeDistribution:
    """
    Simulate net trade returns under `policy`.

    `current_spread` may be a scalar or a per-posterior-draw array. An array
    propagates beta (and other) uncertainty into the entry level, stop, and
    target — required when the OU and cointegrating residual are fit jointly.
    """
    rng = np.random.default_rng(seed)
    kappa_post = np.asarray(kappa_post, dtype=float)
    n_draws = len(kappa_post)
    s0_draws = np.asarray(current_spread, dtype=float)
    if s0_draws.ndim == 0:
        s0_draws = np.full(n_draws, float(s0_draws))
    if len(s0_draws) != n_draws:
        raise ValueError("current_spread length must match posterior draws")

    idx = np.tile(np.arange(n_draws), paths_per_draw)
    if len(idx) > max_paths:
        idx = rng.choice(idx, size=max_paths, replace=False)
    k = kappa_post[idx]
    sig = np.asarray(sigma_post, dtype=float)[idx]
    nu = np.asarray(nu_post, dtype=float)[idx]
    s0 = s0_draws[idx]
    n = len(idx)

    # Trade direction from the posterior-mean entry spread (same rule as
    # signals.direction); paths keep their own s0 for exits.
    mean_s0 = float(s0_draws.mean())
    direction = 1.0 if mean_s0 < 0 else -1.0
    s = s0.copy()
    exit_s = np.full(n, np.nan)
    held = np.full(n, policy.horizon_bars, dtype=float)
    open_ = np.ones(n, dtype=bool)
    best = s0.copy()

    stop_level = policy.stop_mult * s0
    cross_level = -policy.cross_frac * s0
    target_level = policy.target_frac * s0
    trail_dist = policy.trail_frac * np.abs(s0)
    activation = policy.trail_activation_frac * np.abs(s0)

    for t in range(1, policy.horizon_bars + 1):
        s = (1.0 - k) * s + rng.standard_t(nu) * sig
        if policy.exit_mode == "trail":
            best = np.where(direction * (s - best) > 0, s, best)
        if t % policy.check_every_bars and t != policy.horizon_bars:
            continue

        # Same order as PositionManager.evaluate_open: profit exit, stop, time.
        if policy.exit_mode == "cross":
            take = direction * (s - cross_level) >= 0
        elif policy.exit_mode == "trail":
            activated = direction * (s - s0) >= activation
            take = activated & (direction * (best - s) > trail_dist)
        else:
            take = direction * (s - target_level) >= 0
        stop = direction * (s - stop_level) <= 0
        done = open_ & (take | stop | (t == policy.horizon_bars))
        exit_s[done] = s[done]
        held[done] = t
        open_ &= ~done
        if not open_.any():
            break

    r = (0.5 * direction * (exit_s - s0)
         - policy.cost_frac
         - policy.carry_frac_per_bar * held)
    q = float(np.quantile(r, stress_quantile))
    return TradeDistribution(
        returns=r,
        exp_return=float(r.mean()),
        p_win=float((r > 0).mean()),
        stress_loss_frac=max(0.0, -q),
        mean_bars_held=float(held.mean()),
    )


def kelly_fraction(returns: np.ndarray, f_max: float = 50.0) -> float:
    """argmax_f mean(log(1 + f r)) over f >= 0. Zero if E[r] <= 0."""
    r = np.asarray(returns, dtype=float)
    if len(r) == 0 or r.mean() <= 0:
        return 0.0
    worst = r.min()
    hi = f_max if worst >= 0 else min(f_max, 0.999 / -worst)

    def growth(f: float) -> float:
        return float(np.mean(np.log1p(f * r)))

    # The objective is concave in f: golden-section search.
    lo, phi = 0.0, (math.sqrt(5) - 1) / 2
    a, b = lo + (1 - phi) * (hi - lo), lo + phi * (hi - lo)
    ga, gb = growth(a), growth(b)
    for _ in range(80):
        if ga < gb:
            lo, a, ga = a, b, gb
            b = lo + phi * (hi - lo)
            gb = growth(b)
        else:
            hi, b, gb = b, a, ga
            a = lo + (1 - phi) * (hi - lo)
            ga = growth(a)
    f = (lo + hi) / 2
    return f if growth(f) > 0 else 0.0


def calibration_factor(predicted: list[float], realized: list[float],
                       prior_mean: float = 0.5, prior_sd: float = 0.25) -> float:
    """
    Posterior mean of c in realized_i ~ Normal(c * predicted_i, s), with
    c ~ Normal(prior_mean, prior_sd) and s the realized residual scale.
    Clipped to [0, 1]: evidence can shrink the model's edge, never inflate it.
    """
    x = np.asarray(predicted, dtype=float)
    y = np.asarray(realized, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if len(x) < 2:
        return float(np.clip(prior_mean, 0.0, 1.0))
    s2 = max(float(np.var(y - prior_mean * x)), 1e-8)
    prec = 1.0 / prior_sd**2 + float((x * x).sum()) / s2
    mean = (prior_mean / prior_sd**2 + float((x * y).sum()) / s2) / prec
    return float(np.clip(mean, 0.0, 1.0))


def shrink_returns(returns: np.ndarray, c: float) -> np.ndarray:
    """Scale the predicted edge by c while keeping the dispersion."""
    r = np.asarray(returns, dtype=float)
    return r - r.mean() + c * r.mean()


@dataclass
class BookExposure:
    """Current open book on one dex, in USD."""
    equity: float
    gross: float = 0.0               # sum of |leg notionals|
    maintenance: float = 0.0         # sum of gross_leg * maintenance_frac
    stress_loss: float = 0.0         # sum of N_i * stress_loss_frac_i


@dataclass
class SizingDecision:
    notional: float
    kelly_f: float
    calibration: float
    binding: str
    detail: dict = field(default_factory=dict)


def size_trade(
    dist: TradeDistribution,
    beta: float,
    book: BookExposure,
    account_gross: float,
    account_equity: float,
    maint_frac_a: float,
    maint_frac_b: float,
    calibration: float,
    cfg_lev: dict,
) -> SizingDecision:
    """Pair notional N for a new trade. Every cap is logged as `binding`."""
    frac_kelly = float(cfg_lev.get("kelly_fraction", 0.25))
    # Prefer a fitted ρ from portfolio.load_portfolio_stats when provided.
    rho = float(cfg_lev.get("pair_correlation",
                            cfg_lev.get("assumed_pair_correlation", 0.3)))
    k_conc = int(cfg_lev.get("max_concurrent_positions", 10))
    max_trade_frac = float(cfg_lev.get("max_trade_notional_frac", 0.5))
    max_gross_lev = float(cfg_lev.get("max_gross_leverage", 2.0))
    safety = float(cfg_lev.get("stress_margin_buffer", 1.5))

    r_adj = shrink_returns(dist.returns, calibration)
    f_star = kelly_fraction(r_adj)
    # K equicorrelated bets of equal size: the growth-optimal size of each
    # is f*/(1 + (K-1) rho) (multivariate Kelly with a common correlation).
    f_bet = frac_kelly * f_star / (1.0 + (k_conc - 1) * rho)

    gross_per_n = (1.0 + abs(beta)) / 2.0
    mm_per_n = 0.5 * maint_frac_a + 0.5 * abs(beta) * maint_frac_b

    caps = {
        "kelly": f_bet * account_equity,
        "per_trade": max_trade_frac * account_equity,
        "gross_leverage": max(0.0, (max_gross_lev * account_equity - account_gross) / gross_per_n),
    }
    # Joint stress on this dex: every open position takes its stress loss at
    # once, and the remaining equity must still cover maintenance margin
    # with a buffer.
    per_n = dist.stress_loss_frac + safety * mm_per_n
    room = book.equity - book.stress_loss - safety * book.maintenance
    caps["liquidation_stress"] = max(0.0, room / per_n) if per_n > 0 else float("inf")

    binding = min(caps, key=caps.get)
    notional = max(0.0, caps[binding])
    return SizingDecision(
        notional=notional, kelly_f=f_star, calibration=calibration,
        binding=binding,
        detail={k: round(v, 2) for k, v in caps.items()} | {"f_bet": round(f_bet, 4)},
    )
