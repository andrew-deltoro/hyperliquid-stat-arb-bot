"""Test the correlation guard logic without touching the exchange."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.hyperliquid_bot.phase3.correlation_guard import CorrelationGuard


def main():
    guard = CorrelationGuard(cluster_caps={
        "btc_eth": 250.0,
        "alt_l1": 300.0,
        "defi": 300.0,
    })

    # Simulate 2 open positions
    open_positions = [
        {"status": "open", "coin_a": "ETH", "coin_b": "BTC", "notional_usd": 150.0},
        {"status": "open", "coin_a": "SOL", "coin_b": "AVAX", "notional_usd": 150.0},
    ]

    print("=== Test 1: SOL/AVAX (would push alt_l1 to $300) ===")
    r = guard.check("SOL", "AVAX", 150.0, open_positions)
    print(f"  allowed: {r.allowed}")
    print(f"  reason:  {r.reason}")
    print()

    print("=== Test 2: AAVE/COMP (new defi position, $150) ===")
    r = guard.check("AAVE", "COMP", 150.0, open_positions)
    print(f"  allowed: {r.allowed}")
    print(f"  reason:  {r.reason}")
    print()

    print("=== Test 3: UNI/SUSHI (would push defi to $300, cap is $300) ===")
    open_positions.append({
        "status": "open", "coin_a": "AAVE", "coin_b": "COMP",
        "notional_usd": 150.0,
    })
    r = guard.check("UNI", "SUSHI", 100.0, open_positions)
    print(f"  allowed: {r.allowed}")
    print(f"  reason:  {r.reason}")
    print()

    print("=== Test 4: DOGE/WIF (unclustered in this test) ===")
    r = guard.check("DOGE", "WIF", 100.0, open_positions)
    print(f"  allowed: {r.allowed}")
    print(f"  reason:  {r.reason}")


if __name__ == "__main__":
    main()
