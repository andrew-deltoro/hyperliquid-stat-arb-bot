#!/usr/bin/env python3
"""One-shot: backfill `cf_weight` into an existing signals.csv.

The counterfactual weight is a deterministic function of the recorded `z`, so
historical rows can be filled in without look-ahead -- preserving the frozen
rule. Idempotent: rows that already have a value are left alone.

Run:  .venv/bin/python scripts/backfill_cf_weight.py [--path <signals.csv>]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from hyperliquid_bot.phase3.conviction import counterfactual_weight  # noqa: E402

DEFAULT = Path("/home/hlbot/hlbot-runtime/live/reports/signals.csv")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=str(DEFAULT))
    ap.add_argument("--apply", action="store_true", help="write; else dry-run")
    args = ap.parse_args()
    p = Path(args.path)
    rows = list(csv.DictReader(open(p)))
    header = list(rows[0].keys())
    if "cf_weight" not in header:
        header.append("cf_weight")

    filled = kept = 0
    for r in rows:
        if (r.get("cf_weight") or "").strip():
            kept += 1
            continue
        try:
            w = counterfactual_weight(float(r["z"]))
        except (KeyError, TypeError, ValueError):
            w = 1.0
        r["cf_weight"] = f"{w:.4f}"
        filled += 1

    print(f"rows: {len(rows)}   filled: {filled}   already present: {kept}")
    if not args.apply:
        print("dry run -- pass --apply to write")
        return
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    p.with_suffix(p.suffix + ".bak-cf").write_bytes(p.read_bytes())
    tmp.replace(p)
    print(f"wrote {p}  (backup: {p.with_suffix(p.suffix + '.bak-cf')})")


if __name__ == "__main__":
    main()
