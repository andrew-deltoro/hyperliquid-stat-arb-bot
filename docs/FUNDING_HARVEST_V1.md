# Hyperliquid spot/perp funding test v1

## Locked claim

When the preceding 24 hourly funding prints are all positive, buy the verified
Hyperliquid spot asset and short the same quantity of its perp. Close after
seven days or after the first negative hourly funding print, whichever comes
first.

This is a paper experiment. It uses public `/info` requests only and contains
no wallet, signing, transfer, or order path.

## What is measured

- BTC/UBTC, ETH/UETH, HYPE, and PURR use locked spot pair and token IDs.
- Entries are considered once daily at 00:05 UTC.
- Both entry legs and both exit legs walk the visible L2 book.
- Tier-0 taker fees are charged to all four simulated fills.
- Funding uses Hyperliquid's documented formula:
  `quantity * oracle price * hourly funding rate`.
- Oracle snapshots are collected every five minutes; a trade is invalid if an
  oracle within ten minutes of a funding print is unavailable.
- Spot and perp mark-to-market PnL, funding, and all four fees determine net
  PnL. Cash at zero PnL is the benchmark.

The scorer clusters coins by entry date. The locked confirmatory sample is the
first 30 complete entry dates. Its skeptical prior is centered on zero net
edge, and the claim is supported only if posterior `P(mean net PnL > 0)` is at
least 95%. Before then, all output is descriptive and the verdict is
`not_ready`.

## Operations

```bash
systemctl --user status hlbot-funding-harvest-v1.service
journalctl --user -u hlbot-funding-harvest-v1.service
python3 scripts/funding_harvest_score.py
.venv/bin/python scripts/funding_harvest_visual.py
```

To stop future collection without touching any other bot:

```bash
systemctl --user disable --now hlbot-funding-harvest-v1.service
```

The locked rules are in `config/funding_harvest_hypothesis.yaml`; changing one
after observing results creates a new hypothesis rather than modifying v1.
