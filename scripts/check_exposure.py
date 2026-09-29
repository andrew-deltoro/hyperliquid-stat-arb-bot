"""Check what exposure the bot sees on the live account."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.hyperliquid_bot.phase3.config import load_config
from src.hyperliquid_bot.phase3.executor import LiveExecutor


def main():
    cfg = load_config("config/phase3-live.yaml")
    ex = LiveExecutor(cfg)
    total = ex._current_total_notional()
    cap = float(cfg.raw["safeguards"]["max_total_exposure_usd"])
    print(f"Bot sees total exposure: ${total:.2f}")
    print(f"Safeguard cap:           ${cap:.2f}")
    print(f"Headroom:                ${cap - total:.2f}")


if __name__ == "__main__":
    main()
