"""
Reduce-only flatten of exchange positions that are not in open state.

Default orphan list matches the 2026-10-07 reconcile: COMP, GMT, SNX.
Does not touch coins that belong to an open / needs_review position.

Usage:
  HLBOT_RUNTIME_DIR=/home/hlbot/hlbot-runtime/live \\
    .venv/bin/python scripts/flatten_orphans.py --config config/phase3-live.yaml
  # dry-run (default): prints what would close
  # --execute: place reduce-only IOC closes
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
# Prefer the live checkout's .env when this script is run from a worktree.
for candidate in (
    Path("/home/hlbot/hyperliquid-stat-arb-bot/.env"),
    REPO / ".env",
):
    if candidate.exists():
        load_dotenv(candidate, override=False)
        break

from src.hyperliquid_bot.phase3.config import load_config
from src.hyperliquid_bot.phase3.executor import LiveExecutor, Order
from src.hyperliquid_bot.phase3.state import load_state


DEFAULT_ORPHANS = ["COMP", "GMT", "SNX"]


def tracked_coins(state) -> set[str]:
    out = set()
    for p in state.positions:
        if p.get("status") in ("open", "needs_review"):
            out.add(p["coin_a"])
            out.add(p["coin_b"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/phase3-live.yaml")
    ap.add_argument("--coins", nargs="*", default=DEFAULT_ORPHANS)
    ap.add_argument("--execute", action="store_true",
                    help="Actually place reduce-only closes (off = dry-run)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if cfg.raw.get("executor", {}).get("mode") != "live":
        raise SystemExit("config executor.mode must be live")

    state = load_state()
    tracked = tracked_coins(state)
    print(f"tracked by open/review state: {sorted(tracked)}")

    ex = LiveExecutor(cfg, log_fn=print)
    live = ex.all_positions()
    if live is None:
        raise SystemExit("could not fetch exchange positions")
    print(f"live nonzero: {live}")

    targets = []
    for coin in args.coins:
        if coin in tracked:
            print(f"SKIP {coin}: still referenced by open/review state")
            continue
        szi = live.get(coin, 0.0)
        if szi == 0:
            print(f"SKIP {coin}: flat on exchange")
            continue
        targets.append((coin, szi))

    if not targets:
        print("nothing to flatten")
        return

    for coin, szi in targets:
        side = "sell" if szi > 0 else "buy"
        size = abs(szi)
        # Mid for slippage reference
        try:
            mids = ex.info.all_mids()
            px = float(mids.get(coin, 0) or 0)
        except Exception:
            px = 0.0
        if px <= 0:
            print(f"SKIP {coin}: no mid price")
            continue
        notional = size * px
        print(f"{'EXECUTE' if args.execute else 'DRY-RUN'} "
              f"reduce_only {side} {size} {coin} (~${notional:.2f}) "
              f"to flatten szi={szi:+.6f}")
        if not args.execute:
            continue
        fill = ex.place_order(
            Order(coin=coin, side=side, size=size, notional_usd=notional,
                  tag="orphan_flatten", reduce_only=True),
            ref_price=px,
        )
        print(f"  -> status={fill.status} size={fill.size} px={fill.price} "
              f"err={fill.error!r}")
        after = ex.get_position(coin)
        print(f"  -> live szi now {after}")

    if not args.execute:
        print("\nRe-run with --execute to place the reduce-only closes.")


if __name__ == "__main__":
    main()
