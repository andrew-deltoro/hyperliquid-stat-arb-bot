# Phase 4 — Testnet Execution: A Study Guide

*Companion notes for the stat-arb bot. Read alongside McElreath,* Statistical Rethinking, *2nd ed.*

**Status: planning document, pre-build. Will be updated as we deploy.**

---

## 0. The one-sentence summary

Phase 1–3 built a bot that runs in **dry-run**: it computes signals, opens and closes positions on paper, and logs everything. Phase 4 wires the bot to Hyperliquid's **testnet** so it places real orders with fake money. Phase 5 flips it to **mainnet** with real capital.

Dry-run → testnet → mainnet is a standard deployment pipeline in algo trading. Each step adds realism while keeping risk bounded.

---

## 1. What "testnet" actually means here

Hyperliquid's testnet is a **complete replica of the mainnet exchange** running on separate infrastructure. Key properties:

- **Real matching engine.** Orders are matched by the same code that runs on mainnet.
- **Real order book.** Other traders place orders; you see real depth.
- **Fake USDC.** Testnet USDC has no value. You can request it from a faucet.
- **Real fills.** When your order crosses, it executes. Slippage is real.
- **Same API.** The SDK works identically; only the base URL differs.

**What testnet does NOT replicate:**
- Mainnet liquidity (testnet order books are thinner)
- Real market impact (fake orders don't move markets)
- Adversarial flow (testnet has fewer bots competing for the same edge)

Testnet is the closest thing to live trading without risking capital.

---

## 2. The Hyperliquid SDK

We'll use `hyperliquid-python-sdk`. Key components:

| Component | Purpose |
|---|---|
| `Info` | Read-only queries: prices, positions, account state, order history |
| `Exchange` | Order placement, cancellation, position management |
| `API` | Low-level HTTP wrapper |

**Installation:**
```bash
pip install hyperliquid-python-sdk
```

**Basic usage:**
```python
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from eth_account import Account

# Load private key from env
account = Account.from_key(os.environ["HL_PRIVATE_KEY"])

# Initialize exchange (testnet)
exchange = Exchange(
    wallet=account,
    base_url="https://api.hyperliquid-testnet.xyz",
    account_address=account.address,
)
info = Info(base_url="https://api.hyperliquid-testnet.xyz", skip_ws=True)
```

**Order placement:**
```python
exchange.order(
    coin="LINK",
    is_buy=True,
    sz=38.77,           # size in coin units
    limit_px=11.35,     # limit price
    order_type={"limit": {"tif": "Ioc"}},  # Immediate-Or-Cancel
    reduce_only=False,
)
```

`Ioc` (Immediate-Or-Cancel) means "fill what you can right now, cancel the rest." This is what we want for stat-arb entries — we don't want unfilled orders sitting on the book.

---

## 3. Safe API key handling (McElreath would approve)

The Hyperliquid API key signs every order. If leaked, an attacker can trade on your behalf but **cannot withdraw funds** — Hyperliquid separates trading and withdrawal permissions.

**Best practices:**

1. **Use an API key, not your wallet's main private key.** Hyperliquid lets you generate API wallets that can sign trades but not transfers.
2. **Store in `.env` file, chmod 600.** Never commit to git.
3. **Load via environment variable, never hardcode.**
4. **Rotate quarterly.** Generate a new key, update `.env`, revoke the old one.

**The `.env` file:**
```bash
HL_PRIVATE_KEY=0x...
HL_ACCOUNT_ADDRESS=0x...
HL_NETWORK=testnet
```

**The systemd service reads it via `EnvironmentFile=`:**
```ini
[Service]
EnvironmentFile=/home/hlbot/hyperliquid-stat-arb-bot/.env
```

---

## 4. The safeguards (all four must be in place before testnet)

### Safeguard 1: Hard cap on order size
```python
MAX_NOTIONAL_PER_ORDER = 50.0  # USD
```
Any order larger than this is refused before the SDK is called.

### Safeguard 2: Total exposure cap
```python
MAX_TOTAL_EXPOSURE_USD = 100.0
```
Sum of all open positions can't exceed this. Checked before every open.

### Safeguard 3: Daily order circuit breaker
```python
MAX_DAILY_ORDERS = 20
```
If the bot places more than 20 orders in a 24h window, it stops. Prevents runaway loops.

### Safeguard 4: Kill switch
```python
KILL_SWITCH_FILE = "/home/hlbot/hyperliquid-stat-arb-bot/KILL"
```
If this file exists, the executor refuses to place any order. Create with `touch KILL`, delete with `rm KILL`.

**The four together mean:** even if the model goes haywire, the most you can lose is bounded. This is the McElreath Ch 6 idea applied to operations — don't trust the model to be right; bound the consequences if it's wrong.

---

## 5. The four-phase deployment

### Phase 4.1 — Build (Sunday, Sep 27)
- Install SDK
- Implement `LiveExecutor.place_order()`
- Add safeguards
- Unit test with a mock
- **Deliverable:** code ready to run

### Phase 4.2 — Manual test (Monday, Sep 28)
- Fund testnet wallet
- Place one manual order via a script
- Verify it fills
- Inspect fill price vs expected
- **Deliverable:** confirmation the SDK works end-to-end

### Phase 4.3 — Automated testnet (Tuesday, Sep 29)
- Flip `executor.mode: "testnet"` in config
- Systemd cycles begin placing real testnet orders
- Monitor for 3 days (Tue/Wed/Thu)
- **Deliverable:** 3 days of testnet trade history

### Phase 4.4 — Go/no-go (Friday morning, Oct 2)
- Review testnet performance
- If clean → flip to `mode: live` (mainnet) at 5% size
- **Deliverable:** mainnet trading begins

---

## 6. The go/no-go checklist

Before flipping to mainnet, ALL seven must pass:

| # | Check | Pass criteria |
|---|---|---|
| 1 | Orders fill | 100% within 30 seconds |
| 2 | Fill price accuracy | Within 0.1% of mid |
| 3 | Partial fills handled | No crashes |
| 4 | Position tracking | Bot JSON == exchange state |
| 5 | Exits trigger | Target/stop rules fire on real fills |
| 6 | No crashes | 48+ hours clean |
| 7 | PnL within model | Testnet PnL within 20% of model |

**If any fail:** fix, restart the clock. No exceptions.

---

## 7. What could go wrong (and what we'll do)

| Failure | Symptom | Response |
|---|---|---|
| API key invalid | SDK raises `AuthError` | Fix `.env`, restart |
| Insufficient balance | Order rejected | Reduce size or fund wallet |
| Order never fills | Position stays 0 | Increase slippage tolerance |
| Partial fill | Bot thinks it has full position | Reconcile with `Info.user_state()` |
| Network timeout | Cycle crashes | Systemd restarts next cycle |
| Model outputs NaN | Order refused | Safeguard catches it |
| Wrong direction | Bot goes long when it should short | Reconcile and investigate |

**Every failure gets logged.** Every fix gets documented in this file.

---

## 8. The lessons from Phase 3 that apply here

From our week of dry-run:

1. **The model is calibrated.** P=0.9 trades closed at target 4/4 times in the cleanest cases.
2. **β-drift exit was overfiring.** Fixed with posterior-aware logic. Zero false exits since.
3. **SEI/SUI was a false positive.** Strong ΔWAIC but 3 losses in a row. Drop from universe.
4. **Small sample size matters.** 11 trades is not enough to trust. 20+ is better.

**Translation for Phase 4:**
- Trust the signals, but **watch the first 5 trades closely**
- **Reconcile every fill** against the model's expectation
- If actual PnL diverges from model PnL by >20%, pause and diagnose

---

## 9. What "success" looks like

**By Friday Oct 2 morning:**
- 3 days of testnet history
- ~15–20 testnet trades closed
- Win rate >60%
- Positive testnet PnL
- Zero infrastructure failures
- Fill slippage <0.2% on average

**If all true:** flip to mainnet with confidence.

**If not:** extend testnet, diagnose, iterate. No shame in waiting.

---

## 10. If you remember only five things

1. **Testnet is the real exchange with fake money.** Use it to validate execution, not just the model.
2. **API keys can trade but not withdraw.** Hyperliquid separates these. Still store carefully.
3. **Four safeguards bound your risk.** Size cap, exposure cap, order cap, kill switch.
4. **One phase at a time.** Don't flip to mainnet until the testnet checklist passes.
5. **Actual PnL vs model PnL is the ultimate truth.** If they diverge, the model or execution is wrong.
