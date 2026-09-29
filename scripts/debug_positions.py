"""Debug: dump raw user_state response."""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.utils import constants

load_dotenv()
wallet = os.getenv("HL_ACCOUNT_ADDRESS_MAINNET")
print(f"Wallet: {wallet}")

info = Info(constants.MAINNET_API_URL, skip_ws=True)
state = info.user_state(wallet)

print(f"\nTop-level keys: {list(state.keys())}")
print(f"\nmarginSummary: {json.dumps(state.get('marginSummary', {}), indent=2)}")

positions = state.get("assetPositions", [])
print(f"\nassetPositions: {len(positions)} entries")
for p in positions:
    pos = p.get("position", {})
    print(f"  {json.dumps(pos, indent=2)}")
