import pandas as pd, numpy as np, glob
from pathlib import Path

R = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports")
files = sorted(glob.glob(str(R / "bounce_*.csv")))
if not files:
    print("No bounce CSVs found")
    raise SystemExit(1)

dfs = [pd.read_csv(f) for f in files]
df = pd.concat(dfs, ignore_index=True)
print(f"Loaded {len(files)} files, {len(df)} events total")
print()

counts = df.outcome.value_counts()
total = len(df)
for outcome in ["bounce", "cross", "stall"]:
    n = counts.get(outcome, 0)
    print(f"  {outcome:>8}: {n:>4} ({100*n/max(total,1):>5.1f}%)")
print()

print(f"{'pair':>22}  {'n':>4}  {'bounce%':>8}  {'cross%':>8}  {'stall%':>8}  {'med_h':>8}")
print("-" * 74)
for pair, group in df.groupby("pair"):
    n = len(group)
    b = (group.outcome == "bounce").mean() * 100
    c = (group.outcome == "cross").mean() * 100
    s = (group.outcome == "stall").mean() * 100
    med_h = group.hours_to_min_z.median()
    print(f"{pair:>22}  {n:>4}  {b:>7.1f}%  {c:>7.1f}%  {s:>7.1f}%  {med_h:>7.0f}h")

print()
print("Interpretation:")
print("  bounce% > 60  -> exit at target (do not chase past zero)")
print("  cross%  > 60  -> hold past target (trail can run)")
print("  stall%  > 50  -> exit at target (holding loses)")
