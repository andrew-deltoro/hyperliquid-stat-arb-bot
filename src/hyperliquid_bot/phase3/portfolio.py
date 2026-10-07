"""
Portfolio-level leverage inputs (audit §3 step 3).

1. Cross-pair correlation ρ fitted from the panel of per-cycle signal
   features (default: exp_return), shrunk toward a prior — replaces a
   fixed assumed_pair_correlation in the Kelly split.
2. Partial pooling of the calibration factor c across pairs (Ch. 13):
   each pair has its own posterior mean of realized/predicted edge,
   shrunk toward the population c when the pair has few closed trades.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .leverage import calibration_factor
from .paths import REPORTS_DIR

SIGNALS_FILE = REPORTS_DIR / "signals.csv"
TRADES_FILE = REPORTS_DIR / "trades.csv"


@dataclass
class PortfolioStats:
    rho: float
    rho_raw: float
    n_pair_obs: int
    calibration_global: float
    calibration_by_pair: dict[str, float]
    n_trades_global: int
    detail: str = ""


def _read_signal_panel(path: Path, feature: str = "exp_return",
                       max_cycles: int = 200) -> dict[str, dict[str, float]]:
    """pair -> {timestamp: feature} for the most recent max_cycles stamps."""
    if not path.exists():
        return {}
    by_ts: dict[str, dict[str, float]] = defaultdict(dict)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                val = float(row.get(feature) or "nan")
            except ValueError:
                continue
            if val != val:
                continue
            ts = row.get("timestamp_iso", "")
            pair = row.get("pair", "")
            if ts and pair:
                by_ts[ts][pair] = val
    stamps = sorted(by_ts)[-max_cycles:]
    panel: dict[str, dict[str, float]] = defaultdict(dict)
    for ts in stamps:
        for pair, val in by_ts[ts].items():
            panel[pair][ts] = val
    return panel


def fit_cross_pair_correlation(
    path: Path | None = None,
    feature: str = "exp_return",
    prior_rho: float = 0.3,
    prior_strength: float = 20.0,
    min_overlap: int = 8,
    max_cycles: int = 200,
) -> tuple[float, float, int]:
    """
    Mean pairwise correlation of `feature` across pairs, with Bayesian
    shrinkage toward prior_rho. Returns (rho_shrunk, rho_raw, n_pair_obs).
    """
    panel = _read_signal_panel(path or SIGNALS_FILE, feature=feature,
                               max_cycles=max_cycles)
    pairs = sorted(panel)
    if len(pairs) < 2:
        return prior_rho, prior_rho, 0

    # Align on the union of timestamps; require pairwise overlap.
    corrs = []
    for i, a in enumerate(pairs):
        for b in pairs[i + 1:]:
            shared = sorted(set(panel[a]) & set(panel[b]))
            if len(shared) < min_overlap:
                continue
            xa = np.array([panel[a][t] for t in shared])
            xb = np.array([panel[b][t] for t in shared])
            if xa.std() < 1e-12 or xb.std() < 1e-12:
                continue
            corrs.append(float(np.corrcoef(xa, xb)[0, 1]))
    if not corrs:
        return prior_rho, prior_rho, 0
    raw = float(np.clip(np.mean(corrs), -0.99, 0.99))
    # Keep ρ ≥ 0 for the equicorrelation Kelly split (negative would
    # increase size, which we refuse without a full covariance model).
    raw = max(0.0, raw)
    n = float(len(corrs))
    shrunk = (n * raw + prior_strength * prior_rho) / (n + prior_strength)
    return float(np.clip(shrunk, 0.0, 0.99)), raw, int(n)


def _trade_rows(path: Path | None = None) -> list[dict]:
    path = path or TRADES_FILE
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def pooled_calibrations(
    prior_mean: float = 0.5,
    prior_sd: float = 0.25,
    pair_prior_strength: float = 4.0,
    path: Path | None = None,
) -> tuple[float, dict[str, float], int]:
    """
    Population c and per-pair c_p.

    c_p = (n_p * c_local + m * c_global) / (n_p + m), with m = pair_prior_strength.
    Pairs with no trades inherit c_global.
    """
    rows = _trade_rows(path)
    pred, real, pair_ids = [], [], []
    for row in rows:
        # Pre-E[r] journal rows have a blank prediction; skip them so c
        # stays on the prior instead of inventing a p_revert proxy.
        raw_p = (row.get("exp_return_at_entry") or "").strip()
        if not raw_p:
            continue
        try:
            p = float(raw_p)
            r = float(row.get("pnl_pct") or "nan")
        except ValueError:
            continue
        if p == p and r == r:
            pred.append(p)
            real.append(r)
            pair_ids.append(row.get("pair", ""))

    c_global = calibration_factor(pred, real, prior_mean=prior_mean, prior_sd=prior_sd)
    by_pair: dict[str, float] = {}
    buckets: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for p, r, pair in zip(pred, real, pair_ids):
        if pair:
            buckets[pair].append((p, r))
    for pair, pts in buckets.items():
        xs = [a for a, _ in pts]
        ys = [b for _, b in pts]
        c_local = calibration_factor(xs, ys, prior_mean=c_global, prior_sd=prior_sd)
        n = len(pts)
        by_pair[pair] = (
            (n * c_local + pair_prior_strength * c_global)
            / (n + pair_prior_strength)
        )
    return c_global, by_pair, len(pred)


def load_portfolio_stats(cfg_lev: dict) -> PortfolioStats:
    prior_rho = float(cfg_lev.get("assumed_pair_correlation", 0.3))
    prior_strength = float(cfg_lev.get("rho_prior_strength", 20.0))
    feature = str(cfg_lev.get("rho_feature", "exp_return"))
    rho, rho_raw, n_obs = fit_cross_pair_correlation(
        prior_rho=prior_rho, prior_strength=prior_strength, feature=feature,
    )
    c_g, c_by, n_tr = pooled_calibrations(
        prior_mean=float(cfg_lev.get("calibration_prior_mean", 0.5)),
        prior_sd=float(cfg_lev.get("calibration_prior_sd", 0.25)),
        pair_prior_strength=float(cfg_lev.get("pair_calibration_strength", 4.0)),
    )
    n_missing = sum(
        1 for r in _trade_rows()
        if not (r.get("exp_return_at_entry") or "").strip()
    )
    detail = (f"ρ={rho:.2f} (raw {rho_raw:.2f}, n_pairs={n_obs}) "
              f"c={c_g:.2f} (n_trades={n_tr}, pairs={len(c_by)})")
    if n_missing:
        detail += f" missing_exp_return={n_missing}"
    return PortfolioStats(
        rho=rho, rho_raw=rho_raw, n_pair_obs=n_obs,
        calibration_global=c_g, calibration_by_pair=c_by,
        n_trades_global=n_tr,
        detail=detail,
    )


def calibration_for_pair(stats: PortfolioStats, pair: str) -> float:
    return stats.calibration_by_pair.get(pair, stats.calibration_global)
