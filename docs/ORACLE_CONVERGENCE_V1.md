# Oracle Basis Reversion

Human-facing experiment name: **Oracle Basis Reversion**. The internal
hypothesis IDs retain their original names so the locked tests remain
traceable and are not silently rewritten.

This is a public-data-only paper experiment. It cannot place orders.

Every UTC hour it filters Hyperliquid perps to at least $5 million of trailing
daily volume, ranks them by impact-mid price minus oracle, paper-buys the
cheapest decile, and paper-shorts the richest decile. Long and short notionals
are balanced. Each position is $500 and closes after four hours by walking the
then-visible order book.

Net cohort P&L includes both taker fees and both book walks. Positive
`convergence_bps` means the selected premium moved toward the oracle in the
hypothesized direction. Profitability is assessed at the cohort level because
coins selected in the same hour share market conditions.

Hourly cohorts are retained for descriptive diagnostics, but adjacent cohorts
overlap because positions last four hours. The formal posterior therefore uses
only the non-overlapping 00/04/08/12/16/20 UTC cohorts. The first 30 complete
confirmatory cohorts are locked. The hypothesis is supported only if the
posterior probability that mean net cohort P&L exceeds cash is at least 95%.

The control also stores executable early-exit marks every 15 minutes. These
marks do not change v1's four-hour exit. Any future horizon selected from them
must be tested on later holdout data.

## Cost-gated v2 and robust sensitivity

V2 runs concurrently in a separate database. Its actual entry book-walk fill
must be at least 12 bps favorable to the oracle: 9 bps for entry/exit taker
fees plus a fixed 3 bp spread/model-error buffer. If both tails cannot supply
equal long and short counts above that gate, the cohort is skipped. V2 does
not overwrite or filter v1.

The supplementary robust diagnostic uses a Student-t likelihood with partial
pooling by coin and cohort and a long/short side effect. It repeats the model
after removing the largest winner. Because hourly cohorts overlap, this is a
sensitivity analysis only and cannot issue the locked verdict.

```bash
systemctl --user status hlbot-oracle-convergence-v1.service
systemctl --user status hlbot-oracle-convergence-v2.service
python3 scripts/oracle_convergence_score.py
.venv/bin/python scripts/oracle_convergence_visual.py
.venv/bin/python scripts/oracle_convergence_robust.py
.venv/bin/python scripts/oracle_convergence_diagnostics_visual.py
python3 scripts/oracle_convergence_score.py \
  --config config/oracle_convergence_cost_gated_v2.yaml
```

To stop only this experiment:

```bash
systemctl --user disable --now hlbot-oracle-convergence-v1.service
```
