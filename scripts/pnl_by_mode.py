"""
Which exits came from which mode? Old trail/target vs new cross.
Split by entry date cutoff.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

CUTOFF = "2026-10-05"  # cross-exit mode implemented

with open("/home/hlbot/hlbot-runtime/live/state/positions.json") as f:
    state = json.load(f)

closed = [p for p in state["positions"] if p.get("status") == "closed"]

old = [p for p in closed if (p.get("entry_time_iso") or "")[:10] < CUTOFF]
new = [p for p in closed if (p.get("entry_time_iso") or "")[:10] >= CUTOFF]

def stats(rows, label):
    real = [p for p in rows if (p.get("pnl_usd") or 0) != 0]
    if not real:
        print(f"  {label}: 0 real trades")
        return
    pnls = [(p.get("pnl_usd") or 0) for p in real]
    wins = sum(1 for x in pnls if x > 0)
    total = sum(pnls)
    mean = total / len(pnls)
    print(f"  {label}: n={len(real)}  total=${total:>+7.2f}  mean=${mean:>+6.2f}  WR={100*wins/len(real):.1f}%")

print("=== Closed trades by entry mode ===")
stats(old, "OLD (pre-Oct 5, trail/target)")
stats(new, "NEW (post-Oct 5, cross)")
print()

# What modes are the open positions using?
op = [p for p in state["positions"] if p.get("status") == "open"]
print(f"=== Open positions: {len(op)} ===")
for p in op:
    pair = p.get("pair", "?")
    entry = (p.get("entry_time_iso") or "")[:10]
    print(f"  {pair:>20}  entry={entry}")

