# Hyperliquid Stat-Arb Bot — Code & Security Audit

| | |
|---|---|
| **Audit date** | 2026-10-07 (report written 19:09 UTC) |
| **Repository** | `hyperliquid-stat-arb-bot` (origin `github.com/andrew-deltoro/hyperliquid-stat-arb-bot`) |
| **Commit audited** | `5b08e67cebebc82bff2f6300e675c29e22d2f540` — "Directional perp bot live: BTC + SOL, 1x, $500 budget" (2026-10-07 08:28 UTC) |
| **Working tree** | Clean vs `HEAD`; `HEAD == origin/main`. Untracked: `scripts/leverage_study.py`, `scripts/directional_perp_v2..v4.py`, cache dirs |
| **Live runtime inspected** | `/home/hlbot/hlbot-runtime/live` (state, logs as of the 16:00 UTC cycle) |
| **Scope** | Review only. No files modified, no orders placed, nothing executed from the repo. `.env` was not read. |
| **Modeling reference** | McElreath, *Statistical Rethinking*, 2nd ed. (section numbers verified against the PDF table of contents) |

> **Point-in-time notice.** Line numbers, config values, and runtime observations below describe the commit and runtime state listed above. Re-verify them against the current code before acting on any finding.

---

## 0. Urgent items (as of 2026-10-07)

1. **Supply-chain dropper in the repo (security, Critical).** See §1. Rotate the Hyperliquid API wallet if the repo has ever been opened in VS Code or Cursor on a machine with Node installed.
2. **Daily order limit exhausted; closes will be silently dropped.** `hlbot-runtime/live/state/daily_orders.json` showed `count: 10` with `max_daily_orders: 10`. Until 00:00 UTC every order is rejected, closes included, yet `_close()` still marks the position closed in state (C1 + H1). The `KILL` file does not help, because it also blocks closes.
3. **Suspected orphan legs on the exchange.** The 16:00 UTC cycle reported live exposure on **COMP** and **SNX** with no open state pair containing either coin. Earlier logs show orphaned **DYDX**, and state contains `exit_reason: "manual_flatten_executor_bug"`.
4. **Sizing tripled today through a config change.** Commit `48f0615` ("3x leverage on pairs bot") lowered `baseline_balance` from 3105 to 1035, so scale ≈ 2.99. Because `scale_safeguards: true`, the safeguard caps tripled too (`$1047/order`, `$7479 exposure`).

---

## 1. Security: install-on-open dropper (Critical)

**Origin.** Initial template commit `3295ae8`, author `aopunya4332` (this account has no other commits). The template's README contains SEO spam and unverifiable performance claims ("+$6,980", "Sharpe 2.54"). Treat those claims as marketing.

