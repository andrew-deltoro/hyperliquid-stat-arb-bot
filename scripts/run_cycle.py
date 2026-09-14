"""Run one scheduler cycle (for testing/cron)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.hyperliquid_bot.phase3.scheduler import run_cycle

if __name__ == "__main__":
    log = run_cycle()
    print(f"\nLog: {log}")