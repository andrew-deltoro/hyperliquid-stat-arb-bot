"""Quick health check for the dry-run."""
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
TRADES = REPO / "reports" / "trades.csv"
SIGNALS = REPO / "reports" / "signals.csv"
STATE = REPO / "state" / "positions.json"
LOGS = REPO / "logs"

print("=" * 60)
print("DRY-RUN HEALTH CHECK")
print("=" * 60)

# --- Logs ---
cycle_logs = sorted(LOGS.glob("cycle_*.log"))
if cycle_logs:
    latest = cycle_logs[-1]
    print(f"\nLatest cycle: {latest.name}")
    print(f"  ({datetime.fromtimestamp(latest.stat().st_mtime)})")
else:
    print("\nNo cycle logs yet.")

# --- Open positions ---
if STATE.exists():
    import json
    s = json.loads(STATE.read_text())
    open_pos = [p for p in s["positions"] if p["status"] == "open"]
    print(f"\nOpen positions: {len(open_pos)}")
    for p in open_pos:
        print(f"  {p['pair']:12s}  ${p['notional_usd']:6.2f}  "
              f"{p['direction']}  "
              f"HL={p['half_life_hours']:.1f}h  "
              f"opened {p['entry_time_iso'][:16]}")
    total = sum(p["notional_usd"] for p in open_pos)
    print(f"  Total notional: ${total:.2f}")
else:
    print("\nNo open positions.")

# --- Trades ---
if TRADES.exists():
    t = pd.read_csv(TRADES)
    print(f"\nClosed trades: {len(t)}")
    if len(t):
        wins = (t.pnl_usd > 0).sum()
        print(f"  Win rate:  {wins}/{len(t)} = {wins/len(t):.1%}")
        print(f"  Total PnL: ${t.pnl_usd.sum():+.2f}")
        print(f"  Avg PnL:   ${t.pnl_usd.mean():+.2f}")
        print(f"  Avg dur:   {t.elapsed_hours.mean():.1f}h")
        print(f"  Reasons:   {dict(t.exit_reason.value_counts())}")

# --- Signals ---
if SIGNALS.exists():
    s = pd.read_csv(SIGNALS)
    print(f"\nSignals logged: {len(s)}")
    if len(s):
        opens = (s.action == "OPEN").sum()
        print(f"  OPEN:  {opens}")
        print(f"  SKIP:  {len(s) - opens}")
        print(f"  Most common skip: "
              f"{s[s.action=='SKIP'].skip_reason.value_counts().head(1).to_dict()}")

print("\n" + "=" * 60)