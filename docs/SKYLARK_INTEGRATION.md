# Skylark Macro Integration Plan

## Priority 1: FLP Regime Filter
- Add `state/macro_regime.json` with FLP monthly/weekly values
- Confidence check reads it, computes macro overlay status
- Sizing rules:
  - FLP < 50  → 1.0x (Favorable)
  - FLP 50-60 → 0.75x (low Transitional)
  - FLP 60-65 → 0.50x (high Transitional)
  - FLP >= 65 → skip new entries (Defensive)

## Priority 2: GFC Blowout Warning
- Track BTC GFC forward spread daily
- If BTC spread > 3: flag all crypto pairs, reduce sizing
- If ETH spread > 10: same

## Data Source
- Contact: friend at Skylark Digital Assets
- Request: API endpoint or daily JSON push
- Fallback: manual weekly entry (low-fidelity)

## Current Reading (Oct 2026)
- FLP monthly: 58.13 (Transitional, trending favorable)
- FLP weekly: 56.41 (Transitional, stabilizing)
- GBC: 56.54 (Expansion)
- GFC: all four assets Expansion, no blowouts
- Interpretation: normal-to-cautious regime, full sizing OK

## To Do
- [ ] Save report archive
- [ ] Contact friend about API access
- [ ] Build macro_regime.json schema
- [ ] Wire into confidence check v2
- [ ] Backtest: does FLP add value to historical PnL?
