"""
Closed trade + live position analysis.
Run: python scripts/analyze_pnl.py
"""
import json
from datetime import datetime, timezone
from pathlib import Path

STATE_PATH = Path("/home/hlbot/hlbot-runtime/live/state/positions.json")


def load():
    with open(STATE_PATH) as f:
        return json.load(f)


def fmt_time(iso):
    if not iso:
        return "?"
    return iso[:16].replace("T", " ")


def main():
    state = load()
    closed = [p for p in state["positions"] if p.get("status") == "closed"]
    open_p = [p for p in state["positions"] if p.get("status") == "open"]

    # Sort by exit time, treating None as empty string
    closed.sort(key=lambda p: p.get("exit_time_iso") or "", reverse=True)

    print(f"Total closed trades: {len(closed)}")
    print()

    # Group by reason
    reasons = {}
    for p in closed:
        r = p.get("exit_reason", "unknown")
        reasons.setdefault(r, []).append(p)

    print("=== Exit reason breakdown ===")
    for r, trades in sorted(reasons.items(), key=lambda x: -len(x[1])):
        total_pnl = sum(t.get("pnl_usd", 0) for t in trades)
        mean_pnl = total_pnl / len(trades) if trades else 0
        print(f"  {r:>20}: n={len(trades):>3}  total=${total_pnl:>+8.2f}  mean=${mean_pnl:>+6.2f}")
    print()

    print("=== Last 15 closed trades ===")
    print(f"{'pair':>20}  {'entry':>16}  {'exit':>16}  {'reason':>18}  {'pnl':>9}")
    print("-" * 90)
    for p in closed[:15]:
        pair = p.get("pair", "?")
        e = fmt_time(p.get("entry_time_iso"))
        x = fmt_time(p.get("exit_time_iso"))
        r = p.get("exit_reason", "?")
        pnl = p.get("pnl_usd", 0) or 0
        print(f"{pair:>20}  {e:>16}  {x:>16}  {r:>18}  ${pnl:>+8.2f}")
    print()

    # Summary stats
    total_realized = sum(p.get("pnl_usd", 0) or 0 for p in closed)
    now = datetime.now(timezone.utc)
    recent = []
    for p in closed:
        try:
            t = datetime.fromisoformat(p.get("exit_time_iso") or "")
            if (now - t).days <= 7:
                recent.append(p)
        except Exception:
            pass
    recent_pnl = sum(p.get("pnl_usd", 0) or 0 for p in recent)

    print("=== Realized PnL ===")
    print(f"  All time:    ${total_realized:>+8.2f}  ({len(closed)} trades)")
    print(f"  Last 7 days: ${recent_pnl:>+8.2f}  ({len(recent)} trades)")
    print()

    print("=== Open positions ===")
    print(f"  Count: {len(open_p)}")
    total_notional = sum(p.get("notional_usd", 0) for p in open_p)
    print(f"  Total notional: ${total_notional:.2f}")
    print()

    if closed:
        wins = [p for p in closed if (p.get("pnl_usd") or 0) > 0]
        losses = [p for p in closed if (p.get("pnl_usd") or 0) < 0]
        win_rate = 100 * len(wins) / len(closed)
        avg_win = sum((p.get("pnl_usd") or 0) for p in wins) / len(wins) if wins else 0
        avg_loss = sum((p.get("pnl_usd") or 0) for p in losses) / len(losses) if losses else 0
        ratio = abs(avg_win / avg_loss) if avg_loss else 0
        print("=== Stats ===")
        print(f"  Win rate: {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Avg win:  ${avg_win:>+7.2f}")
        print(f"  Avg loss: ${avg_loss:>+7.2f}")
        print(f"  Ratio:    {ratio:.2f}  (need >1.0 for profit at 50% WR)")


if __name__ == "__main__":
    main()
