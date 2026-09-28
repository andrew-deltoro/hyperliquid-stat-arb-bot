# Phase 4 — Testnet Execution: A Study Guide

*Companion notes for the stat-arb bot. Phase 4 is more engineering than statistics, but every design choice here protects the statistical edge built in Phases 1–3.*

---

## 0. The one-sentence summary

We built a **testnet executor** that places real orders on Hyperliquid's test network using an isolated API wallet, gated by hard-coded safeguards, so we can validate the entire trade pipeline with zero real-money risk before going live on mainnet.

---

## 1. Why testnet exists

A dry-run bot tells you what *would* have happened. A testnet bot tells you what *actually* happens. These are different things.

**What testnet reveals that dry-run can't:**

| Hidden assumption in dry-run | What testnet uncovers |
|---|---|
| "Order fills at the reference price" | Real fills have slippage; testnet liquidity is thin |
| "Order sizes are valid" | Hyperliquid rejects orders under ~$10 notional |
| "API wallet signs correctly" | Signer vs account_address mismatch breaks everything |
| "Account has funds" | Spot vs Perp clearinghouse confusion blocks trades |
| "Order status parsing works" | Top-level `status: ok` doesn't mean the order filled |

**This is the McElreath Ch 2 "small world / large world" pattern applied to engineering.** Dry-run operates in the small world of the model — every assumption holds. Testnet forces you to confront the large world where assumptions break.

---

## 2. The API wallet pattern

Hyperliquid supports two credentials per account:

| Role | What it does | What goes in `.env` |
|---|---|---|
| **Main wallet** | Holds USDC, can withdraw | `HL_ACCOUNT_ADDRESS` (public) |
| **API wallet (agent)** | Places orders on behalf of main | `HL_PRIVATE_KEY` (secret) |

**The API wallet cannot withdraw funds.** Even if the VPS is fully compromised and the API wallet's private key leaks, the attacker can only place orders — not move USDC out.

**This is the single most important security decision in the whole bot.** Never put your main wallet's private key on a VPS. Ever.

### How the SDK uses them

```python
account = Account.from_key(api_wallet_private_key)   # signs orders
exchange = Exchange(account, base_url,
                    account_address=main_wallet_address)  # target account
```

The `Exchange` client needs both:
- The **API wallet's private key** for signing (proof the request came from an authorized agent)
- The **main wallet's address** for targeting (which account's positions to modify)

---

## 3. Account modes: Unified vs Manual

Hyperliquid has three account abstraction modes. Which one you're in determines *where* your USDC lives, and therefore *how* the SDK reads your balance.

| Mode | Spot + Perp balance | SDK reads | Recommended? |
|---|---|---|---|
| **Unified** | Shared collateral | `user_state()` returns **$0** (perp side empty) | ❌ for us |
| **Portfolio Margin** | Cross-margin, requires >$25M | N/A | ❌ not eligible |
| **Manual** | Separate balances per DEX | `user_state()` returns **Perp balance** | ✅ **this is what we use** |

**The symptom:** the UI shows $999, but `info.user_state()` returns `accountValue: 0`.

**The fix:** switch account mode to Manual. In Manual mode, USDC moves to the Perp clearinghouse, where the SDK naturally reads it.

---

## 4. Spot vs Perp balances

Even in Manual mode, USDC can sit in one of two places:

| Balance | Purpose | Who reads it |
|---|---|---|
| **Spot** | Direct token holding | `info.spot_user_state()` |
| **Perp** | Collateral for futures | `info.user_state()` |

**The bot trades perps.** So it needs the balance in Perp, not Spot.

**The faucet deposits to Spot.** This is why the first diagnostic showed $0 even after claiming USDC — the money was in Spot, the SDK was reading Perp.

**The fix:** one-time "Transfer to Perps" from the Hyperliquid UI, or call `exchange.usdc_transfer()` programmatically.

---

## 5. Order status parsing (the trap)

This is the subtlest bug in Phase 4, and it nearly cost us hours.

The Hyperliquid SDK returns a **nested response** for every order:

```json
{
  "status": "ok",                        ← top-level: request accepted
  "response": {
    "data": {
      "statuses": [
        {"error": "Order must be at least $10"}  ← actual outcome
      ]
    }
  }
}
```

