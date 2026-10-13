#!/usr/bin/env python3
"""Create a current-state graphic for both oracle-convergence experiments."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

REPO = Path(__file__).resolve().parents[1]
V1_DB = REPO / "state" / "oracle_convergence_v1.db"
V2_DB = REPO / "state" / "oracle_convergence_cost_gated_v2.db"
ROBUST = REPO / "state" / "oracle_convergence_v1_robust.json"
OUTPUT = REPO / "reports" / "oracle_convergence_live_status.png"


def scalar(conn, sql):
    return conn.execute(sql).fetchone()[0]


def card(ax, x, title, value, note, color):
    ax.add_patch(
        FancyBboxPatch(
            (x, 0.08), 0.225, 0.8,
            boxstyle="round,pad=0.01",
            transform=ax.transAxes,
            facecolor="#141c30",
            edgecolor=color,
            linewidth=2,
        )
    )
    ax.text(x + 0.015, 0.69, title, transform=ax.transAxes, color="#a9b4cc", fontsize=10)
    ax.text(
        x + 0.015, 0.40, value, transform=ax.transAxes,
        fontsize=14 if len(value) > 16 else 18, weight="bold",
    )
    ax.text(x + 0.015, 0.17, note, transform=ax.transAxes, color="#718096", fontsize=8.5)


def main():
    v1 = sqlite3.connect(V1_DB)
    v1.row_factory = sqlite3.Row
    v2 = sqlite3.connect(V2_DB)
    v2.row_factory = sqlite3.Row
    model = json.loads(ROBUST.read_text())["full_data"]
    cohorts = [
        dict(row)
        for row in v1.execute(
            """SELECT p.cohort_hour, SUM(p.net_pnl_usd) pnl, COUNT(*) positions
               FROM positions p JOIN cohorts c USING(cohort_hour)
               WHERE c.status='complete'
               GROUP BY p.cohort_hour ORDER BY p.cohort_hour"""
        )
    ]
    total = sum(float(row["pnl"]) for row in cohorts)
    winners = sum(float(row["pnl"]) > 0 for row in cohorts)
    losers = len(cohorts) - winners
    open_cohorts = scalar(v1, "SELECT COUNT(*) FROM cohorts WHERE status='open'")
    paths = scalar(v1, "SELECT COUNT(*) FROM path_observations")
    v2_skips = scalar(v2, "SELECT COUNT(*) FROM cohorts WHERE status='skipped'")
    v2_open = scalar(v2, "SELECT COUNT(*) FROM cohorts WHERE status='open'")

    plt.style.use("dark_background")
    fig = plt.figure(figsize=(15, 9), facecolor="#0b1020")
    grid = fig.add_gridspec(3, 2, height_ratios=[0.8, 2.0, 1.35], hspace=0.38, wspace=0.27)
    fig.suptitle("Oracle Basis Reversion — Live Experiment", fontsize=23, weight="bold", y=0.995)
    fig.text(
        0.5, 0.955,
        "Everything shown is paper trading; no orders or funds are involved",
        ha="center", color="#a9b4cc", fontsize=11,
    )

    ax0 = fig.add_subplot(grid[0, :])
    ax0.axis("off")
    card(
        ax0, 0.015, "V1 completed",
        f"{len(cohorts)} cohorts",
        f"{winners} winners / {losers} losers • total USD {total:+.2f}",
        "#2563eb",
    )
    card(
        ax0, 0.262, "Formal evidence",
        "1 / 30",
        "first formal cohort: USD −9.14",
        "#7c3aed",
    )
    card(
        ax0, 0.509, "V2 cost gate",
        f"{v2_open} open / {v2_skips} skipped",
        "requires ≥12 bps on both sides",
        "#d97706",
    )
    card(
        ax0, 0.756, "Robust confidence",
        f"{model['probability_expected_cohort_gt_zero']:.0%}",
        f"next cohort profit: {model['probability_new_cohort_gt_zero']:.0%}",
        "#0891b2",
    )

    ax1 = fig.add_subplot(grid[1, 0])
    labels = [row["cohort_hour"][11:16] + " UTC" for row in cohorts]
    values = [float(row["pnl"]) for row in cohorts]
    bars = ax1.bar(labels, values, color=["#22c55e" if value > 0 else "#ef4444" for value in values])
    ax1.axhline(0, color="white", linewidth=1)
    ax1.set_ylabel("Net P&L after fills and fees (USD)")
    ax1.set_title("What has actually completed", loc="left")
    ax1.grid(axis="y", alpha=0.15)
    for bar, value in zip(bars, values):
        ax1.text(
            bar.get_x() + bar.get_width() / 2,
            value + (1 if value >= 0 else -2.2),
            f"{value:+.2f}",
            ha="center",
            va="bottom" if value >= 0 else "top",
            fontsize=9,
        )
    # 20:00 UTC is the first pre-registered non-overlapping cohort.
    if len(bars) >= 3:
        bars[2].set_edgecolor("#fbbf24")
        bars[2].set_linewidth(3)
        bars[2].set_hatch("//")
        ax1.text(2, ax1.get_ylim()[0] * 0.82, "FORMAL", ha="center", color="#fbbf24", fontsize=8)

    ax2 = fig.add_subplot(grid[1, 1])
    low, high = model["expected_cohort_90pct"]
    mean = model["expected_balanced_8_position_cohort_mean_usd"]
    ax2.errorbar(
        [mean], [0], xerr=[[mean - low], [high - mean]], fmt="o",
        color="#60a5fa", ecolor="#94a3b8", capsize=7, markersize=10,
    )
    ax2.axvline(0, color="#ef4444", linewidth=1)
    ax2.set_yticks([0], ["Expected cohort P&L"])
    ax2.set_xlabel("USD; dot = posterior mean, bar = 90% interval")
    ax2.set_title("Why the answer is still “not ready”", loc="left")
    ax2.grid(axis="x", alpha=0.15)
    ax2.text(
        0.5, 0.12,
        "The interval crosses zero by a wide margin.\nPositive lean ≠ established edge.",
        transform=ax2.transAxes, ha="center", color="#fbbf24", fontsize=11,
    )

    ax3 = fig.add_subplot(grid[2, :])
    ax3.axis("off")
    steps = [
        ("READ", "oracle + executable books", "#0891b2"),
        ("RANK", "cheap longs + rich shorts", "#2563eb"),
        ("FILTER", "V2 requires ≥12 bps", "#d97706"),
        ("WATCH", f"{paths} path marks so far", "#7c3aed"),
        ("CLOSE", "after four hours", "#ef4444"),
        ("LEARN", f"{open_cohorts} V1 cohorts open", "#22c55e"),
    ]
    xs = [0.02, 0.185, 0.35, 0.515, 0.68, 0.845]
    for index, ((title, body, color), x) in enumerate(zip(steps, xs)):
        ax3.add_patch(
            FancyBboxPatch(
                (x, 0.24), 0.13, 0.53, boxstyle="round,pad=0.01",
                transform=ax3.transAxes, facecolor="#141c30", edgecolor=color, linewidth=2,
            )
        )
        ax3.text(x + 0.065, 0.59, title, transform=ax3.transAxes, ha="center", weight="bold")
        ax3.text(
            x + 0.065, 0.39, body, transform=ax3.transAxes,
            ha="center", color="#a9b4cc", fontsize=8.5, wrap=True,
        )
        if index < len(steps) - 1:
            ax3.add_patch(
                FancyArrowPatch(
                    (x + 0.135, 0.50), (xs[index + 1] - 0.005, 0.50),
                    transform=ax3.transAxes, arrowstyle="-|>", color="#64748b",
                    linewidth=1.5, mutation_scale=14,
                )
            )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(OUTPUT)


if __name__ == "__main__":
    main()
