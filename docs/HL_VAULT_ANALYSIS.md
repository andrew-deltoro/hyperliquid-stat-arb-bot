# Hyperliquid Vault Ecosystem Analysis (2026-10-02)

## Operator Economics
- Creation cost: 10,000 USDC gas fee
- Min leader deposit: 100 USDC
- Profit share to leader: 10% default
- Required self-allocation: >=5% of TVL at all times
- **Critical restriction:** vaults can ONLY trade validator-operated perps.
  No spot. No HIP-3. This excludes our funding-arb strategy entirely.

## Top Strategies (ranked by Sharpe where available)
| Vault | Strategy | Return | Sharpe | Notes |
|---|---|---|---|---|
| Overdose | Low-risk BTC/ETH + occasional alts | +77.3% (3mo) | 1.2 | "Low-risk" still delivers |
| Edge & Hedge | Quant delta-neutral, 3x lev | +18.9% (1mo) | ~2.0 | Backtest CAGR ~110% |
| AceVault Hyper01 | HFT | 127% monthly APR | N/A | 28% win rate, 3.71 profit factor |
| Tera Liquid | Systematic trend + funding analysis | 203.5% annualized | N/A | Diversified systematic |

## Vault-of-Vaults Model
- Fund-of-funds allocating across other vaults
- Backtest: 1:4 return/drawdown ratio
- Projected: 20-80% CAGR
- Our Bayesian framework could rank/allocate to underlying vaults

## The Blow-Up Risk (Critical)
- May 2026: A top-performing HFT vault erased most gains because the
  operator made a discretionary directional bet on HYPE.
- **Lesson:** Even data-driven operators blow up when they deviate
  from their core edge into directional gambling.

## Implications for Our Bot
1. **Delta-neutral + stat-arb vaults are validated** on HL
   (Edge & Hedge, Tera Liquid)
2. **Our funding-arb strategy cannot run in a vault** (spot-only restriction)
   -- would need a separate account structure
3. **Vault-of-vaults is a future business model** -- our Bayesian framework
   could rank/allocate to underlying vaults
4. **Never deviate from core edge** -- the HYPE blow-up is a warning