**`status: "ok"` means "the exchange received your request."** It does **not** mean the order filled. A `$5` order will return `status: "ok"` but include `{"error": "..."}` inside. A naive parser (like ours initially was) reports a false fill.

**The correct parse:**

```python
if statuses[0].get("error"):
    → rejected
elif statuses[0].get("filled"):
    → market fill, capture oid
elif statuses[0].get("resting"):
    → limit order placed, capture oid
```

**Why this matters:** if we'd trusted the top-level status, the bot would have logged phantom fills and thought it had open positions that didn't exist. Silent corruption of the state file. Exactly the kind of bug that "works" in a demo and blows up live.

---

## 6. Minimum order sizes and slippage

Two exchange-level constraints that dry-run can't reveal:

**Minimum notional: $10.** Orders under $10 USDC value get rejected. Our first smoke test used $5 — silently failed.

**Slippage tolerance.** When placing a market order, you specify a slippage parameter. Hyperliquid computes a limit price at `ref_price × (1 ± slippage)` and only fills against resting orders within that band. On testnet, liquidity is thin; a 1% slippage band may not reach any sellers.

**We use 3% on testnet.** Configurable via `config/phase3.yaml`:
```yaml
executor:
  slippage_tolerance: 0.03
```

On mainnet, 1% is usually enough. Testnet needs more.

---

## 7. Safeguards (the safety net)

Before any live order reaches the exchange, it passes through `Safeguards.check_order()`:

| Check | Threshold | What it prevents |
|---|---|---|
| **Kill switch** | `KILL` file exists → refuse all | Emergency stop |
| **Per-order notional** | ≤ $50 | A miscomputed size from wiping the account |
| **Total exposure** | ≤ $100 | Stacking many small positions |
| **Daily order count** | ≤ 20 | Runaway loops firing hundreds of orders |

**These are hard gates, not soft warnings.** If any fails, the order is rejected before hitting the network. The daily counter persists to `state/daily_orders.json` so it survives bot restarts.

**The KILL switch** is a plain file check. `touch KILL` in the project root and every subsequent order is refused. `rm KILL` to resume. No restart needed.

---

## 8. The dry-run → testnet → mainnet pipeline

Each stage adds realism at the cost of complexity:

| Stage | Real orders? | Real money? | Purpose |
|---|---|---|---|
| **Dry-run** | No | No | Strategy validation |
| **Testnet** | Yes | No (mock USDC) | Execution validation |
| **Mainnet (small)** | Yes | Yes ($10–50) | First live trial |
| **Mainnet (full)** | Yes | Yes (100%) | Production |

**Never skip a stage.** Each one catches a distinct class of bugs:
- Dry-run catches logic bugs (wrong β, wrong exits)
- Testnet catches execution bugs (signing, order format, funds location)
- Small mainnet catches real-market bugs (real slippage, real liquidity)
- Full mainnet is only reached after all three prove themselves

---

## 9. The mainnet safety checklist

Before the bot ever touches mainnet:

- [ ] Config `network: "testnet"` verified in every file
- [ ] API wallet approved on **testnet only**, not mainnet
- [ ] `.env` permissions are `600`, owned by `hlbot`, gitignored
- [ ] Safeguards validated (oversized order → rejected)
- [ ] Kill switch tested (`touch KILL` → next order rejected)
- [ ] Testnet has run for 5+ days without incident
- [ ] Testnet fills match dry-run predictions within slippage tolerance
- [ ] Small mainnet test planned ($10–20 first order, not $50)

**If any box is unchecked, do not proceed.** Real money has real consequences.

---

## 10. If you remember only five things

1. **API wallets can trade but not withdraw.** Use them on any VPS. Never deploy a main wallet key.
2. **`status: ok` in the SDK response is not a fill.** Parse the nested `statuses` array for `filled` / `resting` / `error`.
3. **Spot and Perp are separate balance sheets.** The bot trades perps; USDC must be in Perp.
4. **Minimum $10 order size on Hyperliquid.** Small test orders will silently fail.
5. **Three-stage pipeline: dry → testnet → small mainnet.** Skipping stages is how real money dies.
