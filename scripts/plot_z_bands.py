#!/usr/bin/env python3
"""Draw the z-band picture for one pair: entry, target, stop and cross levels.

Illustrative figure for the Bollinger/mean-reversion explainer. Uses the live
signal tape for the pair and the *actual* entry levels frozen in positions.json.

Run:  .venv/bin/python scripts/plot_z_bands.py
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
sys.stdout.reconfigure(line_buffering=True)

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from datetime import datetime  # noqa: E402

SIGNALS = Path("/home/hlbot/hlbot-runtime/live/reports/signals.csv")
POS = Path("/home/hlbot/hlbot-runtime/live/state/positions.json")
OUT = REPO / "reports" / "figs"


def main(pair: str = "AIXBT/DOGE") -> None:
    rows = [r for r in csv.DictReader(open(SIGNALS)) if r["pair"] == pair]
    rows.sort(key=lambda r: r["timestamp_iso"])
    pos = next(p for p in json.loads(POS.read_text())["positions"]
               if p.get("pair") == pair and p.get("status") == "open")

    t = [datetime.fromisoformat(r["timestamp_iso"].replace("Z", "+00:00")) for r in rows]
    z = [float(r["z"]) for r in rows]

    sig = float(pos["entry_sigma_raw"])
    entry_spread = float(pos["entry_spread"])
    z_entry = entry_spread / sig
    z_target = float(pos["target_spread"]) / sig          # target_frac
    z_stop = float(pos["stop_spread"]) / sig              # stop_mult
    cross_frac = 0.75
    z_cross = -cross_frac * z_entry                        # opposite side

    fig, ax = plt.subplots(figsize=(11, 5.2))
    # Bollinger-style envelope around zero, drawn as BANDS with visible lines.
    # +/-(1,2) sigma in z units -> z of +/-(1,2).
    ax.axhspan(-1, 1, color="#4c78a8", alpha=0.13, lw=0, zorder=0)
    ax.axhspan(-2, -1, color="#4c78a8", alpha=0.26, lw=0, zorder=0)
    ax.axhspan(1, 2, color="#4c78a8", alpha=0.26, lw=0, zorder=0)
    for lvl, lab in ((-2, "−2σ band"), (-1, "−1σ band"),
                     (0, "mean (z=0)"), (1, "+1σ band"), (2, "+2σ band")):
        kw = dict(color="#4c78a8" if lvl else "#333",
                  ls="-", lw=1.6 if lvl else 1.2,
                  alpha=0.85 if lvl else 1.0, zorder=3)
        ax.axhline(lvl, **kw)
        ax.annotate(lab, xy=(0.004, lvl), xycoords=("axes fraction", "data"),
                    xytext=(0, 2), textcoords="offset points",
                    fontsize=8, color="#2c5d8a" if lvl else "#333",
                    ha="left", va="bottom", zorder=8)
    ax.axhline(z_entry, color="#d62728", ls="--", lw=1.4)
    ax.axhline(z_stop, color="#8c1c13", ls=":", lw=1.6)
    ax.axhline(z_target, color="#2ca02c", ls="--", lw=1.4)
    ax.axhline(z_cross, color="#9467bd", ls="-.", lw=1.6)

    ax.plot(t, z, color="#1f2937", lw=1.7, zorder=5)

    ent = datetime.fromisoformat(pos["entry_time_iso"].replace("Z", "+00:00"))
    ax.axvline(ent, color="#555", lw=1.0, alpha=0.7)

    # exit-level labels on the RIGHT edge (band labels own the left edge)
    for lvl, lab, col in (
        (z_entry, f"entry {z_entry:+.2f}", "#d62728"),
        (z_target, f"target {z_target:+.2f} (0.75×)", "#2ca02c"),
        (z_stop, f"stop {z_stop:+.2f} (3.0×)", "#8c1c13"),
        (z_cross, f"cross exit {z_cross:+.2f}", "#9467bd"),
    ):
        ax.annotate(lab, xy=(0.996, lvl), xycoords=("axes fraction", "data"),
                    xytext=(0, 2), textcoords="offset points",
                    fontsize=8.5, color=col, ha="right", va="bottom", zorder=9)

    ax.annotate("entry", xy=(ent, ax.get_ylim()[0]), xytext=(6, 6),
                textcoords="offset points", fontsize=8, color="#555", rotation=90)

    ax.set_title(f"{pair} — z-score of the cointegration residual with live exit levels\n"
                 "bands: ±1σ / ±2σ envelope (a Bollinger band applied to the SPREAD, not a price)",
                 fontsize=11)
    ax.set_ylabel("z (σ units from the residual mean)")
    ax.grid(alpha=0.25)
    ax.margins(x=0.01)
    fig.autofmt_xdate()
    fig.tight_layout()
    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / "z_bands_example.png"
    fig.savefig(f, dpi=150)
    print(f"wrote {f}")
    print(f"  z_entry={z_entry:+.3f} target={z_target:+.3f} stop={z_stop:+.3f} "
          f"cross={z_cross:+.3f}  last z={z[-1]:+.3f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "AIXBT/DOGE")
