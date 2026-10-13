"""Score the Kalshi tape. Refuses a verdict until the locked minimum.

The first run that reaches the minimum writes the verdict file and later
runs print that file instead of cutting the sample again.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path("/home/hlbot/hyperliquid-stat-arb-bot")
sys.path.insert(0, str(ROOT / "src"))

from hyperliquid_bot.kalshi_hypothesis import score_joined, walk_forward  # noqa: E402
CONFIG_PATH = ROOT / "config" / "kalshi_hl_hypothesis.yaml"


def load_tape(path: Path) -> tuple[list[dict], dict[str, dict]]:
    decisions: list[dict] = []
    settlements: dict[str, dict] = {}
    if not path.exists():
        return decisions, settlements
    with path.open() as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("type") == "decision":
                decisions.append(row)
            elif row.get("type") == "settlement" and row.get("ticker"):
                settlements[row["ticker"]] = row
    return decisions, settlements


def main() -> None:
    with CONFIG_PATH.open() as handle:
        cfg = yaml.safe_load(handle)
    verdict_path = Path(cfg["outputs"]["verdict"])
    if verdict_path.exists():
        print(verdict_path.read_text())
        return
    decisions, settlements = load_tape(Path(cfg["outputs"]["tape"]))
    scored = score_joined(decisions, settlements, cfg["test"]["series"])
    result = walk_forward(
        scored,
        minimum=int(cfg["minimum_scored"]),
        scatter_frac=float(cfg["walk_forward"]["scatter_frac"]),
    )
    result["hypothesis_id"] = cfg["hypothesis_id"]
    result["series"] = cfg["test"]["series"]
    if not result["ready"]:
        print(
            f"{result['hypothesis_id']}: {result['scored']} scored, "
            f"{result['remaining']} to go before a verdict"
        )
        return
    result["kill"] = result["scatter_mean_pnl"] <= float(cfg["walk_forward"]["kill_if_scatter_mean_pnl_leq"])
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    verdict_path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
