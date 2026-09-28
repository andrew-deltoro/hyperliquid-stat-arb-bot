"""Diagnose Hyperliquid wallet setup: verify API wallet, main wallet, balance."""
import os
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.utils import constants

load_dotenv()

pk = os.getenv("HL_PRIVATE_KEY")
acct_addr = os.getenv("HL_ACCOUNT_ADDRESS")

if not pk:
    print("ERROR: HL_PRIVATE_KEY not set in .env")
    raise SystemExit(1)
if not acct_addr:
    print("ERROR: HL_ACCOUNT_ADDRESS not set in .env")
    raise SystemExit(1)

api_addr = Account.from_key(pk).address

print("=" * 60)
print("WALLET DIAGNOSTIC")
print("=" * 60)
print(f"API wallet (from PK):    {api_addr}")
print(f"Main wallet (from env):  {acct_addr}")
print()

info = Info(constants.TESTNET_API_URL, skip_ws=True)

# --- Main wallet state ---
print("--- Main wallet state ---")
try:
    state = info.user_state(acct_addr)
    margin = state.get("marginSummary", {})
    account_value = float(margin.get("accountValue", 0))
    print(f"  Balance:         ${account_value:.2f}")
    positions = state.get("assetPositions", [])
    print(f"  Open positions:  {len(positions)}")
    for p in positions:
        pos = p.get("position", {})
        coin = pos.get("coin", "?")
        szi = pos.get("szi", "0")
        print(f"    {coin}: size={szi}")
except Exception as e:
    print(f"  ERROR: {e}")

print()

# --- API wallet state ---
print("--- API wallet state ---")
try:
    api_state = info.user_state(api_addr)
    api_margin = api_state.get("marginSummary", {})
    api_value = float(api_margin.get("accountValue", 0))
    print(f"  Balance:         ${api_value:.2f}")
except Exception as e:
    print(f"  ERROR: {e}")

print()
print("=" * 60)
print("INTERPRETATION:")
print("  - Main wallet should show $1000 (mock USDC)")
print("  - API wallet should show $0 (it's just a signer)")
print("  - If main shows $0, the wrong address is in .env")
print("=" * 60)
