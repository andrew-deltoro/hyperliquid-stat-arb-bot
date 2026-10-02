# Position Sizing Graduation

## Current Level: 1.25x (base_notional=2500)

## Step to 1.5x (base_notional=3000) when:
- ≥ 20 live trades closed
- Live Sharpe per trade > 0.3
- Max drawdown < 15% of capital
- No single trade lost > 3% of account

## Step to 2x (base_notional=4000) when:
- ≥ 50 live trades closed
- Live Sharpe per trade > 0.5
- Max drawdown < 20% of capital

## Step to 3x (base_notional=6000) when:
- ≥ 100 live trades closed
- Live Sharpe per trade > 0.5
- Max drawdown < 20% of capital

## Tripwires (drop to previous level):
- 3 consecutive losing trades > 8% of capital combined
- Live drawdown > 25% of capital
- Live Sharpe < 0 over 30+ trades
