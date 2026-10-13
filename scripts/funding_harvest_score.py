#!/usr/bin/env python3
"""Bayesian score report for the locked funding-harvest paper experiment."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.hyperliquid_bot.funding.hypothesis import (  # noqa: E402
    cluster_by_entry_date,
    init_db,
    posterior_summary,
)

DEFAULT_CONFIG = REPO / "config" / "funding_harvest_hypothesis.yaml"


def build_report(conn, cfg: dict) -> dict:
    all_positions = [dict(row) for row in conn.execute("SELECT * FROM positions")]
    by_date = {}
    for row in all_positions:
        by_date.setdefault(row["entry_date"], []).append(row)
    # A shared entry date is the inferential unit. Do not leak a date into the
    # model merely because one coin closed early while its siblings remain open.
    complete_dates = sorted(
        date
        for date, group in by_date.items()
        if group and all(row["status"] == "closed" for row in group)
    )
    rows = sorted(
        [
            row
            for row in all_positions
            if row["entry_date"] in complete_dates
            and row["status"] == "closed"
            and row["valid"] == 1
        ],
        key=lambda row: (row["entry_date"], row["coin"]),
    )
    invalid = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE status='closed' AND valid=0"
    ).fetchone()[0]
    dates = sorted({row["entry_date"] for row in rows})
    minimum = int(cfg["inference"]["minimum_independent_entry_dates"])
    report = {
        "hypothesis_id": cfg["hypothesis_id"],
        "benchmark": "cash_pnl_usd_equals_zero",
        "return_denominator": "gross entry notional: spot purchase plus perp short",
        "valid_completed_trades": len(rows),
        "invalid_completed_trades": invalid,
        "independent_entry_dates": len(dates),
        "minimum_independent_entry_dates": minimum,
        "ready": len(dates) >= minimum,
    }
    if rows:
        pnls = [float(row["net_pnl_usd"]) for row in rows]
        gross = [
            float(row["entry_spot_notional"]) + float(row["entry_perp_notional"])
            for row in rows
        ]
        report["descriptive_all_available"] = {
            "net_pnl_usd": sum(pnls),
            "mean_pnl_usd_per_trade": sum(pnls) / len(pnls),
            "net_return_on_gross_entry_notional": sum(pnls) / sum(gross),
        }
    if not report["ready"]:
        report["remaining_entry_dates"] = minimum - len(dates)
        report["verdict"] = "not_ready"
        return report

    # The first N complete entry dates are the immutable confirmatory sample.
    locked_dates = set(dates[:minimum])
    locked_rows = [row for row in rows if row["entry_date"] in locked_dates]
    clusters = cluster_by_entry_date(locked_rows)
    p = cfg["inference"]["prior"]
    posterior = posterior_summary(
        clusters,
        draws=int(cfg["inference"]["posterior_draws"]),
        seed=int(cfg["inference"]["seed"]),
        prior={
            "mu": float(p["mean_usd"]),
            "kappa": float(p["kappa"]),
            "alpha": float(p["alpha"]),
            "beta": float(p["beta"]),
        },
    )
    threshold = float(
        cfg["inference"]["support_if_probability_mean_gt_zero_at_least"]
    )
    supported = posterior["probability_mean_gt_zero"] >= threshold
    report.update(
        {
            "locked_entry_dates": dates[:minimum],
            "locked_valid_trades": len(locked_rows),
            "posterior": posterior,
            "decision_rule": f"P(mean net PnL > cash) >= {threshold}",
            "verdict": "supported" if supported else "not_supported",
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
