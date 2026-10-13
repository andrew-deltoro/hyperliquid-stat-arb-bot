#!/usr/bin/env python3
"""Visualize robust posterior sensitivity and 15-minute path observations."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yaml

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "oracle_convergence_hypothesis.yaml"
OUTPUT = REPO / "reports" / "oracle_convergence_diagnostics.png"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    cfg = yaml.safe_load(args.config.read_text())
    db = Path(cfg["outputs"]["database"])
    db = db if db.is_absolute() else REPO / db
    robust = Path(cfg["outputs"]["report"]).with_name(
        Path(cfg["outputs"]["report"]).stem.replace("_report", "_robust") + ".json"
    )
    robust = robust if robust.is_absolute() else REPO / robust
    model = json.loads(robust.read_text())
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    paths = [dict(row) for row in conn.execute("SELECT * FROM path_observations")]

    plt.style.use("dark_background")
    fig = plt.figure(figsize=(14, 8), facecolor="#0b1020")
    grid = fig.add_gridspec(2, 2, height_ratios=[0.65, 2.4], hspace=0.32, wspace=0.25)
    fig.suptitle("Oracle Basis Reversion — Confidence Diagnostics", fontsize=22, weight="bold", y=0.98)

    full = model["full_data"]
    reduced = model["largest_winner_removed"]["model"]
    cards = [
        ("Completed cohorts", str(full["cohorts"]), "overlapping; exploratory"),
        (
            "P(expected P&L > 0)",
            f"{full['probability_expected_cohort_gt_zero']:.0%}",
            "robust partial-pooling model",
        ),
        (
            "Without largest winner",
            f"{reduced['probability_expected_cohort_gt_zero']:.0%}",
            "outlier sensitivity",
        ),
        ("15-minute marks", str(len(paths)), "executable exits, not mids"),
    ]
    ax0 = fig.add_subplot(grid[0, :])
    ax0.axis("off")
    for i, (title, value, note) in enumerate(cards):
        x = 0.015 + i * 0.247
        ax0.add_patch(
            plt.Rectangle(
                (x, 0.07), 0.225, 0.8, transform=ax0.transAxes,
                facecolor="#141c30", edgecolor=["#2563eb", "#7c3aed", "#d97706", "#0891b2"][i],
                linewidth=2,
            )
        )
        ax0.text(x + 0.018, 0.68, title, transform=ax0.transAxes, color="#a9b4cc", fontsize=10)
        ax0.text(x + 0.018, 0.39, value, transform=ax0.transAxes, fontsize=20, weight="bold")
        ax0.text(x + 0.018, 0.16, note, transform=ax0.transAxes, color="#718096", fontsize=8.5)

    ax1 = fig.add_subplot(grid[1, 0])
    labels = ["All data", "Largest winner removed"]
    summaries = [full, reduced]
    means = [row["expected_balanced_8_position_cohort_mean_usd"] for row in summaries]
    lows = [row["expected_cohort_90pct"][0] for row in summaries]
    highs = [row["expected_cohort_90pct"][1] for row in summaries]
    ax1.errorbar(
        means,
        [1, 0],
        xerr=[[mean - low for mean, low in zip(means, lows)],
              [high - mean for mean, high in zip(means, highs)]],
        fmt="o",
        color="#60a5fa",
        ecolor="#94a3b8",
        capsize=6,
        markersize=9,
    )
    ax1.axvline(0, color="#ef4444", linewidth=1)
    ax1.set_yticks([1, 0], labels)
    ax1.set_xlabel("Expected net P&L for a balanced 8-position cohort ($)")
    ax1.set_title("Robust posterior: dots are means, bars are 90% intervals", loc="left")
    ax1.grid(axis="x", alpha=0.15)

    ax2 = fig.add_subplot(grid[1, 1])
    if paths:
        side_by_position = {
            (row["cohort_hour"], row["coin"]): row["side"]
            for row in conn.execute("SELECT cohort_hour, coin, side FROM positions")
        }
        for side, color in (("long", "#22c55e"), ("short", "#ef4444")):
            selected = [
                row for row in paths
                if side_by_position.get((row["cohort_hour"], row["coin"])) == side
            ]
            ax2.scatter(
                [row["elapsed_minutes"] for row in selected],
                [row["convergence_bps"] for row in selected],
                label=side,
                color=color,
                alpha=0.7,
            )
        ax2.axhline(0, color="white", linewidth=1)
        ax2.set_xlabel("Minutes after entry")
        ax2.set_ylabel("Convergence toward oracle (bps)")
        ax2.set_title("Executable path observations", loc="left")
        ax2.legend(frameon=False)
        ax2.grid(alpha=0.15)
    else:
        ax2.axis("off")
        ax2.text(0.5, 0.5, "15-minute paths are beginning to collect", ha="center")

    fig.text(
        0.5,
        0.018,
        "Sensitivity result, not the locked verdict. Wide intervals mean confidence is still low.",
        ha="center",
        color="#fbbf24",
        fontsize=11,
        weight="bold",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(args.output)


if __name__ == "__main__":
    main()
