"""Testnet smoke test: connect, check balance, place one tiny order, verify."""
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.hyperliquid_bot.phase3.config import load_config
from src.hyperliquid_bot.phase3.executor import LiveExecutor, Order


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/phase3-testnet.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if cfg.raw.get("executor", {}).get("mode") != "live":
        print(f"ERROR: {args.config} has mode != live")
        return

    print("=== LiveExecutor smoke test ===")
    ex = LiveExecutor(cfg, log_fn=print)
    ex._current_total_notional = lambda: 0.0

    # Fetch current mark price so the order is marketable
    print("\n--- Fetching current ETH price ---")
    mids = ex.info.all_mids()
    eth_price = float(mids.get("ETH", 0))
    if eth_price <= 0:
        print(f"ERROR: could not fetch ETH price (got {mids.get('ETH')})")
        return
    print(f"  ETH mark: ${eth_price:.2f}")

    # Compute size for $15 notional
    size = round(15.0 / eth_price, 4)
    print(f"  size for $15: {size} ETH")

    print("\n--- Placing a tiny test order ($15 notional) ---")
    fill = ex.place_order(
        Order(coin="ETH", side="buy", size=size, notional_usd=15.0,
              tag="smoke_test"),
        ref_price=eth_price,
    )
    print(f"\nResult: status={fill.status}  oid={fill.order_id}  "
          f"error={fill.error}")

    print("\n--- Safeguard test: try to place $200 order (should reject) ---")
    fill2 = ex.place_order(
        Order(coin="ETH", side="buy", size=0.05, notional_usd=200.0,
              tag="oversized"),
        ref_price=eth_price,
    )
    print(f"\nResult: status={fill2.status}  error={fill2.error}")

if __name__ == "__main__":
    main()
