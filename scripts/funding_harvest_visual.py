#!/usr/bin/env python3
"""Create a plain-language visual dashboard for funding-harvest v1."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.funding_harvest_score import build_report  # noqa: E402

DEFAULT_CONFIG = REPO / "config" / "funding_harvest_hypothesis.yaml"
DEFAULT_OUTPUT = REPO / "reports" / "funding_harvest_v1_dashboard.png"


def resolve(repo: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo / path


def make_dashboard(config_path: Path, output: Path) -> None:
    cfg = yaml.safe_load(config_path.read_text())
    db_path = resolve(REPO, cfg["outputs"]["database"])
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    report = build_report(conn, cfg)

    observations = conn.execute(
        """SELECT coin, ts_ms, funding_rate FROM funding_observations
           WHERE ts_ms >= (SELECT MAX(ts_ms) FROM funding_observations) - 172800000
           ORDER BY ts_ms"""
    ).fetchall()
    decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    open_count = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE status='open'"
    ).fetchone()[0]
    closed = [
        dict(row)
        for row in conn.execute(
            """SELECT coin, exit_ts_ms, net_pnl_usd FROM positions
               WHERE status='closed' AND valid=1 ORDER BY exit_ts_ms"""
        )
    ]

    plt.style.use("dark_background")
    fig = plt.figure(figsize=(14, 10), facecolor="#0b1020")
    grid = fig.add_gridspec(3, 1, height_ratios=[1.05, 2.5, 1.65], hspace=0.34)
    fig.suptitle(
        "Hyperliquid Funding-Harvest Test — Visual Status",
        fontsize=22,
        fontweight="bold",
        y=0.992,
    )
    fig.text(
        0.5,
        0.952,
        "Paper only: buy spot + short the same perp after 24 positive hourly funding prints",
        ha="center",
        color="#a9b4cc",
        fontsize=11,
    )

    # Status cards
    ax0 = fig.add_subplot(grid[0])
    ax0.axis("off")
    cards = [
        ("Funding observations", f"{len(observations):,}", "latest 48 hours shown below"),
        ("Entry decisions", str(decisions), "checked once per day"),
        ("Paper positions", f"{open_count} open / {len(closed)} closed", "no real orders"),
        (
            "Evidence progress",
            f"{report['independent_entry_dates']} / {report['minimum_independent_entry_dates']}",
            "independent completed dates",
        ),
    ]
    colors = ["#2563eb", "#7c3aed", "#0891b2", "#d97706"]
    for i, ((title, value, note), color) in enumerate(zip(cards, colors)):
        x = 0.015 + i * 0.247
        ax0.add_patch(
            plt.Rectangle(
                (x, 0.08), 0.225, 0.78, transform=ax0.transAxes,
                facecolor="#141c30", edgecolor=color, linewidth=2
            )
        )
        ax0.text(x + 0.018, 0.69, title, transform=ax0.transAxes, color="#a9b4cc", fontsize=10)
        ax0.text(
            x + 0.018, 0.38, value, transform=ax0.transAxes,
            color="white", fontsize=15 if len(value) > 12 else 20, fontweight="bold"
        )
        ax0.text(x + 0.018, 0.17, note, transform=ax0.transAxes, color="#718096", fontsize=8.5)

    # Actual funding series
    ax1 = fig.add_subplot(grid[1])
    palette = {"BTC": "#f59e0b", "ETH": "#818cf8", "HYPE": "#2dd4bf", "PURR": "#f472b6"}
    grouped = {}
    for row in observations:
        grouped.setdefault(row["coin"], []).append(row)
    for coin, rows in grouped.items():
        times = [datetime.fromtimestamp(row["ts_ms"] / 1000, tz=timezone.utc) for row in rows]
        annualized = [float(row["funding_rate"]) * 24 * 365 * 100 for row in rows]
        ax1.plot(times, annualized, label=coin, linewidth=1.8, color=palette.get(coin))
    ax1.axhline(0, color="#ef4444", linewidth=1, alpha=0.8)
    ax1.set_title("What the bot currently sees: hourly funding, annualized", loc="left", fontsize=14)
    ax1.set_ylabel("Annualized funding (%)")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b %d\n%H:%M", tz=timezone.utc))
    ax1.grid(alpha=0.15)
    ax1.legend(ncol=4, frameon=False, loc="upper left")
    ax1.text(
        1.0,
        1.02,
        "Above zero → perp shorts receive funding",
        transform=ax1.transAxes,
        ha="right",
        color="#a9b4cc",
        fontsize=9,
    )

    ax2 = fig.add_subplot(grid[2])
    if closed:
        labels = [f"{row['coin']} #{i + 1}" for i, row in enumerate(closed)]
        pnls = [float(row["net_pnl_usd"]) for row in closed]
        bar_colors = ["#22c55e" if value > 0 else "#ef4444" for value in pnls]
        ax2.bar(labels, pnls, color=bar_colors)
        ax2.axhline(0, color="white", linewidth=0.8)
        ax2.set_ylabel("Net P&L after funding, slippage and fees ($)")
        ax2.set_title("Completed paper trades", loc="left", fontsize=14)
        ax2.tick_params(axis="x", rotation=45)
    else:
        ax2.axis("off")
        steps = [
            ("1", "24 positive\nfunding prints"),
            ("2", "Walk both books\nfor a $500 leg"),
            ("3", "Hold 7 days or\nfunding turns negative"),
            ("4", "Subtract 4 fees\nand all slippage"),
            ("5", "Compare net P&L\nwith cash = $0"),
        ]
        for i, (number, text) in enumerate(steps):
            x = 0.04 + i * 0.195
            ax2.text(
                x, 0.62, number, transform=ax2.transAxes, ha="center", va="center",
                fontsize=14, fontweight="bold",
                bbox={"boxstyle": "circle,pad=0.45", "facecolor": "#2563eb", "edgecolor": "none"},
            )
            ax2.text(x, 0.28, text, transform=ax2.transAxes, ha="center", fontsize=10)
            if i < len(steps) - 1:
                ax2.annotate(
                    "", xy=(x + 0.15, 0.62), xytext=(x + 0.05, 0.62),
                    xycoords=ax2.transAxes,
                    arrowprops={"arrowstyle": "->", "color": "#64748b", "lw": 1.8},
                )
        ax2.set_title("What happens next", loc="left", fontsize=14)

    verdict = report["verdict"].replace("_", " ").upper()
    footer = (
        f"CURRENT VERDICT: {verdict}.  "
        f"This is not evidence of profit yet. The locked test needs "
        f"{report.get('remaining_entry_dates', 0)} more completed entry dates."
        if not report["ready"]
        else f"LOCKED VERDICT: {verdict}."
    )
    fig.text(
        0.5, 0.018, footer, ha="center", color="#fbbf24",
        fontsize=11, fontweight="bold"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    make_dashboard(args.config, args.output)
    print(args.output)


if __name__ == "__main__":
    main()
