#!/usr/bin/env python3
"""Visual dashboard for the perp-versus-oracle convergence experiment."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts.oracle_convergence_score import build_report  # noqa: E402

CONFIG = REPO / "config" / "oracle_convergence_hypothesis.yaml"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args()
    cfg = yaml.safe_load(args.config.read_text())
    db = Path(cfg["outputs"]["database"])
    out = Path(cfg["outputs"]["dashboard"])
    db = db if db.is_absolute() else REPO / db
    out = out if out.is_absolute() else REPO / out
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    report = build_report(conn, cfg)
    minimum = int(cfg["inference"]["minimum_complete_cohorts"])
    latest_complete = conn.execute(
        "SELECT MAX(cohort_hour) FROM cohorts WHERE status='complete'"
    ).fetchone()[0]
    latest_any = conn.execute("SELECT MAX(cohort_hour) FROM positions").fetchone()[0]
    display_cohort = latest_complete or latest_any
    positions = [
        dict(row)
        for row in conn.execute(
            """SELECT * FROM positions
               WHERE cohort_hour=?
               ORDER BY entry_basis_bps"""
            ,
            (display_cohort,),
        )
    ]
    complete = [
        dict(row)
        for row in conn.execute(
            """SELECT cohort_hour, SUM(net_pnl_usd) pnl
               FROM positions WHERE status='closed' AND valid=1
               GROUP BY cohort_hour ORDER BY cohort_hour"""
        )
    ]

    plt.style.use("dark_background")
    fig = plt.figure(figsize=(14, 9), facecolor="#0b1020")
    grid = fig.add_gridspec(3, 1, height_ratios=[0.8, 2.3, 1.45], hspace=0.38)
    fig.suptitle("Oracle Basis Reversion — Paper Test", fontsize=22, weight="bold", y=0.985)
    fig.text(
        0.5, 0.945,
        "Long the cheapest decile • Short the richest decile • Close after 4 hours",
        ha="center", color="#a9b4cc", fontsize=11,
    )

    ax0 = fig.add_subplot(grid[0])
    ax0.axis("off")
    cards = [
        (
            "Confirmatory cohorts",
            f"{report['complete_cohorts']} / {minimum}",
            "non-overlapping; needed for verdict",
        ),
        ("Open cohorts", str(report["open_cohorts"]), "each held four hours"),
        ("Displayed positions", str(len(positions)), "equal long and short dollars"),
        ("Current verdict", report["verdict"].replace("_", " ").upper(), "paper evidence only"),
    ]
    for i, (title, value, note) in enumerate(cards):
        x = 0.015 + i * 0.247
        ax0.add_patch(
            plt.Rectangle(
                (x, 0.08), 0.225, 0.78, transform=ax0.transAxes,
                facecolor="#141c30", edgecolor=["#2563eb", "#7c3aed", "#0891b2", "#d97706"][i],
                linewidth=2,
            )
        )
        ax0.text(x + 0.018, 0.68, title, transform=ax0.transAxes, color="#a9b4cc", fontsize=10)
        ax0.text(
            x + 0.018, 0.39, value, transform=ax0.transAxes,
            color="white", fontsize=17 if len(value) < 15 else 13, weight="bold",
        )
        ax0.text(x + 0.018, 0.17, note, transform=ax0.transAxes, color="#718096", fontsize=8.5)

    ax1 = fig.add_subplot(grid[1])
    if positions:
        labels = [row["coin"] for row in positions]
        values = [float(row["entry_basis_bps"]) for row in positions]
        colors = ["#22c55e" if row["side"] == "long" else "#ef4444" for row in positions]
        ax1.barh(labels, values, color=colors)
        ax1.axvline(0, color="white", linewidth=1)
        ax1.axvspan(-9, 9, color="#fbbf24", alpha=0.10)
        ax1.set_xlabel("Executable entry price minus oracle (basis points)")
        state_label = "completed" if latest_complete else "open"
        ax1.set_title(
            f"{state_label.title()} cohort {display_cohort[5:13].replace('T', ' ')}: "
            "green longs were cheap; red shorts were rich",
            loc="left",
        )
        ax1.text(
            1, 1.02, "Yellow band = 9 bp round-trip taker fees (before spread)",
            transform=ax1.transAxes, ha="right", color="#fbbf24", fontsize=9,
        )
        ax1.grid(axis="x", alpha=0.15)
    else:
        ax1.axis("off")
        ax1.text(0.5, 0.5, "Waiting for the first hourly cohort", ha="center", fontsize=18)

    ax2 = fig.add_subplot(grid[2])
    if latest_complete:
        result_positions = sorted(positions, key=lambda row: float(row["net_pnl_usd"]))
        labels = [row["coin"] for row in result_positions]
        values = [float(row["net_pnl_usd"]) for row in result_positions]
        net = sum(values)
        fees = sum(float(row["fees_usd"]) for row in result_positions)
        convergence = sum(float(row["convergence_bps"]) for row in result_positions) / len(result_positions)
        ax2.barh(
            labels,
            values,
            color=["#22c55e" if value > 0 else "#ef4444" for value in values],
        )
        ax2.axvline(0, color="white", linewidth=1)
        ax2.set_xlabel("Net four-hour P&L after entry/exit fills and fees ($)")
        ax2.set_title(
            f"Exploratory result: USD {net:+.2f} net  |  USD {fees:.2f} fees  |  "
            f"{convergence:+.1f} bp mean convergence",
            loc="left",
        )
        ax2.grid(axis="x", alpha=0.15)
    else:
        ax2.axis("off")
        ax2.text(
            0.5, 0.66,
            "ENTRY → wait 4 hours → EXIT → did the premium shrink enough to beat costs?",
            ha="center", fontsize=14, weight="bold",
        )
        ax2.text(
            0.5, 0.31,
            "The first cohort is still open. No profitability conclusion exists yet.",
            ha="center", color="#fbbf24", fontsize=12,
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(out)


if __name__ == "__main__":
    main()
