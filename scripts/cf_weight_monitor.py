#!/usr/bin/env python3
"""Prospective monitor for the logged conviction weight (`cf_weight`).

Reads the frozen counterfactual weight that journaling now records for every
signal, joins it to the forward outcome of each OPEN episode, and reports whether
a bounded `cf_weight` tilt beats flat sizing -- with a permutation p-value.

Run this periodically (e.g. monthly). The hypothesis is only interesting if, as
episodes accrue, the Sharpe delta stays positive AND p drops below ~0.05 on a
sample substantially larger than the ~50 episodes of the original study. Until
then, conviction sizing stays OFF.

Run:  .venv/bin/python scripts/cf_weight_monitor.py
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
sys.stdout.reconfigure(line_buffering=True)

import numpy as np  # noqa: E402

from entry_quality_screen import SIGNALS, build  # noqa: E402
from conviction_scaling import episodes  # noqa: E402

HORIZON = "r_target"


def load_cf_weights() -> dict[tuple[str, str], float]:
    out = {}
    for r in csv.DictReader(open(SIGNALS)):
        v = (r.get("cf_weight") or "").strip()
        if v:
            try:
                out[(r["pair"], r["timestamp_iso"])] = float(v)
            except ValueError:
                pass
    return out


def main() -> None:
    rows = build(force=False)
    eps = episodes(rows, HORIZON)
    cf = load_cf_weights()
    kept = []
    for e in eps:
        key = (e["pair"], e["timestamp_iso"])
        if key in cf and e.get(HORIZON) is not None:
            kept.append((cf[key], e[HORIZON]))
    print(f"OPEN episodes: {len(eps)}   with cf_weight: {len(kept)}")
    if len(kept) < 30:
        print("too few episodes to say anything yet -- keep running.")
        return
    w = np.asarray([k[0] for k in kept], float)
    r = np.asarray([k[1] for k in kept], float)

    flat = (r.mean(), r.std(), r.mean() / r.std())
    m = (w * r).sum() / w.sum()
    v = (w * (r - m) ** 2).sum() / w.sum()
    sc = (m, np.sqrt(v), m / np.sqrt(v))
    print(f"flat:   mean {flat[0]*100:+.3f}%  std {flat[1]*100:.3f}%  Sharpe {flat[2]:.4f}")
    print(f"scaled: mean {sc[0]*100:+.3f}%  std {sc[1]*100:.3f}%  Sharpe {sc[2]:.4f}  "
          f"(delta {sc[2]-flat[2]:+.4f})")

    rng = np.random.default_rng(41)
    null = []
    for _ in range(5000):
        ws = rng.permutation(w)
        mm = (ws * r).sum() / ws.sum()
        vv = (ws * (r - mm) ** 2).sum() / ws.sum()
        null.append(mm / np.sqrt(vv) if vv > 0 else np.nan)
    p = float(np.mean(np.asarray(null) >= sc[2]))
    print(f"permutation p = {p:.3f}")
    print("VERDICT:", "looks real -- consider validating further"
          if (p < 0.05 and len(kept) >= 80) else
          "not yet significant / too few episodes -- keep sizing flat")


if __name__ == "__main__":
    main()
