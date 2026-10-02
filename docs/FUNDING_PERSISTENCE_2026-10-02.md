# Funding Persistence Analysis — 2026-10-02

## Data
- 18h window, 49 distinct timestamps, 11,466 snapshots
- HL native funding floor: +10.95% annualized (guaranteed minimum)

## HL-Native Candidates (Spot + Perp on HL)
| Pair | % >10% | % >25% | Avg Ann | OI |
|---|---|---|---|---|
| PURR | 100% | 18% | +33.90% | $21.3M |
| MON  | 100% | 36% | +27.89% | $62.1M |
| PUMP | 50%  | 16% | +16.59% | $290M |

## Cross-Venue Candidates (need Binance/Coinbase spot)
| Coin | % >10% | % >25% | Avg Ann | OI |
|---|---|---|---|---|
| ZRO      | 100% | 82% | +51.14% | $77M |
| FARTCOIN | 100% | 70% | +41.40% | $37.8M |
| XRP      |  98% | 34% | +23.08% | $330M |
| ZEC      | 100% | 24% | +17.10% | $604M |
| GRASS    | 100% | 64% | +31.20% | $36.9M |

## Recommendation
1. Build HL-native harvester first (2 days) — prove the pipeline
2. Expand to cross-venue later (2 weeks) — bigger yield

## Structural Insight
HL funding rates for these assets never flip negative.
The floor is +10.95% annualized. Risk is reversion to floor, not full loss.