| File | Behavior |
|---|---|
| `.vscode/settings.json` | `"task.allowAutomaticTasks": "on"`; sets `cmd.exe` (Windows) and `bash` automation profiles |
| `.vscode/tasks.json` | Task `"npm: install all dependencies"` with `runOptions.runOn: "folderOpen"`. Runs `node -e …`, which reads `REGISTRY_STR` from `.env.example`, base64-decodes it, sets it as the npm registry, and runs `npm install --include=dev --include=optional` |
| `.env.example` | `REGISTRY_STR=<base64>` decodes to `http://212.193.3.61:4873` (bare IP, Verdaccio's default port). Decoded locally; the host was never contacted |
| `.npmrc` | `registry=${REGISTRY}` |
| `package.json` | A single dependency: `ioredis-om-typed` |

**Impact.** If triggered, arbitrary code from an attacker-controlled registry runs with access to the folder, including `.env` (`HL_PRIVATE_KEY_MAINNET`). An agent wallet cannot withdraw, but it can trade, for example losing trades against an attacker's orders.

**Evidence checked on the VPS (none found):** `node` and `npm` are not on PATH; there is no `node_modules`, no `~/.npm`, and no `ioredis-om-typed` under `$HOME`; `~/.cursor-server` contains no reference to the task, IP, or package.

**Not verifiable from the VPS:** the developer's Windows/WSL machine. Commit `e5fd04a` converted `.env.example` to CRLF line endings, so the repo has been edited on Windows.

**Recommended actions**
1. Revoke the Hyperliquid API (agent) wallet. Create a new one on a machine that has never opened this repo. Review account fills for unexpected trades.
2. On every machine that opened the repo, check for `node_modules/ioredis-om-typed`, the npm logs (`%LOCALAPPDATA%\npm-cache\_logs` on Windows, `~/.npm/_logs` elsewhere), and connections to `212.193.3.61`.
3. Delete `.vscode/tasks.json`, `.vscode/settings.json`, `.npmrc`, `package.json`, and the `REGISTRY_STR` line, then commit. The public GitHub copy serves the dropper to anyone who clones it.
4. Turn off automatic tasks and require Workspace Trust in your user-level editor settings.

**Secret scan of git history:** no private-key-shaped strings (64-hex) in any commit, including the one unreachable commit left by an amend. The real `.env` was never committed. `HL_PRIVATE_KEY` was empty in both committed versions of `.env.example`.

**Test:** a CI check that fails if `.vscode/tasks.json` contains `runOn: folderOpen`, if `.npmrc` sets a non-default registry, or if any file holds a base64 string that decodes to a URL.

---

## 2. Findings: trading system

Format for each finding: **severity**, location, problem and how it fails in practice, fix, and a test that would catch it.

### Critical

#### C1. Close results are ignored; state is marked closed anyway
- **Where:** `src/hyperliquid_bot/phase3/positions.py:330-363`
- **Problem:** `_close()` places both close legs, never checks `fill.status`, then sets `status="closed"`, writes PnL, and saves. Any rejected close (daily limit, kill switch, exposure cap, per-order cap, margin, slippage, network error) leaves a live position the bot no longer tracks. Executor messages go to stdout (`systemd.log`), while the cycle log still says "✅ CLOSED". Production has hit `REJECTED: daily order limit reached (10/10)` (`systemd.log:9821-9830`).
- **Fix:** Mark closed only after both legs are confirmed flat via `user_state` on the correct dex. Otherwise keep the position open with a `close_pending` flag and retry.
- **Test:** a fake executor returns `status="rejected"` for close legs. Assert the position stays open and no journal row is written.

#### C2. Close size is recomputed from USD at the exit price, without reduce-only
- **Where:** `phase3/positions.py:333-346`
- **Problem:** The close size is `leg_usd / exit_price`, not the entry quantity. A long leg that rose 20% is only about 83% closed, leaving a residual. A leg that fell is oversold and flips to the opposite side. Orders are not reduce-only. Coins are shared across pairs (`xyz:TSM` is in two open pairs; SOL is in three pairs plus the directional bot), and Hyperliquid nets each coin into one position per account. The NVDA/PLTR close on 2026-10-07 matched only because the legs were about $60 and rounded to 2 decimals.
- **Fix:** Store the filled size and side per leg at entry. Close exactly that size with `reduce_only=True`, capped at the live net position. Confirm that your SDK version supports reduce-only on the order call you use.
- **Test:** open at 100, close at 120 and at 80. Assert close size equals entry size and the order is reduce-only.

#### C3. Maker-then-taker fallback can double-fill
- **Where:** `phase3/executor.py:263-267, 432-480`
- **Problem:** After the 30s Alo attempt, `_try_maker` cancels and returns `None`, then `place_order` sends a market order for the full size. Partial maker fills before the cancel are never checked. A failed cancel because the order already filled is only logged, and an exception while polling status `break`s into the same path. Any of these can produce up to 2x the intended exposure. `systemd.log` has dozens of `maker timeout … cancelled` lines; check `user_fills` for those order IDs to see whether any partially filled.
- **Fix:** After the cancel, query the order and fills, and send a taker order only for `requested − filled`. If the cancel fails, re-query before doing anything else. Use client order IDs (cloids) for idempotency.
- **Test:** mock the order status as `filled sz=0.4/1.0` at cancel and assert the taker size is 0.6. Separately, make the cancel raise after a fill and assert no taker order is sent.

#### C4. Partial, resting, or errored legs are left naked
- **Where:** `phase3/positions.py:152-185`; `phase3/executor.py:287-344`
- **Problem:** Leg A counts as failed whenever its status is not `"filled"`, which includes `partial` and `resting`. The function then returns without unwinding the partial fill. If leg B fails, the rollback sends a non-reduce-only order for the *requested* `size_a` rather than what filled, and ignores the result. Leg B's own partial fill is never unwound. An `"error"` result from a timeout may still have filled. The orphans and `manual_flatten_executor_bug` in state are consistent with this.
- **Fix:** Treat legs as quantities. After each order, read the actual position delta from the exchange. Unwind exactly that delta with reduce-only, verify flat, and alert if the hedge cannot be restored.
- **Test:** a state-machine test over leg A × leg B outcomes in {filled, partial, resting, error, exception}. In every end state, net exchange exposure must be either a hedged pair recorded in state or zero.

#### C5. Reconciliation fails open and can mark every position closed
- **Where:** `phase3/scheduler.py:94-126`
- **Problem:** Both `user_state` calls swallow exceptions. If both fail (429, outage), `live_coins` is empty and every open position is marked `state_reconciled`/closed. The bot then trades on top of positions it has forgotten. A position is closed only when *both* legs are missing, so a single missing or liquidated leg is never detected, and orphan legs are never surfaced.
- **Fix:** If either query fails, abort reconciliation and block new entries for the cycle. Reconcile per leg by size and side. On any mismatch, halt and alert instead of editing state.
- **Test:** make `user_state` raise. Assert state is unchanged and no entries are placed.

### High

#### H1. Safeguards block risk-reducing orders
- **Where:** `phase3/testnet_safeguards.py:85-116`; `phase3/executor.py:212-226`
- **Problem:** The kill switch, daily count, per-order cap, and projected-exposure checks all apply to closes and rollbacks, and a close *adds* to projected exposure. Combined with C1, this produces orphans.
- **Fix:** Classify each order as increasing or reducing exposure. Reduce-only orders bypass the count and exposure caps and are the only orders allowed under `KILL`.
- **Test:** with the counter at max and `KILL` present, a reduce-only close is still submitted.

#### H2. Exposure checks miss HIP-3 positions and netting across strategies
- **Where:** `phase3/executor.py:176-209`; `phase3/positions.py:36-40, 55-80`
- **Problem:** `_current_total_notional` and the pre-trade duplicate check query only the main dex. The scheduler's own comment (`scheduler.py:92-93`) says xyz positions don't appear there, so every `xyz:*` leg is missing from the exposure cap. The pre-trade check fails open on exceptions. `total_open_notional` uses the pair notional, not gross leg notional. The directional bot trades SOL on the same account with no coordination.
- **Fix:** Compute gross exposure across all dexes, fail closed on query errors, and keep a per-coin intended position across all pairs and strategies.
- **Test:** mock xyz-dex positions and assert they are counted. Opening a second pair on `xyz:TSM` must be blocked or accounted for.

#### H3. HIP-3 size precision falls back to a guess
- **Where:** `phase3/executor.py:229-238`
- **Problem:** `self.info.meta()` with no dex argument likely omits `xyz:*` assets, so `szDecimals` silently defaults to 2. `round()` can round up past notional caps. **Needs verification:** whether your SDK's `meta(dex="xyz")` returns the correct `szDecimals`, and how HIP-3 prices are rounded.
- **Fix:** Look up metadata per dex, round sizes down, and fail if the asset is not found.
- **Test:** an `xyz:` coin missing from the main metadata must raise an error, not use 2 decimals.

#### H4. No exchange-side leverage, margin mode, or liquidation awareness
- **Where:** repo-wide; no calls to `update_leverage` and no reads of `liquidationPx` or margin used.
- **Problem:** Effective leverage is whatever the account default is for each asset. The xyz dex has separate collateral, and the logs show repeated `Insufficient margin to place order` on xyz assets (`systemd.log:6985-8914`).
- **Fix:** At startup, set leverage and cross/isolated mode per asset explicitly and read them back to verify. Budget margin per dex. Block entries when liquidation distance is below k× the stop distance.
- **Test:** startup fails if the configured leverage doesn't match what the exchange reports.

#### H5. `p_revert`/edge carries no information and doesn't match the exits
- **Where:** `phase3/signals.py:221-231, 245-252`; `stats/mean_reversion.py:145-157`
- **Problem:** A "hit" means |S| < 0.75·|S₀| at any point within 3.33 half-lives, i.e. a 25% retracement. Almost any noisy path does that. The live exit is `cross`, which requires reaching −0.75× the entry spread on the opposite side. The stop and costs are ignored. Live logs show P = 0.93–0.997 for every pair, including z = +0.05, so `edge_min` never binds and sizing is effectively `base × ~0.49 × discounts × scale`.
- **Fix:** Simulate the real exit policy (cross target, 3× stop, time stop, fees, funding) from posterior draws and return the posterior predictive distribution of net PnL (§3.3).
- **Test:** for a random walk (κ ≈ 0), expected net PnL ≤ 0 and sizing returns zero.

#### H6. The cointegration model doesn't test cointegration, and its beta uncertainty is overconfident
- **Where:** `stats/cointegration.py:45-69`; `phase3/signals.py:200-214`; `phase3/positions.py:303-309`
- **Problem:** The model fits i.i.d. Student-t residuals to log A on log B, which is the spurious-association setup (§5.1). There is no stationarity test, and R-hat < 1.05 only shows the sampler converged (§9.5). Autocorrelated residuals make `beta_sd` far too small. Live: NVDA/PLTR beta went 0.39 → 0.47 in two days against an SD of 0.017 (about 4.6 SDs), which fires `beta_shift` on noise. Beta uncertainty is dropped: `spread_samples` is computed but unused, and OU is fit to the posterior-mean spread (§15.1).
- **Fix:** Fit the spread and OU dynamics jointly (error-correction / state-space; compare §16.4), propagate beta draws into the OU fit, and run posterior predictive checks on residual autocorrelation.
- **Test:** fit two independent simulated random walks. The pipeline must reject them (low posterior probability of κ > 0, or a failed stationarity check).

#### H7. The BTC volatility filter fails open
- **Where:** `phase3/scheduler.py:23-35`
- **Problem:** Any exception returns `0.0`, which passes the 55% filter. That is most likely during market stress, exactly when entries are most dangerous.
- **Fix:** Return `None` and block entries.
- **Test:** `fetch_candles` raises; assert no `OPEN` signals.

#### H8. Directional bot applies leverage twice, and its stops aren't on the exchange
- **Where:** `directional/signals.py:128`; `directional/positions.py:88`
- **Problem:** `suggested_notional = base × L`, then `try_open` multiplies by `L` again, so L=3 becomes 9x. It's latent only because `leverage: 1.0` today. Stops are checked once a day against the mid price, so price gaps blow through the 2% risk assumption.
- **Fix:** Apply leverage once and place reduce-only trigger stops on the exchange.
- **Test:** with L=3, notional = 3× base (capped).

#### H9. The leverage study can't justify a leverage choice, and the 3x change scaled the safeguards with it
- **Where:** `scripts/leverage_study.py:7-12, 25-28, 53-86` (untracked); `config/phase3-live.yaml:61`; `phase3/scheduler.py:64-78`
- **Problem:** The study's assumptions don't hold:
  - It says "2×ATR stops prevent liquidation", but the pairs bot has no ATR stops.
  - It assumes a uniform 2.5% maintenance margin. Verify per asset; low-max-leverage alts are likely much higher.
  - Trades are applied sequentially, though up to 10 run concurrently with correlated losses.
  - PnL is gross of fees and funding.
  - Its input, `walk_forward_ensemble.py`, uses MAP fits, no Kalman, a closed-form `p_revert`, target rather than cross exits, a 72h horizon, and overlapping trades every 48h. That is not the live strategy.

  Separately, scaling via `baseline_balance` with `scale_safeguards: true` raises the caps by the same factor as sizing.
- **Fix:** Set safeguards as fixed account-level constants, independent of the scaler, and review leverage changes on their own.
- **Test:** with scale = 3, the safeguard caps are unchanged.

### Medium

- **M1. Partial current candle used as if complete.**
  - **Where:** `data/ohlcv.py:65`; `directional/signals.py:63-100`
  - **Problem:** `end_ms=now` probably includes the in-progress bar; whether Hyperliquid's `candleSnapshot` returns it needs verification. The directional `vol_ratio` then compares a partial day's volume against full days (live values 0.32/0.51 at 15:28 UTC).
  - **Fix:** drop bars where `t + interval > now`.
  - **Test:** the last returned bar is closed.
- **M2. No staleness or gap checks on market data.**
  - **Where:** `data/ohlcv.py:110-126`
  - **Problem:** Taking the timestamp intersection silently drops missing hours, so bars are unevenly spaced in the OU fit. There is no last-bar freshness check and no 429 retry or backoff, and the mainnet data URL is hard-coded even for testnet.
  - **Fix:** reject series with gaps or a stale last bar, add retry with backoff, and derive the URL from the network.
  - **Test:** inject a 3h gap or a 2h-old last bar; assert the pair is skipped.
- **M3. Kalman look-ahead within the window, and an incomplete drift guard.**
  - **Where:** `phase3/signals.py:157-177`
  - **Problem:** The terminal (β, α) is applied retroactively to the whole window, which shrinks the in-sample spread SD and inflates z. Uncentered `log_b` with `P_init=1` lets α and β trade off, and only β is guarded. Live: GMT/MEME α 3.44 → 1.42 was accepted; LTC/DOGE β 1.88 → 0.59 was rejected.
  - **Fix:** use the one-step-ahead innovation for z, center `log_b`, and guard α too.
  - **Test:** synthetic data with known β; compare filtered-innovation z with retro-applied z.
- **M4. Recorded fills and PnL are not exchange-realized.**
  - **Where:** `phase3/executor.py:335-344, 451-458`; `phase3/journal.py:116-152`
  - **Problem:** The fill price is the candle close, not `avgPx`. A taker fee is assumed even on maker fills, and funding is ignored.
  - **Fix:** use `avgPx`/`totalSz` and reconcile against `user_fills` and funding history.
  - **Test:** a mock fill whose `avgPx` differs from the reference price stores `avgPx`.
- **M5. The correlation guard is mostly inert.**
  - **Where:** `phase3/correlation_guard.py:21-27, 47-53`
  - **Problem:** It requires both legs in the same hard-coded cluster. Only SOL/AVAX and AAVE/COMP of the 16 live pairs qualify; SNX isn't in `defi`, and xyz assets never match.
  - **Fix:** per-coin and per-factor caps on gross leg notional.
  - **Test:** a third SNX pair is blocked.
- **M6. No cycle lock, and the documented manual run uses a different state directory.**
  - **Where:** `phase3/scheduler.py:43`; `docs/vps_operations.md:62`; `phase3/paths.py:23-27`
  - **Problem:** Overlapping cycles can both open the same pair. The documented manual command doesn't set `HLBOT_RUNTIME_DIR`, so it uses `repo/state`, which has an empty positions file. `repo/state/daily_orders.json` was at 10 on 2026-10-07, which suggests this already happened.
  - **Fix:** take an `flock` on the runtime directory and refuse live mode without `HLBOT_RUNTIME_DIR`.
  - **Test:** a second concurrent `run_cycle` exits.
- **M7. Funding executor safety.**
  - **Where:** `funding/executor.py:35, 99-132, 161-188`
  - **Problem:** It defaults to `dry_run=False`. Exchange error dicts are treated as success, and only exceptions trigger an unwind. Sizes use 5 significant figures rather than `szDecimals`. The spot mid lookup by `"COIN/USDC"` may not match the keys `all_mids` uses (verify). `usd_class_transfer` moves margin out from under the other bots.
  - **Fix:** default to dry-run, parse each leg's status and unwind on error, use `szDecimals`, and coordinate transfers.
  - **Test:** the perp leg returns `{"error": …}`; assert the spot leg is unwound.
- **M8. Directional and pairs bots share one account without coordinating.**
  - **Where:** `directional/scheduler.py:43-64`; `directional/executor.py:64-73`
  - **Problem:** Directional equity counts only the main dex. The $300 reserve is static and assumes 2.5% maintenance. If `market_close` acts on the *net* position (verify for your SDK version), a shared SOL position could be closed in the wrong direction.
  - **Fix:** run each strategy on its own subaccount.
  - **Test:** net long SOL from pairs plus a directional short; the directional close reduces only the directional quantity.
- **M9. Negative beta is unhandled.**
  - **Where:** `phase3/positions.py:115-116, 146-149`
  - **Problem:** The `Normal(1,1)` prior allows β < 0, which gives a negative `leg_b_usd` and a side mismatch.
  - **Fix:** reject β ≤ 0, or flip the side and use |β|.
  - **Test:** β = −0.3 is rejected or the side is flipped.

### Low

- **L1.** Unreachable code after `return None` in `phase3/executor.py:483-599`, including a `rounded.size` NameError.
- **L2.** `config/phase3.yaml:66` misspells `max_notational_per_order`, so the setting is ignored.
- **L3.** `stats/cointegration.py:110` (`fit_null_model`) references an undefined `cores`.
- **L4.** `stats/cointegration.py:58-62` labels −2·lppd as WAIC, but there is no p_WAIC penalty (§7.4).
- **L5.** `phase3/journal.py:76-89` hard-codes `skip_reason` at |z| < 1.5 and omits the volatility and half-life rejects.
- **L6.** Dependencies are unpinned, and pymc, arviz, pyyaml, requests, and eth-account are undeclared. Unseeded `np.random` in `simulate_reversion_probability` makes signals nondeterministic.
- **L7.** `docs/vps_operations.md:11` publishes the server IP and SSH user in a repo with a public remote.
- **L8.** `half_life_bars` is treated as hours, which is only valid while the interval is `1h`.

### Tests

There is no `tests/` directory. Production history shows hot-patch regressions that a minimal test suite would have caught: `NameError: half_life_hours` across all pairs (`logs/cycle_20261001_223138.log`) and `KeyError: 'exit_horizon_mult'`. Priorities:
1. Order-lifecycle state-machine tests with a fake exchange (C1–C5, H1).
2. A smoke test that runs `generate_signal` on a fixed candle fixture with a small number of draws.
3. Property tests for sizing and rounding per dex.

---

## 3. Path to justified leverage

1. Fix C1–C5 and H1–H3 first. Leverage multiplies orphaned and flipped legs as much as it multiplies PnL.
2. Size from the posterior predictive distribution of net trade returns under the real exit rules, fees, slippage, and funding (§3.3). Choose \(f\) to maximize \(\mathbb{E}_{\text{post}}[\log(1+fR)]\) (a Bayesian Kelly), then use a fraction of that.
3. Pool across pairs (Ch. 13: partial pooling of κ and of trade returns) and model their joint behavior (Ch. 14), because spreads blow out together under stress. Set leverage at the portfolio level, per dex, on gross leg notional. The fixed κ prior (sd 0.004, logit sd ≈ 0.35) dominates; refresh it and check its influence with a prior predictive simulation.
4. Calibrate: compare predicted `p_revert` in `signals.csv` with realized outcomes from exchange fills.
5. Constrain by liquidation: use per-asset maintenance margin from exchange metadata, assume moves past the stop (stops are checked only once per cycle), and require equity to survive a joint k-sigma move across all open legs.

Nothing in this review shows the strategy is profitable or safe to run live, at any leverage.

---

## 4. Not verified

- **Installed SDK behavior:** `meta`/`user_state` dex arguments, `market_close` netting, `query_order_by_oid` response shape, reduce-only support, and HIP-3 price/size rounding.
- **Exchange behavior:** whether `candleSnapshot` includes the in-progress bar, and per-asset maintenance margin and max leverage.
- **Deployment:** systemd unit and timer contents and the cycle interval.
- **Exchange state:** actual positions, including the COMP/SNX orphans.
- **Other machines:** the developer's Windows/WSL machine (dropper exposure).

## 5. Key commits referenced

| Commit | Date | Message |
|---|---|---|
| `3295ae8` | 2026-09-01 | Initial commit (template, dropper files) — author `aopunya4332` |
| `e5fd04a` | 2026-09-14 | Phase 3: Bayesian coint + OU + … (`.env.example` rewritten with CRLF) |
| `fd33b05` | 2026-10-01 | Fix 3 execution bugs: pre-trade live check, partial-fill detection, reverse reconciliation |
| `d421f7c` | 2026-10-03 | Add cross exit mode (+45pct mean PnL vs trail, n=189) |
| `3c93cbf` | 2026-10-05 | Fix signals.py pair_key (NameError); reconcile queries xyz dex |
| `f9df684` | 2026-10-06 | Kalman-smoothed beta for fast-reverting crypto pairs |
| `48f0615` | 2026-10-07 | 3x leverage on pairs bot + directional perp backtest |
| `5b08e67` | 2026-10-07 | Directional perp bot live: BTC + SOL, 1x, $500 budget (audited HEAD) |
