# Position Sizing Graduation

## Current Level: 1.25x (base_notional=2500, max_notional=350)
Started: 2026-10-02

## Step to 1.5x (base_notional=3000) when:
- ≥ 20 live trades closed
- Live Sharpe per trade > 0.3
- Max drawdown < 15% of capital
- No single trade lost > 3% of account

## Step to 2x (base_notional=4000) when:
- ≥ 50 live trades closed
- Live Sharpe per trade > 0.5
- Max drawdown < 18% of capital

## Step to 3x (base_notional=6000) when:
- ≥ 100 live trades closed
- Live Sharpe per trade > 0.5
- Max drawdown < 18% of capital

## Immediate Tripwires (drop to 1x):
- 2 consecutive losing trades totaling > 6% of capital
- Live drawdown > 18% of capital
- Any single-day loss > 8% of capital
- Live Sharpe < 0 over 20+ trades

## Emergency Tripwires (pause all trading):
- Live drawdown > 25% of capital
- System bug causes unexpected position
- Exchange outage or ADL event
