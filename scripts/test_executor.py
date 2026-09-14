"""Full lifecycle test: open → hold → close → PnL.

Fake signal is internally consistent:
  entry prices 100 / 50
  alpha 1.0, mean_raw 0.0  ->  entry_spread = log(100) - 1 - log(50) ≈ -0.3069
  target = 0.5 * entry_spread ≈ -0.1534
  stop   = 3.0 * entry_spread ≈ -0.9206
"""
import sys
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent
STATE = REPO / "state" / "positions.json"
TRADES = REPO / "reports" / "trades.csv"
SIGNALS = REPO / "reports" / "signals.csv"

for f in (STATE, TRADES, SIGNALS):
    if f.exists():
        f.unlink()
        print(f"deleted {f}")

from src.hyperliquid_bot.phase3.config import load_config
from src.hyperliquid_bot.phase3.positions import PositionManager
from src.hyperliquid_bot.phase3.executor import build_executor
from src.hyperliquid_bot.phase3.signals import Signal


ENTRY_A = 100.0
ENTRY_B = 50.0
ALPHA = 1.0
MEAN_RAW = 0.0


def main():
    cfg = load_config()

    # Compute entry spread consistent with alpha/mean/prices
    entry_spread = (
        math.log(ENTRY_A) - ALPHA - 1.0 * math.log(ENTRY_B) - MEAN_RAW
    )
    print(f"\nconsistent entry_spread = {entry_spread:+.5f}")
    print(f"target_spread = {0.5 * entry_spread:+.5f}")
    print(f"stop_spread   = {3.0 * entry_spread:+.5f}")

    executor = build_executor(cfg, log_fn=print)
    pm = PositionManager(cfg, executor=executor)

    fake = Signal(
        pair="TEST/FAKE",
        coin_a="TEST", coin_b="FAKE",
        beta=1.0, beta_sd=0.01,
        nu_coint=10.0, converged_coint=True,
        alpha=ALPHA, mean_raw=MEAN_RAW, sigma_raw=0.05,
        current_spread=entry_spread,
        spread_sd=0.05, z=-3.0,
        kappa=0.02, half_life_hours=34.0,
        sigma_ou=0.005, nu_ou=4.0, converged_ou=True,
        p_revert=0.90, edge=0.40, notional=80.0,
        direction="LONG_A_SHORT_B", action="OPEN",
    )

    r = pm.try_open(fake, ENTRY_A, ENTRY_B)
    print(f"\ntry_open → {r['opened']}")

    # ---------- Test 1: mild favorable move → hold ----------
    print("\n--- Test 1: mild favorable move (a=101, b=49.5) → should HOLD ---")
    result = pm.evaluate_open("TEST/FAKE", 101.0, 49.5, fresh_beta=1.0)
    print(f"evaluate_open → {result['action']} "
          f"({result.get('reason', '')})")

    # ---------- Test 2: strong favorable move → target ----------
    print("\n--- Test 2: strong favorable move (a=110, b=45) → should HIT TARGET ---")
    result = pm.evaluate_open("TEST/FAKE", 110.0, 45.0, fresh_beta=1.0)
    print(f"evaluate_open → {result['action']} "
          f"({result.get('reason', '')})")

    print("\n--- trades.csv ---")
    if TRADES.exists():
        print(TRADES.read_text())


if __name__ == "__main__":
    main()