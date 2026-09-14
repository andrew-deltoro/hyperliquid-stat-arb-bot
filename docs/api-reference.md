# API Reference — Hyperliquid Statistical Arbitrage Bot

## CLI

```bash
python3 main.py run --coin ETH --once   # single tick
python3 main.py run --coin ETH          # continuous loop
python3 main.py info                    # strategy metadata
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| HL_PRIVATE_KEY | — | Trade-only wallet key |
| HL_NETWORK | testnet | Network selection |
| HL_MAX_POSITION_USD | 10000 | Position cap |
| HL_MAX_DAILY_LOSS_USD | 500 | Daily loss halt |
| HL_KILL_SWITCH | false | Emergency stop |

## Core Classes

- `StrategyImpl` — signal and order logic
- `BotRunner` — async execution loop
- `HyperliquidClient` — SDK wrapper
- `RiskManager` — safety rails
