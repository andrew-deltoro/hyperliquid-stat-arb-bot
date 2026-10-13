#!/usr/bin/env python3
"""Human-facing realized and open P&L for the live pairs bot."""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import requests
import yaml
from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.utils import constants

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.hyperliquid_bot.phase3.funding import funding_for_coins  # noqa: E402
from src.hyperliquid_bot.phase3.journal import compute_pnl  # noqa: E402

RUNTIME = Path(os.getenv("HLBOT_RUNTIME_DIR", "/home/hlbot/hlbot-runtime/live"))
OUTPUT = REPO / "reports" / "live_pairs_pnl.png"


def main() -> None:
    load_dotenv(REPO / ".env")
    cfg = yaml.safe_load((REPO / "config" / "phase3-live.yaml").read_text())
    rows = list(csv.DictReader((RUNTIME / "reports" / "trades.csv").open()))
    realized = defaultdict(float)
    for row in rows:
        realized[row["pair"]] += float(row["pnl_usd"])

    positions = [
        row
        for row in json.loads((RUNTIME / "state" / "positions.json").read_text())["positions"]
        if row.get("status") == "open"
    ]
    meta, contexts = requests.post(
        cfg.get("api_url", "https://api.hyperliquid.xyz/info"),
        json={"type": "metaAndAssetCtxs"},
        timeout=20,
    ).json()
    marks = {
        asset["name"]: float(context["markPx"])
        for asset, context in zip(meta["universe"], contexts)
    }
    ex_cfg = cfg["executor"]
    address = os.getenv(ex_cfg.get("subaccount_address_env", "")) or os.getenv(
        ex_cfg["account_address_env"]
    )
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    now = datetime.now(timezone.utc).isoformat()
    open_estimates = {}
    tracked = set()
    for pos in positions:
        tracked.update((pos["coin_a"], pos["coin_b"]))
        funding = sum(
            funding_for_coins(
                info,
                address,
                [pos["coin_a"], pos["coin_b"]],
                pos["entry_time_iso"],
                now,
            ).values()
        )
        pnl, _ = compute_pnl(
            pos,
            marks[pos["coin_a"]],
            marks[pos["coin_b"]],
            funding_usd=funding,
        )
        open_estimates[pos["pair"]] = pnl

    state = requests.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "clearinghouseState", "user": address},
        timeout=20,
    ).json()
    untracked = [
        p["position"]
        for p in state.get("assetPositions", [])
        if float(p["position"].get("szi", 0)) != 0
        and p["position"].get("coin") not in tracked
    ]

    realized_total = sum(realized.values())
    open_total = sum(open_estimates.values())
    wins = sum(float(row["pnl_usd"]) > 0 for row in rows)

    plt.style.use("dark_background")
    fig = plt.figure(figsize=(14, 9), facecolor="#0b1020")
    grid = fig.add_gridspec(2, 2, height_ratios=[0.75, 2.6], hspace=0.32, wspace=0.32)
    fig.suptitle("Live Pairs Bot — P&L", fontsize=23, weight="bold", y=0.995)
    fig.text(
        0.5,
        0.957,
        "Closed trades use the journal's fee/slippage model; open trades estimate net P&L if closed now",
        ha="center",
        color="#a9b4cc",
        fontsize=10.5,
    )

    ax0 = fig.add_subplot(grid[0, :])
    ax0.axis("off")
    cards = [
        ("Realized", f"USD {realized_total:+.2f}", f"{len(rows)} trades • {wins} winners"),
        ("Open if closed now", f"USD {open_total:+.2f}", f"{len(open_estimates)} open pairs"),
        ("Combined estimate", f"USD {realized_total + open_total:+.2f}", "before actual exit fills"),
        ("Untracked residuals", str(len(untracked)), "excluded from pair totals"),
    ]
    colors = ["#2563eb", "#d97706", "#7c3aed", "#ef4444"]
    for i, ((title, value, note), color) in enumerate(zip(cards, colors)):
        x = 0.015 + i * 0.247
        ax0.add_patch(
            plt.Rectangle(
                (x, 0.08),
                0.225,
                0.78,
                transform=ax0.transAxes,
                facecolor="#141c30",
                edgecolor=color,
                linewidth=2,
            )
        )
        ax0.text(x + 0.018, 0.68, title, transform=ax0.transAxes, color="#a9b4cc", fontsize=10)
        ax0.text(x + 0.018, 0.39, value, transform=ax0.transAxes, fontsize=18, weight="bold")
        ax0.text(x + 0.018, 0.17, note, transform=ax0.transAxes, color="#718096", fontsize=8.5)

    ax1 = fig.add_subplot(grid[1, 0])
    ordered = sorted(realized.items(), key=lambda item: item[1])
    ax1.barh(
        [item[0] for item in ordered],
        [item[1] for item in ordered],
        color=["#22c55e" if item[1] > 0 else "#ef4444" for item in ordered],
    )
    ax1.axvline(0, color="white", linewidth=1)
    ax1.set_xlabel("Aggregate realized net P&L (USD)")
    ax1.set_title("Closed performance by pair", loc="left")
    ax1.grid(axis="x", alpha=0.15)

    ax2 = fig.add_subplot(grid[1, 1])
    ordered_open = sorted(open_estimates.items(), key=lambda item: item[1])
    ax2.barh(
        [item[0] for item in ordered_open],
        [item[1] for item in ordered_open],
        color=["#22c55e" if item[1] > 0 else "#ef4444" for item in ordered_open],
    )
    ax2.axvline(0, color="white", linewidth=1)
    ax2.set_xlabel("Estimated net P&L if closed now (USD)")
    ax2.set_title("Currently open pairs", loc="left")
    ax2.grid(axis="x", alpha=0.15)
    if untracked:
        detail = ", ".join(
            f"{row['coin']} {float(row['szi']):+.4g}" for row in untracked
        )
        fig.text(
            0.5,
            0.018,
            f"Risk flag: exchange exposure not represented by an open pair: {detail}",
            ha="center",
            color="#fbbf24",
            fontsize=10.5,
            weight="bold",
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(OUTPUT)


if __name__ == "__main__":
    main()
