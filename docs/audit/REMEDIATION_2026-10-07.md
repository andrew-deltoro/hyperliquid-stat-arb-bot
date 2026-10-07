# Audit remediation — 2026-10-07

Branch `cursor/fix-critical-order-lifecycle`, based on `5b08e67`. Findings refer to
`docs/audit/AUDIT_REPORT_2026-10-07.md` on branch `cursor/audit-report-2026-10-07`. Tests run with fakes only; no orders were
placed while making these changes.

## Status by finding

| Finding | Fix | Tests |
|---|---|---|
| Dropper (§1) | Removed `.vscode/tasks.json`, `.vscode/settings.json`, `.npmrc`, `package.json`, `REGISTRY_STR` | `test_repo_hygiene.py` |
| C1–C5, H1, H3, L1 | Verified reduce-only closes from stored quantities, `close_pending` retries, exact unwinds, `needs_review`, fail-closed reconcile, reduce-only bypasses safeguards, per-dex size precision | `test_order_lifecycle.py` |
| H2 | Exposure sums both dexes and fails closed; `total_open_notional` is gross legs; pre-trade check per coin; directional bot skips coins held by another strategy | `test_risk_controls.py` |
| H4 | `ensure_leverage()` sets exchange leverage before every entry (cross, or isolated when the asset is isolated-only), capped at the asset's `maxLeverage`; failure blocks the entry. Maintenance margin per asset feeds the stress test | `test_risk_controls.py` |
| H5 | `p_revert`/edge replaced by a posterior-predictive simulation of the real exit policy with costs and carry (`leverage.simulate_trade_returns`) | `test_leverage.py`, `test_signal_smoke.py` |
| H6 | Engle-Granger residual test gates entries (`signal.stationarity_level`); `beta_sd` corrected for residual autocorrelation; `beta_shift` uses the current posterior sd as a floor | `test_stats_and_data.py`, `test_risk_controls.py` |
| H7 | BTC vol returns `None` on failure and blocks entries | `test_stats_and_data.py`, `test_signal_smoke.py` |
| H8 | Directional leverage applied once; reduce-only stop trigger rests on the exchange and is cancelled on close | `test_directional_funding.py` |
| H9 | Safeguards are fixed config values; `scale_safeguards` is ignored | `test_risk_controls.py` (sizing never exceeds the per-order cap) |
| M1 | In-progress bars dropped (hourly and directional daily) | `test_stats_and_data.py` |
| M2 | Aligned series rejected on stale last bar, gaps > 3 bars, or > 2% missing; retry with backoff on 429/5xx; data URL follows `executor.network` | `test_stats_and_data.py` |
| M3 | Kalman runs on centered `log_b`; alpha guarded by level shift in residual sds; z can't use a scale smaller than the static fit's | `test_signal_smoke.py` |
| M4 | Fill prices are `avgPx`; PnL uses signed filled quantities; journal records predicted E[r] for calibration | `test_risk_controls.py` |
| M5 | Cluster caps on gross leg notional per leg, with xyz semis/metals and the missing crypto coins; per-coin cap | `test_risk_controls.py` |
| M6 | `flock` on the runtime dir; live mode requires `HLBOT_RUNTIME_DIR`; docs updated | `test_stats_and_data.py` |
| M7 | Funding executor defaults to dry run, parses statuses, hedges the filled quantity and unwinds on error, uses `szDecimals`, transfers need `allow_transfers` | `test_directional_funding.py` |
| M8 | Directional closes are reduce-only for its own quantity (no `market_close` on the net position); budget reserves live margin used | `test_directional_funding.py` |
| M9 | β ≤ 0 skipped at signal time and rejected in `try_open` | `test_risk_controls.py` |
| L2 | `max_notational_per_order` typo fixed in four configs | — |
| L3, L4 | `fit_null_model` takes `cores`; "WAIC" renamed `deviance` | — |
| L5 | `skip_reason` lists every failed check | `test_signal_smoke.py` |
| L6 | `requirements.txt` pinned to the VPS versions; simulations seeded | `test_leverage.py` |
| L7 | Server address removed from `docs/vps_operations.md` | — |
| L8 | Half-life converted with the configured bar interval | — |

## Leverage method

Per trade, with pair notional \(N\) (legs \(N/2\) and \(\beta N/2\)):

1. **Return distribution.** For each OU posterior draw, simulate the spread hourly, check exits
   only every `cycle_hours` (so losses can run past the stop between cycles), and apply the
   live rules: cross exit at \(-0.75 S_0\), divergence stop at \(3 S_0\), time stop at
   \(3.33\times\) half-life. Return per unit notional
   \(r = \tfrac12\,\mathrm{dir}\,(S_{exit}-S_0) - \text{fees/slippage} - \text{carry}\).
2. **Calibration.** \(c\) is the posterior mean of the slope in
   \(r^{realized}_i \sim N(c\, \hat r_i, s)\) with prior \(c \sim N(0.5, 0.25)\), clipped to
   \([0,1]\). With no recorded trades \(c = 0.5\): the model's edge is halved until live
   results support it. Draws are shifted to mean \(c\,\mathbb{E}[r]\).
3. **Bayesian Kelly.** \(f^* = \arg\max_f \mathbb{E}_{post}[\log(1+f r)]\).
4. **Portfolio split.** For \(K\) equicorrelated bets with correlation \(\rho\), each gets
   \(f^*/(1+(K-1)\rho)\); then `kelly_fraction` (¼).
5. **Caps**, whichever binds (logged per entry):
   - `max_trade_notional_frac` × equity;
   - `max_gross_leverage` × equity − current gross;
   - joint stress per dex: equity − Σ stress losses − 1.5 × Σ maintenance ≥ 0 after adding the
     new trade at its 1% posterior-predictive loss;
   - the fixed per-order safeguard.
6. **Exchange leverage** is set to `exchange_leverage` (3×, cross) per asset before entry. This
   only sets initial margin; the risk limit is the gross-leverage cap (1.5×) and the stress test.

In the first shadow cycles the per-trade cap (15% of equity) bound on every entry: the
model's Kelly sizes were 2–5× larger even after calibration and the correlation split. That
is the expected outcome while the OU model is uncalibrated (H6 is only partly addressed);
raise caps only after the calibration factor is estimated from recorded trades.

## Not verified (needs testnet or exchange confirmation)

- `update_leverage` on xyz assets, and whether xyz assets report `onlyIsolated`.
- `maxLeverage` in `meta(dex)` and maintenance margin = ½ × initial margin at max leverage.
- Trigger-order response shape (`resting` with `oid` vs `waitingForTrigger`) and
  `frontendOpenOrders` field names for stop cancellation.
- `user_fills` entries carry the maker order's `oid`; reduce-only rejection vs resizing.
- Spot mids keyed as `@<index>` in `all_mids`.
- Whether xyz candles have weekend gaps that the new data checks would reject.

## Still open

- H6 in full: a joint error-correction/state-space model with beta uncertainty propagated into
  the OU fit. The current fix corrects `beta_sd` and gates on stationarity, but the OU
  posterior is still conditional on the posterior-mean spread.
- §3 step 3: partial pooling of trade returns across pairs, and a fitted (not assumed)
  cross-pair correlation.
- M8 in full: separate subaccounts per strategy.
- Funding history in realized PnL.
