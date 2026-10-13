#!/usr/bin/env python3
"""Robust Bayesian sensitivity analysis for oracle-convergence positions.

This is explicitly supplementary. It does not replace the locked cohort-level
verdict in oracle_convergence_score.py.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import arviz as az
import numpy as np
import pymc as pm
import yaml

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "oracle_convergence_hypothesis.yaml"


def interval(values: np.ndarray) -> list[float]:
    return [float(value) for value in np.quantile(values, [0.05, 0.95])]


def fit_model(rows: list[dict], draws: int, tune: int, seed: int) -> dict:
    coins = sorted({row["coin"] for row in rows})
    cohorts = sorted({row["cohort_hour"] for row in rows})
    coin_map = {coin: index for index, coin in enumerate(coins)}
    cohort_map = {cohort: index for index, cohort in enumerate(cohorts)}
    coin_idx = np.asarray([coin_map[row["coin"]] for row in rows])
    cohort_idx = np.asarray([cohort_map[row["cohort_hour"]] for row in rows])
    is_short = np.asarray([row["side"] == "short" for row in rows], dtype=float)
    pnl = np.asarray([float(row["net_pnl_usd"]) for row in rows])

    with pm.Model():
        alpha = pm.Normal("alpha", mu=0.0, sigma=5.0)
        short_effect = pm.Normal("short_effect", mu=0.0, sigma=5.0)
        coin_sd = pm.HalfNormal("coin_sd", sigma=5.0)
        cohort_sd = pm.HalfNormal("cohort_sd", sigma=5.0)
        coin_offset = pm.Normal("coin_offset", 0.0, 1.0, shape=len(coins))
        cohort_offset = pm.Normal("cohort_offset", 0.0, 1.0, shape=len(cohorts))
        sigma = pm.HalfNormal("sigma", sigma=10.0)
        nu = pm.Deterministic("nu", 2.0 + pm.Exponential("nu_minus_two", 0.1))
        mu = (
            alpha
            + short_effect * is_short
            + coin_sd * coin_offset[coin_idx]
            + cohort_sd * cohort_offset[cohort_idx]
        )
        pm.StudentT("observed", nu=nu, mu=mu, sigma=sigma, observed=pnl)
        trace = pm.sample(
            draws=draws,
            tune=tune,
            chains=2,
            cores=1,
            target_accept=0.97,
            random_seed=seed,
            progressbar=False,
            compute_convergence_checks=False,
        )

    posterior = trace.posterior
    diagnostics = az.summary(
        trace,
        var_names=["alpha", "short_effect", "coin_sd", "cohort_sd", "sigma", "nu"],
        kind="diagnostics",
    )
    alpha_draws = posterior["alpha"].values.reshape(-1)
    side_draws = posterior["short_effect"].values.reshape(-1)
    coin_sd_draws = posterior["coin_sd"].values.reshape(-1)
    cohort_sd_draws = posterior["cohort_sd"].values.reshape(-1)
    sigma_draws = posterior["sigma"].values.reshape(-1)
    nu_draws = posterior["nu"].values.reshape(-1)
    expected_cohort = 8.0 * (alpha_draws + 0.5 * side_draws)

    rng = np.random.default_rng(seed + 1)
    predictive = np.zeros_like(expected_cohort)
    for index in range(len(predictive)):
        shared = rng.normal(0.0, cohort_sd_draws[index])
        long_mu = alpha_draws[index] + shared
        short_mu = alpha_draws[index] + side_draws[index] + shared
        coin_effects = rng.normal(0.0, coin_sd_draws[index], 8)
        residuals = (
            rng.standard_t(nu_draws[index], 8) * sigma_draws[index]
        )
        predictive[index] = (
            np.full(8, long_mu)
            + np.r_[np.zeros(4), np.full(4, short_mu - long_mu)]
            + coin_effects
            + residuals
        ).sum()

    return {
        "positions": len(rows),
        "cohorts": len(cohorts),
        "coins": len(coins),
        "likelihood": "student_t",
        "structure": "partial_pooling_by_coin_and_cohort_plus_side_effect",
        "sampler_diagnostics": {
            "max_rhat": float(diagnostics["r_hat"].max()),
            "min_bulk_ess": float(diagnostics["ess_bulk"].min()),
            "divergences": int(trace.sample_stats["diverging"].values.sum()),
        },
        "expected_balanced_8_position_cohort_mean_usd": float(expected_cohort.mean()),
        "expected_cohort_90pct": interval(expected_cohort),
        "probability_expected_cohort_gt_zero": float((expected_cohort > 0).mean()),
        "predictive_new_cohort_90pct": interval(predictive),
        "probability_new_cohort_gt_zero": float((predictive > 0).mean()),
        "long_mean_per_position_usd": float(alpha_draws.mean()),
        "short_mean_per_position_usd": float((alpha_draws + side_draws).mean()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--draws", type=int, default=1000)
    parser.add_argument("--tune", type=int, default=1000)
    args = parser.parse_args()
    cfg = yaml.safe_load(args.config.read_text())
    db = Path(cfg["outputs"]["database"])
    db = db if db.is_absolute() else REPO / db

    import sqlite3

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in conn.execute(
            """SELECT p.* FROM positions p JOIN cohorts c USING(cohort_hour)
               WHERE c.status='complete' AND p.valid=1 ORDER BY p.cohort_hour, p.coin"""
        )
    ]
    output = Path(cfg["outputs"]["report"]).with_name(
        Path(cfg["outputs"]["report"]).stem.replace("_report", "_robust") + ".json"
    )
    output = output if output.is_absolute() else REPO / output
    report = {
        "hypothesis_id": cfg["hypothesis_id"],
        "status": "exploratory_sensitivity_not_locked_verdict",
        "warning": (
            "Hourly cohorts overlap. This model is diagnostic; the official verdict "
            "uses only pre-registered non-overlapping cohorts."
        ),
    }
    if len(rows) < 8 or len({row["cohort_hour"] for row in rows}) < 2:
        report["ready"] = False
        report["reason"] = "need at least eight positions across two complete cohorts"
    else:
        report["ready"] = True
        report["full_data"] = fit_model(rows, args.draws, args.tune, int(cfg["inference"]["seed"]))
        largest = max(rows, key=lambda row: float(row["net_pnl_usd"]))
        reduced = [row for row in rows if row is not largest]
        report["largest_winner_removed"] = {
            "removed_coin": largest["coin"],
            "removed_cohort": largest["cohort_hour"],
            "removed_pnl_usd": float(largest["net_pnl_usd"]),
            "model": fit_model(
                reduced,
                max(500, args.draws // 2),
                max(500, args.tune // 2),
                int(cfg["inference"]["seed"]) + 100,
            ),
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
