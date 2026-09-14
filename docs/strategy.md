# Strategy — Statistical Pairs Trading

## Overview

Build a stat arb bot on correlated Hyperliquid perp pairs. Track spread z-score, enter mean-reversion trades on divergence, exit on convergence with hedge ratios.

## Detailed Methodology

Two correlated Hyperliquid perp pairs are tracked. Spread z-score is computed from rolling ratio history. Entry at |z| > 2, exit at |z| < 0.5. Hedge ratio maintained via linear regression.

**Optimal regime:** Pairs with historical cointegration on Hyperliquid (e.g., ETH/BTC ratio).

## Performance Context

| Metric | Value |
|--------|-------|
| PnL | +$6,980 |
| Win Rate | 73.2% |
| Sharpe | 2.54 |
| Max DD | -3.4% |

## Implementation

Full logic in `src/hyperliquid_bot/strategy.py`

## Configuration

Edit `config.yaml` params section for strategy-specific tuning.
