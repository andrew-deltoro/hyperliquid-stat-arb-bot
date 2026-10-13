#!/usr/bin/env python3
"""Create a visual, plain-language explainer for the convergence tests."""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

REPO = Path(__file__).resolve().parents[1]
OUTPUT = REPO / "reports" / "oracle_convergence_explainer.png"


def box(ax, x, y, w, h, title, body, edge="#2563eb", face="#141c30", title_size=12):
    patch = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.012,rounding_size=0.012",
        linewidth=2,
        edgecolor=edge,
        facecolor=face,
        transform=ax.transAxes,
    )
    ax.add_patch(patch)
    ax.text(
        x + w / 2,
        y + h * 0.72,
        title,
        transform=ax.transAxes,
        ha="center",
        va="center",
        fontsize=title_size,
        weight="bold",
        color="white",
    )
    ax.text(
        x + w / 2,
        y + h * 0.34,
        body,
        transform=ax.transAxes,
        ha="center",
        va="center",
        fontsize=9.5,
        color="#cbd5e1",
        linespacing=1.35,
    )


def arrow(ax, x1, y1, x2, y2, color="#64748b"):
    ax.add_patch(
        FancyArrowPatch(
            (x1, y1),
            (x2, y2),
            arrowstyle="-|>",
            mutation_scale=16,
            linewidth=1.8,
            color=color,
            transform=ax.transAxes,
        )
    )


def main():
    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(16, 11), facecolor="#0b1020")
    ax.set_facecolor("#0b1020")
    ax.axis("off")
    fig.suptitle(
        "What We Are Testing: Oracle Basis Reversion",
        fontsize=24,
        weight="bold",
        y=0.992,
    )
    fig.text(
        0.5,
        0.953,
        "Can temporary perp mispricing be traded profitably after realistic fills and fees?",
        ha="center",
        color="#a9b4cc",
        fontsize=12,
    )

    # Definitions
    box(
        ax,
        0.035,
        0.75,
        0.27,
        0.14,
        "ORACLE = reference price",
        "A validator-computed spot reference.\nExample: oracle says USD 100.00.",
        edge="#0891b2",
    )
    box(
        ax,
        0.365,
        0.75,
        0.27,
        0.14,
        "PERP = price we can trade",
        "We use the executable book—not a midpoint.\nIt can briefly differ from the oracle.",
        edge="#7c3aed",
    )
    box(
        ax,
        0.695,
        0.75,
        0.27,
        0.14,
        "BPS = size of the difference",
        "1 bp = 0.01% = USD 0.05 on USD 500.\n9 bps ≈ USD 0.45 round-trip fees.",
        edge="#d97706",
    )
    arrow(ax, 0.305, 0.82, 0.36, 0.82)
    arrow(ax, 0.635, 0.82, 0.69, 0.82)

    # Trade examples
    ax.text(
        0.035,
        0.705,
        "1  FIND BOTH SIDES OF THE DISLOCATION",
        transform=ax.transAxes,
        fontsize=13,
        weight="bold",
        color="#93c5fd",
    )
    box(
        ax,
        0.06,
        0.52,
        0.39,
        0.15,
        "CHEAP PERP → PAPER LONG",
        "Oracle: USD 100.00     Buy: USD 99.80\nDifference: −20 bps\nBuy roughly USD 500 of the perp.",
        edge="#22c55e",
    )
    box(
        ax,
        0.55,
        0.52,
        0.39,
        0.15,
        "RICH PERP → PAPER SHORT",
        "Oracle: USD 100.00     Sell: USD 100.20\nDifference: +20 bps\nShort roughly USD 500 of the perp.",
        edge="#ef4444",
    )
    arrow(ax, 0.45, 0.595, 0.545, 0.595)
    ax.text(
        0.5,
        0.63,
        "equal dollars",
        transform=ax.transAxes,
        ha="center",
        color="#a9b4cc",
        fontsize=9,
    )

    # V1/V2 split
    ax.text(
        0.035,
        0.475,
        "2  RUN TWO SEPARATE PAPER TESTS",
        transform=ax.transAxes,
        fontsize=13,
        weight="bold",
        color="#93c5fd",
    )
    box(
        ax,
        0.055,
        0.31,
        0.41,
        0.13,
        "V1 CONTROL: trade the extreme deciles",
        "Asks whether relative ranking works at all.\nRules remain untouched so results stay honest.",
        edge="#2563eb",
    )
    box(
        ax,
        0.535,
        0.31,
        0.41,
        0.13,
        "V2 COST GATE: require at least 12 bps",
        "9 bps known taker fees + 3 bps buffer.\nSkip the hour unless long and short sides qualify.",
        edge="#f59e0b",
    )

    # Measurement chain
    ax.text(
        0.035,
        0.265,
        "3  MEASURE THE ENTIRE PATH",
        transform=ax.transAxes,
        fontsize=13,
        weight="bold",
        color="#93c5fd",
    )
    steps = [
        ("ENTER", "Walk real L2 book\nand charge fee", "#2563eb"),
        ("WATCH", "Executable exit marks\nevery 15 minutes", "#7c3aed"),
        ("EXIT", "Walk opposite book\nafter 4 hours", "#0891b2"),
        ("SCORE", "Price P&L minus\nboth fees", "#d97706"),
        ("COMPARE", "Did the balanced\ncohort beat USD 0?", "#22c55e"),
    ]
    width = 0.155
    xs = [0.025, 0.222, 0.419, 0.616, 0.813]
    for index, ((title, body, color), x) in enumerate(zip(steps, xs)):
        box(ax, x, 0.105, width, 0.12, title, body, edge=color, title_size=11)
        if index < len(steps) - 1:
            arrow(ax, x + width + 0.005, 0.165, xs[index + 1] - 0.008, 0.165)

    # Footer / inference
    ax.text(
        0.5,
        0.065,
        "Confidence rule: 30 non-overlapping four-hour cohorts • robust Student-t model • "
        "partial pooling by coin/cohort • repeat after removing the largest winner",
        transform=ax.transAxes,
        ha="center",
        fontsize=10.5,
        color="#fbbf24",
        weight="bold",
    )
    ax.text(
        0.5,
        0.025,
        "Success means repeatable net profit after costs—not merely that prices moved toward the oracle.",
        transform=ax.transAxes,
        ha="center",
        fontsize=12,
        color="white",
        weight="bold",
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(OUTPUT)


if __name__ == "__main__":
    main()
