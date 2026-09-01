# Security — Hyperliquid Statistical Arbitrage Bot

## API Wallet

Use Hyperliquid **agent wallets** with trade-only permissions. Never use master wallet private keys.

## Key Management

- Store keys in `.env` (never commit)
- Rotate keys periodically
- Use separate keys per bot instance

## Risk Controls

Configure before live trading:
- HL_MAX_POSITION_USD
- HL_MAX_DAILY_LOSS_USD
- HL_KILL_SWITCH

## Testnet First

Always validate on testnet before mainnet deployment.
