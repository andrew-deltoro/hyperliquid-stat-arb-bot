"""Run one scheduler cycle (for testing/cron)."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.hyperliquid_bot.phase3.scheduler import run_cycle


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None,
                        help="Path to config YAML (defaults to config/phase3.yaml)")
    args = parser.parse_args()

    log = run_cycle(config_path=args.config)
    print(f"\nLog: {log}")
