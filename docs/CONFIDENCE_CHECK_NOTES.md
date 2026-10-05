# Confidence Check — Reinterpretation Notes (2026-10-05)

## What we learned on first run
- All pairs showed "red" under the original classify() logic
- Root cause: EG cointegration p-value on 30-day window is too conservative
  for crypto data
- The bot's actual signal is *mean reversion of the linear combo*, not
  classical cointegration

## Revised classification
- Primary: half-life drift, beta drift (both reflect real behavior)
- Secondary: EG p-value — flag only if >0.5 (near-random-walk)
- Live PnL will be added as a fourth metric (needs ~20 live trades per pair)

## Known edge cases
- xyz:MU/TSM: HL/β stable, eg_p=0.93, but 8/8 live wins
  => "metric-too-conservative" case; don't auto-pause on red alone
- The final gate should be: "has the pair produced live losses?" not just
  "does the metric look bad?"

## Next step
- Wire the check into a weekly systemd timer
- Add per-pair alerting when status changes green->yellow
- After ~20 trades per pair, add live PnL vs walk-forward comparison
