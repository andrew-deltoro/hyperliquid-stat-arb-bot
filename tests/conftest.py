import os
import sys
import tempfile
from pathlib import Path

# paths.py creates runtime dirs on import; keep them out of the repo.
os.environ["HLBOT_RUNTIME_DIR"] = tempfile.mkdtemp(prefix="hlbot-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from src.hyperliquid_bot.phase3 import journal, state, testnet_safeguards  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "POSITIONS_FILE", tmp_path / "positions.json")
    monkeypatch.setattr(testnet_safeguards, "DAILY_ORDERS_FILE", tmp_path / "daily_orders.json")
    monkeypatch.setattr(testnet_safeguards, "STATE_DIR", tmp_path)
    monkeypatch.setattr(journal, "TRADES_FILE", tmp_path / "trades.csv")
    monkeypatch.setattr(journal, "SIGNALS_FILE", tmp_path / "signals.csv")
    return tmp_path
