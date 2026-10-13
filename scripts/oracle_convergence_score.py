#!/usr/bin/env python3
"""Score complete hourly cohorts in the locked oracle-convergence test."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.hyperliquid_bot.funding.hypothesis import posterior_summary  # noqa: E402
from src.hyperliquid_bot.oracle_convergence import init_db  # noqa: E402

DEFAULT_CONFIG = REPO / "config" / "oracle_convergence_hypothesis.yaml"


def build_report(conn, cfg: dict) -> dict:
    all_complete = [
        row["cohort_hour"]
        for row in conn.execute(
            """SELECT cohort_hour FROM cohorts WHERE status='complete'
               ORDER BY cohort_hour"""
        )
        if conn.execute(
            "SELECT COUNT(*) FROM positions WHERE cohort_hour=? AND valid=1",
            (row["cohort_hour"],),
        ).fetchone()[0]
    ]
    confirm_hours = {int(hour) for hour in cfg["inference"]["confirmatory_entry_hours_utc"]}
    cohorts = [
        cohort
        for cohort in all_complete
        if int(cohort.split("T", 1)[1].split(":", 1)[0]) in confirm_hours
    ]
    cohort_rows = []
    for cohort in cohorts:
        row = conn.execute(
            """SELECT SUM(net_pnl_usd) AS pnl, AVG(convergence_bps) AS convergence,
                      COUNT(*) AS positions
               FROM positions WHERE cohort_hour=? AND valid=1""",
            (cohort,),
        ).fetchone()
        cohort_rows.append(
            {
                "cohort": cohort,
                "net_pnl_usd": float(row["pnl"]),
                "mean_convergence_bps": float(row["convergence"]),
                "positions": int(row["positions"]),
            }
        )
    minimum = int(cfg["inference"]["minimum_complete_cohorts"])
    report = {
        "hypothesis_id": cfg["hypothesis_id"],
        "benchmark": "market_neutral_cohort_net_pnl_equals_zero",
        "complete_cohorts": len(cohort_rows),
        "all_hourly_complete_cohorts": len(all_complete),
        "minimum_complete_cohorts": minimum,
        "open_cohorts": conn.execute(
            "SELECT COUNT(*) FROM cohorts WHERE status='open'"
        ).fetchone()[0],
        "skipped_cohorts": conn.execute(
            "SELECT COUNT(*) FROM cohorts WHERE status='skipped'"
        ).fetchone()[0],
        "ready": len(cohort_rows) >= minimum,
    }
    if cohort_rows:
        report["descriptive_all_available"] = {
            "net_pnl_usd": sum(row["net_pnl_usd"] for row in cohort_rows),
            "mean_net_pnl_usd_per_cohort": sum(
                row["net_pnl_usd"] for row in cohort_rows
            )
            / len(cohort_rows),
            "mean_convergence_bps": sum(
                row["mean_convergence_bps"] for row in cohort_rows
            )
            / len(cohort_rows),
        }
    if not report["ready"]:
        report["remaining_complete_cohorts"] = minimum - len(cohort_rows)
        report["verdict"] = "not_ready"
        return report

    locked = cohort_rows[:minimum]
    p = cfg["inference"]["prior"]
    posterior = posterior_summary(
        [row["net_pnl_usd"] for row in locked],
        draws=int(cfg["inference"]["posterior_draws"]),
        seed=int(cfg["inference"]["seed"]),
        prior={
            "mu": float(p["mean_usd"]),
            "kappa": float(p["kappa"]),
            "alpha": float(p["alpha"]),
            "beta": float(p["beta"]),
        },
    )
    posterior["model"] = "normal_inverse_gamma_on_nonoverlapping_four_hour_cohorts"
    posterior["independent_units"] = posterior.pop("independent_entry_dates")
    threshold = float(
        cfg["inference"]["support_if_probability_mean_gt_zero_at_least"]
    )
    report.update(
        {
            "locked_cohorts": [row["cohort"] for row in locked],
            "posterior": posterior,
            "decision_rule": f"P(mean cohort net PnL > 0) >= {threshold}",
            "verdict": (
                "supported"
                if posterior["probability_mean_gt_zero"] >= threshold
                else "not_supported"
            ),
        }
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    cfg = yaml.safe_load(args.config.read_text())
    db_path = Path(cfg["outputs"]["database"])
    report_path = Path(cfg["outputs"]["report"])
    if not db_path.is_absolute():
        db_path = REPO / db_path
    if not report_path.is_absolute():
        report_path = REPO / report_path
    report = build_report(init_db(db_path), cfg)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
