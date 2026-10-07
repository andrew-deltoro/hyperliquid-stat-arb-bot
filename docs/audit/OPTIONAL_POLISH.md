# Optional polish (not blocking)

These are nice-to-haves left after the 2026-10-07 remediation. None change
the live risk path until you choose to act on them.

## 1. Funding-rate persistence / carry model

Live closes now attribute `user_funding_history` into `funding_usd` and PnL.
What is still optional:

- Persist a local funding-rate panel (the `scripts/funding_collector.py` /
  `reports/*_funding.pkl` path) so the **entry** simulator's carry term uses
  the same series you analyze offline, instead of a constant or last-print.
- Join that panel into walk-forward / leverage studies so estimated E[r]
  matches live funding attribution more closely.

Skip until you care about tightening the prior on short-horizon carry; the
journal already records realized funding on each close.

## 2. xyz HIP-3 weekend / gap policy

`M2` rejects aligned series with gaps > 3 bars or > 2% missing. xyz perps can
gap over weekends or thin sessions. Options:

- Confirm whether your xyz universe actually gaps enough to starve signals
  (log skip reasons for a week).
- If yes: allow a dex-specific gap budget, or drop xyz pairs from the live
  book on Friday close and re-admit Monday — rather than silently failing
  the data check mid-week.

Until confirmed, leaving the strict check is safer than inventing fills.

## 3. Trigger-order / fill shape confirmation

Still listed under "Not verified" in `REMEDIATION_2026-10-07.md`: stop-trigger
response shape, `frontendOpenOrders` cancel fields, maker `oid` on
`user_fills`, xyz `onlyIsolated` / `maxLeverage`. Worth a single testnet
session when you next touch directional stops or xyz leverage — not urgent
while pairs live on main-dex cross.

## 4. Calibration burn-in

Historical `trades.csv` rows have `p_revert_at_entry` but no
`exp_return_at_entry`. Do **not** backfill E[r] from p_revert (wrong units;
would crush `c`). Schema + `funding_usd` were migrated via
`scripts/backfill_calibration.py`; older coin funding is often absent from
HL's retained `userFunding` window (zeros are honest). New closes write E[r]
and live funding. Until then `c` stays on the prior (0.5). Optional: after
~30 trades with E[r], review whether to raise `max_trade_notional_frac` —
only if `c` and live PnL support it.

## 5. API wallet rotation

Ignored for this pass per operator request. Still recommended if the old
template dropper was ever opened with Node on a machine that held keys.
