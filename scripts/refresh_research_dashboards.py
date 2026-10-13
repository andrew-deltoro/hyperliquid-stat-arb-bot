#!/usr/bin/env python3
"""Refresh all human-facing research dashboards.

The robust model reruns only when another V1 cohort completes. Lightweight
score reports and graphics refresh every cycle.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
V1_DB = REPO / "state" / "oracle_convergence_v1.db"
ROBUST = REPO / "state" / "oracle_convergence_v1_robust.json"
STATUS = REPO / "reports" / "dashboard_refresh_status.json"


def run(script: str, *args: str) -> None:
    subprocess.run(
        [str(PYTHON), str(REPO / "scripts" / script), *args],
        cwd=REPO,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=180,
    )


def completed_v1_cohorts() -> int:
    if not V1_DB.exists():
        return 0
    conn = sqlite3.connect(V1_DB)
    return int(
        conn.execute("SELECT COUNT(*) FROM cohorts WHERE status='complete'").fetchone()[0]
    )


def robust_cohorts() -> int:
    try:
        return int(json.loads(ROBUST.read_text())["full_data"]["cohorts"])
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return 0


def refresh() -> dict:
    started = datetime.now(timezone.utc)
    complete = completed_v1_cohorts()
    if complete >= 2 and complete != robust_cohorts():
        run("oracle_convergence_robust.py")

    run("oracle_convergence_score.py")
    run(
        "oracle_convergence_score.py",
        "--config",
        "config/oracle_convergence_cost_gated_v2.yaml",
    )
    run("funding_harvest_score.py")
    run("oracle_convergence_live_status.py")
    run("oracle_convergence_diagnostics_visual.py")
    run("oracle_convergence_visual.py")
    run(
        "oracle_convergence_visual.py",
        "--config",
        "config/oracle_convergence_cost_gated_v2.yaml",
    )
    run("funding_harvest_visual.py")
    run("live_pairs_pnl_visual.py")
    result = {
        "status": "ok",
        "started_utc": started.isoformat(),
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "v1_complete_cohorts": complete,
    }
    STATUS.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    while True:
        try:
            result = refresh()
            print(json.dumps(result), flush=True)
        except Exception as exc:
            result = {
                "status": "error",
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
            }
            STATUS.write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result), file=sys.stderr, flush=True)
            if not args.loop:
                raise
        if not args.loop:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
