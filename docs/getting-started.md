# Getting Started — Hyperliquid Statistical Arbitrage Bot

## Prerequisites

- Python 3.10+
- Hyperliquid account at [hyperliquid.xyz](https://hyperliquid.xyz)
- Trade-only API wallet for live trading

## Install

```bash
git clone https://github.com/YOUR_USERNAME/hyperliquid-stat-arb-bot.git
cd hyperliquid-stat-arb-bot
pip install -e .
cp .env.example .env
```

## First Run

```bash
python3 main.py run --coin ETH --once
```

## Go Live

1. Create trade-only API wallet on Hyperliquid
2. Set `HL_PRIVATE_KEY` in `.env`
3. Set `HL_NETWORK=mainnet`
4. Start with small `size` in `config.yaml`
5. Run: `python3 main.py run --coin ETH`

## Next Steps

- [Strategy Deep Dive](./strategy.md)
- [API Reference](./api-reference.md)
- [FAQ](./faq.md)
