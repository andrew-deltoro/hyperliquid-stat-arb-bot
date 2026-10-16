# Day-trading edge matrix

A map from a claimed edge to the test that is allowed to kill it.
One row is one bet. The matrix is not a menu to run in bulk on the
same tape. Running several rows and keeping the winner is the
overfit this file exists to stop.

Nothing here is a live order, a sizing change, or a claim that a
row makes money.

## How a row is allowed to become a test

1. Name who is on the other side, in the "Who loses" line. A pattern
   with no counterparty stays a description.
2. The clock of the signal has to be the clock of the data. A weekly
   macro print cannot be an intraday entry.
3. At most three numbers are locked in the row. They are chosen here,
   before the scatter. Changing one after the result is a new row.
4. The first picture is a scatter or a decile bar of the precommitted
   statistic, with the row's cost already subtracted. No equity curve
   before that picture is flat or sloped.
5. If that picture fails the kill rule, stop. The last part of the
   sample stays sealed. Adding a filter, a second horizon, or a
   different coin is a new row and spends a new test.
6. If the picture lines up, walk-forward once: the earliest 70% of
   calendar time is the only place the scatter is inspected; the last
   30% is scored once, with costs, and then the row is closed.
7. A pass means the walk-forward number has the same sign as the
   scatter and clears its cost. It does not mean the bot should trade it.
   Sizing stays off until a later, separate decision.

Costs, unless a row says otherwise:

| Fill | What is charged |
|---|---|
| Taker | 4.5 bp fee each side, plus the quoted half-spread when the tape has a book, otherwise 3 bp slippage each side |
| Maker | 1.5 bp fee each side, and a fill only if price trades through the quote. A mid-price fill is not allowed |

Hyperliquid's published fee is the taker number already used by the
pairs bot. The half-spread is whatever the row's own quotes show.

## What is actually on disk

| Feed | What it can support | What it cannot |
|---|---|---|
| Hyperliquid `candleSnapshot` | Intraday statistical rows, once a horizon is pulled. Hourly history is already how the pairs bot fits. It keeps the most recent ~5,000 bars **per interval** and returns `[]` for anything older — no back-pagination. Measured 2026-10-14: 1m 3.6d · 5m 17.4d · 15m 52.2d · 30m 104.2d · 1h 208.4d · 2h 416.7d · 4h 833.2d · 1d 6.2y. | A finished intraday study. The bars are not sitting in a day-trade panel yet. A daily-session row at 5 minutes needs 120 days, which 17.4 days cannot supply. Either archive Hyperliquid 5m forward, or pull and freeze another venue's 5-minute history before the row is designed. |
| `data-api.binance.vision` | Frozen 5-minute panels for Q5 and later intraday rows. Reachable from this host where `api.binance.com` returns HTTP 451. 5m BTCUSDT paginates cleanly back past 2018 (3 × 1,000-bar pages verified contiguous, no gaps), about 210 requests per 2 years. The monthly bulk zips at `…/data/spot/monthly/klines/<SYM>/5m/<SYM>-5m-YYYY-MM.zip` are far faster: ~0.4–0.5 MB per symbol-month, HTTP 200 from each pair's listing month (404 before it), and a daily-zip route covers the current partial month. Fetcher: `scripts/feeds/binance_vision_bulk.py`; cache `reports/.binance_cache/`. | Premium and RWA perps. A second coin is a new row where a row says so — except Q9, whose by-liquidity buckets are defined over a frozen multi-pair universe. |
| Funding cache, 10 Jun–6 Oct 2026 | The funding rows below. Daily and hourly funding for the perp universe. | A claim about the next price. That daily decile already failed. |
| `hl_microstructure.jsonl` | BTC and ETH, top-5 book, from 2026-10-07 01:31 UTC through 2026-10-08 06:14 UTC. 3,443 snapshots each, every 30 seconds. Fields: `ofi_5`, `taker_ratio_5m`, `spread_bps`, `vwap_dev_bps`, `kalshi_mid`. The recorder is still appending. | A walk-forward. One day is one regime. No L2 row opens its holdout until 20 full UTC days are in the file. |
| Yahoo daily bars | Nothing in this matrix. | Day trades in ETFs or FX. |
| Webull OpenAPI | **Free tier is deeper than assumed.** Verified 2026-10-15 against the live API: equity/ETF bars come with **volume**, `delay_minutes: 0` (real-time), `start_time`/`end_time` **pagination**, and RTH/PRE/ATH session tags. SPY 5m reaches **~2010**, SPY 1m **~mid-2021**, SPY daily 1,200 bars (2022→now). Two 1,200-bar pages de-duped to 1,824 contiguous bars with correct session-aware gaps. Paid tiers add futures (`$11.99` L1 / `$38.99` L2 per month) and footprint (price unpublished), plus OPRA options (**free** if you trade options once a calendar month). Connector: `scripts/feeds/webull_connector.py`; setup `docs/WEBULL_DATA.md`, MCP `docs/WEBULL_MCP_SETUP.md`. | Futures bars, futures depth, futures tick and futures snapshot returned `MARKET_DATA_NOT_SUBSCRIBED` — still locked (a CME line, separate from Order Flow). Crypto is capped at 1,200 bars with **no volume** and no date-range params, so equity is the better free feed. |
| Webull footprint (paid, live 2026-10-15) | Per-price `buy_detail`/`sell_detail` ladders, `delay_minutes: 0`, for US futures (MNQZ6, ESZ6) and equities/ETFs (SPY, QQQ, AAPL, NVDA). This is the confirmation layer Q7 was missing. **No date-range parameter**; `count` clamps to 2000; supported timespans S5/S15/M1/M5/M30. Reach is therefore `timespan × 2000` bars: MNQ 5m ≈ 9.2 days, MNQ 1m ≈ 1.4 days, MNQ 30m ≈ 62 days, SPY/QQQ 30m ≈ 222 days. | **No bar OHLC in the record** — `time`, `total`, `delta`, `buy_total`, `sell_total`, `buy_detail`, `sell_detail`, `trading_session` only. So footprint does **not** route around the locked `get_futures_bars`; a separate price feed is still required. Equities have far deeper footprint reach than MNQ 5m. Fills/order counts absent — size per level only. |
| TradingView | Nothing. No public price endpoint. | Those rows. |
| Pairs journal | The bot's own predicted-versus-realized record, once `exp_return_at_entry` is filled in. | A new intraday signal. |
| Skylark FLP | A size dial decided before the session, from the latest published weekly or monthly print. | An intraday entry. The print does not update during the day. |

## Status

| ID | Source | Claim, one line | Status |
|---|---|---|---|
| Q1 | Statistical | A pair residual reverts inside one session and is flat by the end of it | Closed |
| Q2 | Statistical | The first 30 minutes of range predicts the rest of the UTC day | Ready to design a pull, not run |
| Q3 | Statistical | Yesterday's close-to-open gap fills today | Ready to design a pull, not run |
| Q4 | Statistical | Funding paid since UTC midnight predicts the rest of today's price | Closed |
| Q5 | Statistical | Fading the first 4H New York range after a failed breakout pays after costs | Closed |
| Q6 | Statistical | The MACD/200-EMA cross trend filter wins near the advertised 86% | Closed |
| Q7 | Orderflow | Absorption in a Fib discount zone reproduces the champion's 60-65% win rate | Fire-checked, fires zero times on MNQ 5m; sample-limited, not decidable |
| Q8 | YouTube | The SLC stochastic confirmation filter selects winners from losers at zone touches | Closed — tested; gap −8.1 bp, t −0.88 on first 70%; no selectivity; holdout sealed |
| Q9 | YouTube | Entering the first pullback (crossing candle) beats chasing the extension on the same momentum events | Closed — tested; pullback is worse on both hit rate and realized R; net-R gap −0.64, t −58.8 on first 70%; worse in every liquidity bucket; holdout sealed |
| Q10 | YouTube | The Trading Channel's trend-continuation setup: the HTF trend filter adds value (Claim A) and the broken-level retest + double-top/bottom rejection adds selectivity (Claim B) | Closed — tested; both claims fail; trend filter is adverse (−0.245R, t −1.30; geometry-free bp −8.1), rejection shows no reliable selectivity (+0.337R on realized R but t +1.22, collapses to +0.09R under a min-R filter, bp −2.1); holdouts sealed |
| Q11 | YouTube | Oliver Velez's "whale": after a power bar down the bounce fails, so shorting the failed bounce beats shorting the power bar (Claim A); and the open-relative-to-the-MAs zone rule matters, Zone 1 shorts being worse (Claim B) | Closed — tested; both claims fail; the bounce entry is worse than the naive control on hit rate, realized R and the geometry-free bp (−0.085R, t −59.2); the zone rule's first-70% sign (+0.020R, t +4.7) reverses in its holdout (−0.003R); Claim A holdout sealed, Claim B holdout opened once and reversed |
| L1 | Level 2 | Top-5 imbalance predicts the next 5 minutes of BTC mid | Recording. No test before 20 UTC days |
| L2 | Level 2 | Taker-buy share over 5 minutes predicts the next 5 minutes | Same tape as L1. Not a second look at L1's result |
| L3 | Level 2 | Wide book plus thin depth means do not cross | Execution rule, not a price bet |
| B1 | Behavioral | Round-number prices reverse more than other prices | Blocked on a trade tape longer than the book snapshots |
| B2 | Behavioral | The UTC-day boundary has a repeatable drift | Ready to design a pull, not run |
| P1 | Proprietary | Skylark FLP chooses the side of a day trade | Closed |
| P2 | Proprietary | The pairs journal's predicted return matches realized return | Not a day trade. Keep recording |
| P3 | Proprietary | Kalshi's 15-minute contract leads the Hyperliquid mid | Same 20-day gate as L1 |

"Ready to design a pull" means the candles exist at the API and the
row is fully specified below. It does not mean the test has been started.

## Quantitative statistical anomalies

### Q1. Session residual reversion

Who loses: someone who is using one leg as a directional bet and leans
on the other as a hedge that is no longer the hedge.

Data: hourly Hyperliquid closes. The pairs bot already trades the
multi-day version of this.

Locked: z entry 1.2 rich / 0.9 cheap, cross at 75% through zero, stop
at 3× the entry gap. No new numbers.

Costs: taker row above. The live book already pays them.

Scatter: already done, in production. Sixteen closes, about +$0.60.
The model's expected return stayed positive even after the hedge-break
exit was added, because the simulator still mean-reverts the spread
while the hedge jumps. That is not a day-trade pass.

Walk-forward: not opened. Shortening the hold to "flat by session end"
would be a new strategy fitted to a book that has not made money.

Kill: already met. Do not rerun this as an intraday z-score.

### Q2. Opening range

Who loses: a trader who fades the first push of the UTC day, or a
hedger who must finish a clip before the range is set.

Data: 5-minute BTC candles from `candleSnapshot`. ETH is not in this
row. One coin, so a BTC result cannot be "confirmed" by running ETH
afterward. ETH would be a new row, written before looking.

Locked: opening window 00:00–00:30 UTC. Signal is the sign of that
30-minute return. Hold is the following 90 minutes, then flat. No stop
in the first pass. A stop added after seeing the path is a new row.

Costs: taker, charged on the entry and on the flatten. One round trip
per UTC day.

Scatter: on the first 70% of days only, mean return of the signed
90-minute move, minus the round trip. Deciles are unnecessary. The
statistic is that mean and its t-statistic across days.

Walk-forward: last 30% of days, once, same statistic.

Kill: t-statistic below 2 on the first 70%, or the last 30% has the
opposite sign. A single spectacular day does not count. Require at
least 120 UTC days in the pull before the split, so the holdout has
at least 36 days.

### Q3. Gap fill

Who loses: a stop or a margin call that sells into the overnight jump
and does not get the other side of the fill.

Data: BTC daily and 15-minute candles. The gap is the first 15-minute
close versus the prior UTC day's last close.

Locked: trade only if the absolute gap exceeds 50 bp. Fade it. Exit
when price tags the prior close, or at 06:00 UTC, whichever is first.
No other threshold.

Costs: taker, round trip. If the gap is smaller than the round trip,
the row cannot pay even when the fill happens. That comparison is part
of the scatter, not a later adjustment.

Scatter: fraction of qualifying days that tag the prior close before
06:00, and the mean net return of the fade.

Walk-forward: same 70/30 calendar split as Q2, same 120-day minimum.
Days that do not qualify are cash, not dropped from the clock.

Kill: the fill rate is below 50%, or the mean net return on the first
70% is at or below zero.

### Q4. Funding accrued today

Who loses: a crowded long that has been paying funding since midnight,
if the price has not yet reflected it. The daily decile test already
asked the next-day version of this and the price did not line up.

Data: the funding cache and hourly candles, 10 Jun–6 Oct 2026.

Locked: none. This row is not granted a new horizon.

Costs: not reached.

Scatter: not reopened. Sorting by today's funding and looking at
tomorrow's price was the precommitted test. It failed. "The rest of
today" is a second horizon chosen after that failure.

Walk-forward: not opened.

Kill: already met. Funding may still be studied as a cashflow that
persists. It is not a day-trade price signal unless a future row is
written for a reason other than the failed decile.

### Q5. First 4H New York range, failed breakout fade

Source: a public YouTube video, "The BEST 5 Minute Scalping Strategy
Ever," Data Trader. The row is written because the setup has a nameable
counterparty and a fixed clock, not because the video showed profitable
examples. The video's own presentation is hand-picked, carries no cost
model, and sells an indicator. Two independent public tests of the
same rules already failed: 739 BTC trades at 28.0% and −1.19R per
trade net of 15 bp/side, and 3,505 gold 5m trades at 33.2% and −0.17R
after costs (both from Tracked Signals and Backtest Discipline). Those
tests had to freeze choices the source leaves loose.

This row freezes them too, before any pull is run. The expectation is
that it fails rule 4. It gets one look because the ingredients (a
pre-session anchor that many participants watch, plus a failed sweep)
are real, and the kill test is cheap.

Who loses: the late breakout trader who buys the close above the range
and then sells when it fails — often a stop, or an account forced out
by the move it just chased. If the breakout instead continues, the
crossover counterparty is this bot, and the row is the wrong way round.
The scatter may show either sign. The sign is not chosen after looking.

Data: 5-minute bars, one venue, chosen and frozen in the row's own
config before the pull. Hyperliquid cannot supply the sample: its
`candleSnapshot` keeps only the most recent ~5,000 bars per interval, so
5-minute bars reach back about 17.4 days and older windows return empty
(measured 2026-10-14). A daily-session setup yields roughly one to three
setups a day, and rule 3 wants at least 120 days, which 17.4 days cannot
give. The choices are to archive Hyperliquid 5m forward (a live recorder
that will not have 120 days for months) or to pull and freeze a
multi-year 5-minute panel from a named venue first. The pull does the
latter; `data-api.binance.vision` is the tested source, and the panel is
frozen to disk before the first scatter. Hyperliquid stays the execution
venue, so the row also carries basis risk between the signal venue and
the venue the bot would actually trade. The row is one coin and one
venue. A second coin is a new row, written before looking.

Freeze-update 2026-10-15: the "deep intraday history is unavailable" half
of this constraint is now **retired for US equities/ETFs**. Webull's free
OpenAPI tier serves SPY 5-minute bars back to **~2010** with volume,
`delay_minutes: 0`, and working `start_time`/`end_time` pagination
(verified contiguous, RTH-session aware; see `docs/WEBULL_MCP_SETUP.md`).
1-minute reaches ~mid-2021. So a daily-session equity row can now reach
the 120-day bar for free, on a real venue, without archiving forward.

It is **not** retired for Hyperliquid perps: `candleSnapshot` still holds
only the most recent ~5,000 bars per interval (5m ≈ 17.4 days), and the
crypto endpoint at Webull is worse — 1,200 bars, no volume, no date
range. Any future intraday row should therefore prefer an equity/ETF
instrument if it wants deep history, or keep using the frozen
Binance panel above.

Locked (two numbers): the reference range is the high and low of the
first 4H candle of the New York day, `00:00–04:00 America/New_York`,
used only after it has fully closed. A **breakout** requires a 5-minute
candle to **close** strictly above the range high or strictly below the
range low; a wick alone does not count. A **re-entry** requires a later
5-minute candle to close strictly back inside the same range, on the
same New York date. Those two conditions plus the 2R target are the only
numbers in the row; the rest is mechanical. Entry is the **next** 5-minute open after the re-entry close, not
the re-entry close itself. Stop is the breakout move's **extreme**: the
highest high (for a short) or lowest low (for a long) from the first
close outside through the re-entry candle. Target is exactly **2R** from
the actual entry and stop. Long fade follows a failed low-side break,
short fade follows a failed high-side break. One position at a time. A
fresh setup needs a new close outside the range. Nothing carries into
the next New York date; an unresolved position is flat at the last
5-minute close of the same day. The source's "nearest key level" stop
for very large breakouts is a judgement call and is not used; if it is
later wanted, it is a separate variant written before it is run.

Costs: taker row above — 4.5 bp per side plus the quoted half-spread
where a book exists, otherwise 3 bp slippage per side — charged on the
entry and on the exit. A 5-minute fade with a stop at a breakout
extreme has a small R, so the round trip is a large fraction of R. This
is the cost the video omits and the reason the two independent tests
failed. Costs are in the scatter, not added after.

Scatter: on the first 70% of calendar days, the mean net R per trade
with the round trip subtracted, and the win rate, plus a **day-clustered
t-statistic** of the mean net R (setups inside a day are not independent
samples; the unit is one day). No equity curve before this picture.
Ambiguous bars are resolved against the trade: if one bar touches both
stop and target, the stop is taken. Gaps through the stop are not
improved; gaps beyond the target get no extra credit.

Walk-forward: the last 30% of days, once, same statistic. A pass means
the walk-forward number has the same sign and clears its cost. It is
not a reason to trade it. Sizing stays off until a separate decision.

Kill: the mean net R on the first 70% is at or below zero, or its
day-clustered t-statistic is below 2, or the win rate is below the
break-even rate implied by 2R after costs (about 33.4% gross of the
half-spread). Rule 3 requires at least 120 New York days in the pull
before the 70/30 split, so the holdout has at least 36 days. A result
that only clears the round trip is a kill.

Status: closed. The scatter was run once and met its kill.

Result (BTCUSDT 5m, data-api.binance.vision, frozen 2-year panel
2024-10-12 to 2026-10-15, 211,000 bars, split 513 scatter days / 221
sealed days). Costs 15 bp round trip, charged on notional:

| | first 70% (470 trades, 470 days) |
|---|---|
| win rate (net R > 0) | 31.9% |
| mean gross R | +0.0344 |
| mean net R | −0.8537 |
| day-clustered t | −9.06 |
| exits | 290 stop / 150 target / 30 end-of-day |

The gross number is the finding. Before any cost the fades won 35.5% of
the time against a 33.3% break-even, so the pattern is a very slight
positive gross — smaller than noise on 470 trades. The net number is
−0.85R because a 5-minute stop is tiny: mean R was 47 bp of price
(median 28 bp), so a 15 bp round trip is 0.89R per trade on average, and
0.54R at the median. The edge the video claims is smaller than the cost
of harvesting it, which is exactly what the two independent replications
found. The t-statistic of −9 is not a weak signal, it is a reliable way
to lose the spread.

The holdout was not opened. `scripts/experiments/q5_first4h_range_fade.py`
holds the same freeze and will only score the sealed 30% if the scatter
clears its gate, which it does not.

What this does not decide: whether some other session, R target, or
entry lag turns the same ingredients positive. Each is a new row with
its own reason, written before looking. Adding one now, after seeing
−0.85R, is the overfit the matrix refuses. The one honest follow-up is
to stop pulling this family; the pattern's gross edge is arithmetically
too small to survive a taker round trip at any hold this short.

### Q6. MACD / 200 EMA cross, MACD below zero

Source: a public YouTube video, "BEST MACD Trading Strategy [86% Win
Rate]," TradingLab, 2021 (~4.6M views). The rules are the ones the video
states; the 86% in the title has no trade count, market, timeframe or
test method behind it anywhere in the video or its description. Two
independent public replications also failed to reproduce it, and this
row's numbers land on top of theirs.

Rules as stated: long when the MACD line (12, 26, 9) crosses above its
signal line while both lines are below zero and price is above the
200-period moving average; short on the mirror. Stop beyond the 200 MA,
which the video says "acts as a wall." Target 1.5 x the risk.

Who loses: nobody specific. Unlike Q5, this setup names no counterparty
— it is a trend filter, not a liquidity event. That it still gets a row
is a concession: it is here to test the *claim*, and the claim is
measurable, so one look is cheap. A pattern with no nameable loser is
already a weak candidate under rule 1.

Data: frozen Binance Vision panels (api.binance.com is HTTP 451 from
this host; data-api.binance.vision is not, and provides the depth that
Hyperliquid's ~5,000-bar-per-interval retention cannot). Ten market and
timeframe runs; 15m windows are 365 days and 1h/4h longer. The panel is
frozen to disk before the first scatter.

Locked (choices the video leaves loose, frozen before the test, the same
way the independent replications had to): MACD 12/26/9; the moving
average is a **200-period EMA**; entry is the **next bar's open** after
the cross, not the cross close; the stop sits **0.1 x ATR(14)** beyond
the 200 EMA **as it stood at entry**, so it does not drift; target is
exactly **1.5R**; one position at a time; a fresh cross is required after
an exit; ambiguous bars resolve to **stop**; unresolved positions flatten
at the last close. The video's "wait for a support/resistance bounce"
step is a judgement call and is **not** tested.

Costs: 15 bp round trip on notional (4.5 bp taker fee + 3 bp slippage,
each side), the matrix cost table.

Scatter: per market, the mean net R over all trades, the win rate, and a
t-statistic on trade net R. The decisive number is the gap to the
after-cost break-even win rate, which here is not 40%: with a mean cost
of about 0.28R, a win pays 1.5R − c and a loss pays 1R + c, so
break-even is (1 + c) / (1 + 1.5), about **51%**.

Walk-forward: BTC 1h split 70/30 by calendar time. Scatter and holdout
scored once.

Result (2,109 trades across 10 runs):

| run | n | win | gross R | net R | t |
|---|---|---|---|---|---|
| BTC 1h | 157 | 36.3% | −0.092 | −0.376 | −3.64 |
| BTC 15m | 339 | 33.3% | −0.108 | −0.843 | −8.82 |
| BTC 4h | 65 | 38.5% | −0.034 | −0.098 | −0.65 |
| ETH 1h | 159 | 37.7% | −0.057 | −0.280 | −2.74 |
| ETH 15m | 360 | 34.4% | −0.076 | −0.651 | −8.19 |
| SOL 1h | 142 | 35.2% | −0.120 | −0.299 | −2.92 |
| SOL 15m | 358 | 36.6% | −0.036 | −0.528 | −6.84 |
| BNB 1h | 175 | 40.0% | +0.014 | −0.322 | −3.13 |
| XRP 1h | 188 | 41.0% | +0.064 | −0.178 | −1.89 |
| ADA 1h | 166 | 42.2% | +0.054 | −0.099 | −1.01 |

Mean win rate **37.5%**, highest of any run **42.2%**, against a
break-even near **51%** after costs and 40% before. Not one of ten runs
is net positive. BTC 1h walk-forward: scatter −0.335R, holdout −0.485R,
same sign.

Kill: met. The claimed 86% is about 44 points above the highest win rate
observed and about 35 points above the after-cost break-even. This is
not a marginal miss; the strategy as stated loses on every market and
timeframe tested, and the shorter the timeframe the worse it does, which
is what the 15 bp round trip on a small R predicts. The holdout is
closed. No filter, timeframe, or coin is added afterward to rescue it.

Status: closed.

Q6 addendum, 2026-10-15 — re-test on US equity 5m
------------------------------------------------
The original Q6 ran on crypto, where the frozen stop (200 EMA ± 0.1 ATR)
produced an R so small that the 15 bp round trip ate ~0.9R per trade.
That left an obvious confound: was the edge real but fee-starved? The
free Webull equity feed settles it, because ETF cost is ~25x lower.

Run: `scripts/experiments/macd_ema200_equity.py`, SPY + QQQ 5m, 13,914
RTH bars each (2026-01-29 to 2026-10-14, 179 sessions, volume present,
verified contiguous). Frozen rules copied unchanged from the Q6 harness.
Cost model changed by necessity: 0 commission, 1 tick round trip
(~0.6 bp), with 5 bp and 15 bp scenarios also reported.

| Market | n | win | gross R | net R | day-clustered t |
|---|---|---|---|---|---|
| SPY (frozen rules) | 103 | 39.8% | −0.005 | −0.082 | −0.83 |
| QQQ (frozen rules) | 121 | 39.7% | −0.008 | −0.053 | +0.38 |
| SPY (flatten at RTH close) | 134 | 47.8% | +0.057 | −0.030 | +0.39 |
| QQQ (flatten at RTH close) | 144 | 48.6% | +0.012 | −0.028 | +1.00 |

**The confound is resolved, and it does not help.** At a **zero** cost
assumption SPY is −0.005R and QQQ −0.008R. Gross expectancy is
essentially zero on both, so no cost model was ever the reason this
fails on equities.

The deeper finding is in the exit split, and it is not a cost story:

| | SPY | QQQ |
|---|---|---|
| Mean R (stop distance) | 2.258 (31.6 bp) | 3.220 (47.6 bp) |
| Raw price move per win | +3.101 | +5.160 |
| Raw price move per loss | −2.409 | −3.099 |
| Cost as fraction of R | 0.077R | 0.045R |

Average wins are **smaller in price terms** than average losses — the
1.5R target maps to a larger price distance than the 0.1-ATR stop, so
the win rate needed to break even is pushed *above* the nominal 40%. At
0.6 bp it is 43.1% (SPY) and 41.8% (QQQ). The strategy realises 39.8% and
39.7%. It misses its own break-even by about 3 points, on both
instruments, independently — and that gap is what the 15 bp crypto cost
inflated into −0.854R.

The `flatten at RTH close` variant is worth recording precisely because
it looks like it works and does not. Win rate jumps to ~48%, which is
above the nominal break-even — but average win collapses from +1.44R to
+0.92R while average loss stays ~−0.90R, because most positions are cut
at the close before reaching the 1.5R target. Net stays slightly
negative (−0.030R, −0.028R). Higher win rate, same dead expectancy.

Walk-forward (SPY, frozen rules): scatter n=70 net −0.118R, holdout n=33
net −0.005R. Same sign, both non-positive; the holdout does not open a
positive branch.

Verdict: **Q6 stays closed, now on stronger evidence.** The 86% claim
fails on real-time equity bars with volume, at near-zero cost, on two
instruments, and the failure is in the payoff geometry rather than in
fees or data quality. The equity feed is a durable improvement to this
file's data layer; it did not rescue this row.

### Q7. Orderflow absorption in a Fib discount zone

Source: IQCapital, "Trading WORLD CHAMPION Reveals the Orderflow Strategy
That Won the Robbins Cup", https://www.youtube.com/watch?v=PL7LKUsCgIQ
(58 min, Aug 2026). Chris Creamer won the Robbins World Cup micro
division with a reported 100% month; he reports 60-65% win rate and
about 1.8 profit factor at 1.5-2R. This is the first source in this
file written by a competition winner rather than a content channel, and
it is the most sophisticated: environment -> location -> confirmation ->
execution, resting on auction theory, gamma regime, volume profile and
orderflow.

Locked (frozen before any test, from the video's own description):

| element | rule |
|---|---|
| window | New York open, first 90 minutes only |
| context | prior three sessions making higher highs and higher lows |
| value area | prior UTC day profile, 70% around POC |
| location | Fib 0.705 / 0.786 / 0.886 on swing low -> swing high, **whole zone below the value area low** |
| invalidation | a 5-minute close below the 0.886 level |
| participation | bar volume at least 60% of the rolling median |
| absorption | aggressive sellers (delta below mean - 0.75 sd) at the zone, candle closes off its low |
| dominance shift | next candle makes no lower low, closes bullish, above the prior candle's close |
| entry | next 5-minute open after the shift |
| stop | below the failed-seller low |
| target | prior swing high, floored at 1.5R; stop to break-even at +1R |
| costs | 15 bp round trip on notional |

Data: 316,000 frozen 5-minute BTCUSDT bars, 2023-10-14 to 2026-10-15,
from Binance Vision, which publishes the exchange's own aggressor split
(`taker_buy_base`), so delta is real orderflow and not an estimate.

Attempted test, and why it does not decide the row
--------------------------------------------------
Three sessions of 5-minute bars, 1,098 sessions, 145 value-up days. The
setup fired zero times. The funnel is the finding:

| stage | count |
|---|---|
| bars in the 90-minute window | 19,728 |
| passes the participation floor | 18,763 |
| value-up context | 145 sessions |
| has a swing leg to draw the Fib on | 84 |
| Fib zone sits outside the value area | 23 |
| **completed setups** | **0** |

Rather than report that as a verdict, the freeze was re-examined against
the video and the two softest readings were each relaxed, separately and
together, before concluding anything:

* context as a rising POC (his auction-theory language) instead of
  strictly higher highs and lows: 216 value-up days instead of 145.
* confirmation without requiring the absorption candle itself to close
  bullish, matching the video's sequence ("candle closed bullish, next
  one pulls back and fails higher") rather than requiring both flags on
  one bar.

Neither change produced a trade. Two structural facts explain why, and
neither is a bug:

1. The zone is rarely armed. A Fib drawn 0.705-0.886 below a swing high
   sits far below price, and requiring the *whole* zone under the value
   area leaves only 23 of 84 candidate sessions in discount. The setup
   is meant to be rare; on one instrument it is rare enough that three
   years is a small sample.
2. His guard is a candle that wicks into the zone but *closes back above
   it*. My freeze lets the zone be tapped when the close is inside it,
   which admits candles that instead closed deep in the zone and then
   trended through. Tightening that to his wording cuts the tap count
   from 38 to fewer than 8, which makes the zero stable rather than
   arguable.

What this row does and does not say
-----------------------------------
It does **not** say the strategy fails. Nothing was exercised. It says
the headline claim was not reproduced on the data available here, and it
names the two inputs that make a faithful test impossible with public
data:

* GEX. He trades the options-dealer gamma regime, and reads call wall,
  put wall and gamma flip before the open. Historical option chains are
  not served for past dates and CBOE data is paid; Deribit exposes only
  the live chain and the DVOL index. A price-derived volatility proxy
  was carried in the harness but, with no trades, it never gated
  anything. A live Deribit read is possible; a multi-year backtest of
  the regime is not.
* Footprint. He reads the bid-by-ask ladder inside each 5-minute candle
  (`ATAS`-style), including 400% imbalance highlighting. Only per-candle
  aggressor splits are available as history. Those are real orderflow
  and were used, but the absorption signature he actually trades is
  described at a resolution this data does not reach.

The honest exit is therefore not a kill and not a pass. It is a note
that the vocabulary matters: this row is blocked on data that a
World Cup winner would call the core of the method, and the parts that
*are* reproducible (session clock, value area, Fib discount, aggressor
delta) produced too few events on one crypto instrument to score. He
trades MNQ, in the most liquid hours, on a 58-minute recording of a
competition month; three years of BTC was the closest free stand-in, and
it is a different book.

If this family is revisited, the order of work is: pick an instrument
and session where the pattern is frequent enough to reach 120 events
before any split, keep the value-shift and Fib rules as frozen here, and
accept up front that the gamma and footprint layers cannot be backtested
from free history. Without those layers this is not his strategy; it is
a mean-reversion pattern with a session filter, and the existing rows
already show what those cost.

Status: not decidable on available data. Not a kill. The holdout was
never opened because the scatter never formed.

Q7 unblock path (updated 2026-10-15, second pass)
--------------------------------------------------
The two blockers above are both served by Webull OpenAPI, which was
identified after this row was written:

* Footprint — `get_futures_footprint` returns the bid-by-ask ladder per
  bar for US futures. This is the `ATAS`-style layer, from the vendor.
* GEX — `get_option_snapshot` / `get_option_history_bars` return the
  chain with greeks and open interest, which is what a dealer-gamma
  calculation needs. `get_noii_bars` adds net options imbalance.

`get_futures_history_bars` supplies MNQ/NQ intraday — the instrument the
source actually trades — so the "three years of BTC was the closest free
stand-in" caveat above stops applying.

### Re-test after the subscription was activated (2026-10-15)

A Webull OpenAPI subscription was activated and every layer was
re-exercised live against the `user-webull` MCP server. **Neither Q7
blocker cleared.** The exact responses:

| Layer Q7 needs | Result after re-test |
|---|---|
| `get_futures_footprint` (MNQZ6, M1 and M5) | `MARKET_DATA_NOT_SUBSCRIBED:Insufficient permission, please subscribe to FOOTPRINT.` |
| `get_stock_footprint` (AAPL, SPY) | `MARKET_DATA_NOT_SUBSCRIBED:Insufficient permission, please subscribe to FOOTPRINT.` |
| `get_futures_bars` / `_tick` / `_snapshot` (MNQZ6) | `MARKET_DATA_NOT_SUBSCRIBED:Insufficient permission, please subscribe to US_FUTURES.` |
| `get_futures_depth` (MNQZ6) | `MARKET_DATA_NOT_SUBSCRIBED:Insufficient permission, please subscribe to FUTURES LV2.` |
| option chain (any `get_option*`) | **does not exist** — the 83-tool catalog exposes only option *order-drafting* tools, no chain/greeks endpoint |

What *did* turn on is the equity microstructure group: `get_stock_quotes`
now returns full priced ladders (`depth=1`, `5`, `50` all priced, with
per-`mpid` `order` detail), and `get_stock_tick`, `get_stock_noii_bars`
and `get_stock_capital_flow` also return live data. That is an L1/L2
equity entitlement, not Order Flow.

So the honest position is unchanged in substance: **the footprint layer
is still absolutely unavailable, so Q7 remains not decidable.** [Superseded
for footprint only — see the third-pass update below; the verdict itself is
unchanged because the GEX half is still missing.] Even
setting footprint aside, the GEX half is worse off than the note above
assumed — there is no option-chain market-data tool at all, so real GEX
cannot be computed in the backtest *or* read forward from this server.
The earlier "OPRA is free if you trade options monthly" lever applies to
the OpenAPI backend, but no MCP tool exposes chains through it yet.

Update (2026-10-15, third pass): a live re-test now shows
`get_futures_footprint` / `get_stock_footprint` returning **live ladders**
(`delay_minutes: 0`), so the footprint half of the blocker above has
cleared; CME futures L1/L2 (`US_FUTURES`, `FUTURES LV2`) and options chains
remain closed. See the "Entitlement identity" section of
`WEBULL_MCP_SETUP.md` for the evidence. Status of this row is unchanged.

Revisit order when (if) a `FOOTPRINT` subscription is added: confirm
`get_futures_footprint` returns `buy_detail`/`sell_detail` with a real
per-price ladder rather than the aggregated `total`/`delta`/`buy_total`/
`sell_total` the MCP formatter is known to print (see `WEBULL_DATA.md`),
reach 120 events on MNQ, then rerun the frozen Q7 rules and only then
open the holdout. Until the footprint clears, the status stays
**not decidable**.

Q7 updated status (2026-10-15, fourth pass) — partially decidable
----------------------------------------------------------------
The revisit order above was executed. The footprint layer **did clear**,
and it cleared at the resolution the row needed:

* `get_futures_footprint` (MNQZ6, ESZ6) and `get_stock_footprint`
  (SPY, QQQ, AAPL, NVDA) return **real per-price ladders**, not the
  aggregated totals the MCP formatter prints. Each bar carries
  `buy_detail` / `sell_detail` as `{price -> size}` maps on the
  instrument's tick grid, summing exactly to `buy_total` / `sell_total`,
  with `total == buy_total + sell_total` and
  `delta == buy_total − sell_total`, all verified on 1,200/1,200 MNQ
  bars. `delay_minutes: 0`. So the Q7 **confirmation layer** — absorption
  and the dominance shift, read from the bid-by-ask ladder — can now be
  constructed and tested. The earlier objection that "only per-candle
  aggressor splits are available" no longer applies to the construction
  step.

What is now testable, and what is not:

1. **Testable (confirmation).** The footprint half of layer 3 —
   aggressive-seller absorption at the zone, and the bullish dominance
   shift on the next bar — can be computed on MNQ itself. The harness
   `scripts/experiments/orderflow_absorption_backtest.py` currently
   uses `delta = 2*taker_buy − volume` from Binance; the Webull ladder
   supplies a true per-price delta instead and is the instrument the
   source actually traded.
2. **Hard-capped (history).** Footprint has **no date-range parameter**
   and `count` clamps to 2000, so MNQ 5m reach is ~9.2 days, not the
   120 days a 70/30 split needs (rule 3). On equities the same cap
   reaches ~222 days at 30m, but that is a different instrument and a
   coarser clock than the source's 5m. **On MNQ 5m alone the row still
   cannot reach its own event floor.** Any faithful test has to either
   (a) accept a shorter paper with no holdout, or (b) build the panel
   forward as a live recorder.
3. **Blocked (gamma context).** This is unchanged and now looks
   permanent for this server. There is **no option-chain market-data
   tool anywhere in the ~83-tool catalog** — only `place_option_*`
   order *drafting*. So real dealer gamma / GEX cannot be computed from
   history **or** read forward here. The harness's price-derived vol
   proxy tests the behavioural claim, not the options positioning; it is
   not the champion's context gate. That layer is absent, not merely
   shallow.
4. **No price bars from footprint.** The footprint record carries **no
   OHLC** (`time`, `total`, `delta`, `buy_total`, `sell_total`,
   `buy_detail`, `sell_detail`, `trading_session` only), so it does not
   route around the locked `US_FUTURES` bars line. A separate MNQ price
   feed is still required; that subscription is still LOCKED.

So the precise status is **partially decidable**: the confirmation half
can be built and tested, the gamma context gate cannot be backtested at
all from this server. This is **not** a claim of edge and **not** a
kill. The holdout stays sealed — nothing has been scored, and the
frequency problem the earlier attempt found (the Fib-outside-value zone
is rarely armed; zero setups in three years of BTC) is untouched by
having a better confirmation layer. Adding that layer does not make the
setup fire more often.

Concrete next step, in order:
1. Freeze a small MNQ 5m footprint panel (~9 days) plus an MNQ price
   series (still locked; would need the CME L1 line, or an equity proxy
   such as QQQ with the caveat that it is a different book).
2. Re-derive the frozen Q7 funnel with true ladder delta and confirm the
   setup fires at all at 5m resolution before spending a test.
3. If it fires, count events. If the count is below the 120-event floor,
   the honest outcome is a forward recorder, not a backtest.
4. Record the gamma gate as **forward-only and unavailable** — do not
   substitute the vol proxy and call it his strategy.

| element | now testable? | why |
|---|---|---|
| context: prior-3-session structure | yes | bar-derived |
| context: prior-day value area | yes | bar-derived |
| **context: dealer gamma / GEX** | **no — absent** | no option-chain tool exists |
| location: Fib discount zone | yes | bar-derived |
| **confirmation: absorption + dominance shift** | **yes — NEW** | per-price ladder now live |
| execution: entry / stop / 1.5–2R target | yes | bar-derived |
| sample size on MNQ 5m | **no** | ~9.2-day cap, no pagination |

Q7 fifth pass (2026-10-15) — the fire-check: it fires **zero** times
------------------------------------------------------------------
The concrete next step above (step 2) was executed: feed the frozen funnel a
**true ladder delta** on MNQ and confirm whether the setup fires at all at
5-minute resolution. This is a fire-check, **not** a scored backtest, and
**not** an edge claim. Nothing was scored and no holdout was opened.

**MNQ price bars: still LOCKED — re-tested fresh, not inferred.** The
footprint record carries no OHLC, and every MNQ price endpoint was called
live before accepting that. The exact responses on 2026-10-15:

| Endpoint (MNQZ6) | Live response |
|---|---|
| `get_futures_bars` | `MARKET_DATA_NOT_SUBSCRIBED:Insufficient permission, please subscribe to US_FUTURES.` |
| `get_futures_snapshot` | `MARKET_DATA_NOT_SUBSCRIBED:...subscribe to US_FUTURES.` |
| `get_futures_tick` | `MARKET_DATA_NOT_SUBSCRIBED:...subscribe to US_FUTURES.` |
| `get_futures_depth` | `MARKET_DATA_NOT_SUBSCRIBED:...subscribe to FUTURES LV2.` |
| `get_stock_bars` / `get_stock_bars_single` with `MNQZ6` | `INVALID_SYMBOL:The symbols does not exist in the category.` |

The propagation lesson was applied: this is a **fresh re-test**, not a
single early error, and the CME futures line is genuinely still dark while
Order Flow is live. `get_futures_instruments` confirms only two MNQ contracts
exist (`MNQZ6`, `MNQH7`) — no alternate spelling unlocks bars. Off-Webull
price sources were also checked and are unavailable from this host: Yahoo
`NQ=F` 5m returns **HTTP 429**, Stooq returns a JavaScript bot-challenge, and
there is no Webull NQ-linked ETF with 5m OHLC on the same book (QQQ is a
different, cash-session book — adding it would weaken the result, so it was
not used).

**How the missing OHLC was handled (nothing fabricated).** The ladder gives
some fields exactly and cannot give others, so the two were kept separate
rather than blurred:

| field | source | quality |
|---|---|---|
| `high` / `low` | max / min over the union of `buy_detail` ∪ `sell_detail` keys | **EXACT** — every traded price is in the record |
| `volume` | `total` | **EXACT** |
| `delta` | wire `delta` (true per-price ladder delta) | **EXACT** — better than the harness's BTC estimate |
| `open` / `close` | size-weighted mean traded price (VWAP) | **APPROXIMATED** — a level, not a close |

Frozen rules that depend on `open`/`close` are therefore **approximated, not
faithful**: the invalidation close, "closes off its low", "closes bullish",
"above the prior close", and the next-bar-open entry. The rules that depend
only on high/low/volume — the entire **context**, **value area**,
**participation floor**, and **location** layers — are **faithful**. That
distinction decides the whole result below: the funnel dies inside the
faithful part.

**The panel.** One `get_futures_footprint` call, `MNQZ6`, M5, `count=2000`:

* **2,000 bars returned** (requested 2000; confirms the 2000 clamp).
* Span **2026-10-06 03:00 → 2026-10-15 08:35 UTC = 9.23 calendar days**.
* Contiguous on the 5-minute grid: 1,992 gaps of exactly 5 min, 6 gaps of
  65 min (the daily 17:00–18:00 ET halt), 1 gap of 2,945 min (a weekend).
* All bars carry `trading_session: "RTH"`, `delay_minutes: 0`.
* Invariants hold **2,000/2,000**: `total == buy_total + sell_total`,
  `delta == buy_total − sell_total`, and each ladder sums exactly to its
  side total. Zero violations.
* `sessions` filtering is not usable to widen reach: the server already
  returns RTH only, and `OVN` is documented as unsupported.

**The frozen funnel** (true ladder delta, harness `context()`):

| stage | count |
|---|---|
| bars in the 90-minute window | 126 |
| passes the participation floor | 126 (strict) / 116 (window-pooled) |
| value-up context | 2 |
| has a swing leg to draw the Fib on | 1 |
| Fib zone sits outside the value area | **0** |
| **completed setups** | **0** |

The funnel dies one stage **before** any approximated close is consulted. On
the single armed session (2026-10-14) the Fib zone landed at
`[30732.42, 30758.48]` while the prior-day value-area low was `30680.24`, so
the zone sat **inside** the value area, not below it. The `candidates` gate
never opens, so the confirmation and execution layers are never reached.

Two further honest notes on this zero:

1. **The frozen `context()` has an inverted direction.** It labels "up" when
   the three prior sessions have *descending* highs and lows (`d1 > d2 > d3`
   with `d1` oldest), whereas the written rule says "higher highs and higher
   lows" (`d1 < d2 < d3`). Under the doc-faithful direction the table is
   **0 / 0 / 0** — no value-up session at all. Either way the location count
   is 0. The headline uses the frozen function so this zero is computed the
   same way as the BTC zero.
2. **The participation row does not gate the funnel.** Despite the frozen
   rule table and the BTC write-up listing it, `simulate()` never applies
   `PARTICIPATION_MULT` — it only records `ctx_up` / `leg` / `candidates`.
   The count is reported for accounting only and cannot change the setup
   count.

**What this does and does not license.** With ~9.2 days the panel builds only
**9 daily sessions, 6 of which reach the 90-minute New York window** and 8 of
which can serve as a prior-day value area — nowhere near the row's own
120-event floor. The number is a fire-check, not a verdict. Zero here is **weaker** evidence than the three-year BTC zero, not
stronger — 9 days versus 1,098 sessions. But it is the *same* zero, on the
instrument the source actually trades, and it dies at the *same* stage: the
Fib-outside-value zone is rarely armed. That is consistent with the rarity
being structural to the rules rather than only a small-sample artifact of
BTC, and it does not require the instrument to be wrong. It remains
unconfirmed by a sample large enough to score.

Status after the fire-check: the funnel is confirmed **not to fire** on this
instrument and window at 5m; the result is **sample-limited and cannot reach
the 120-event floor**, so the row stays **not a claim of edge**. If the
family is revisited, the honest path is a forward recorder that accumulates
MNQ 5m footprint past 9 days (the cap has no pagination), plus the CME L1
line for faithful OHLC — not another 9-day fire-check.

### Reproducing the fire-check

```bash
.venv/bin/python scripts/experiments/q7_footprint_firecheck.py
# optional: --panel reports/.webull_cache/<your_pull>.json
```

`scripts/experiments/q7_footprint_firecheck.py` imports the frozen harness by
path (`importlib`) so the rules stay single-sourced — it does **not**
copy-paste them. The harness has no module-level fetch side effect (`fetch()`
is only called inside `main()`, which is `__main__`-guarded); the one import
side effect is that it creates its own empty `reports/.orderflow_cache/`. The
panel is pulled separately with the `user-webull` MCP tool and cached under
`reports/.webull_cache/` (gitignored); the script itself makes no network
calls.

### Q8. SLC (Structure / Level / Confirmation) — differential test of the stochastic filter

Source: a public YouTube video, "The BEST Day Trading Strategy For Beginners
in 2026 (Simple and Proven)," Data Trader,
https://www.youtube.com/watch?v=ExvoIqNglOk. Branded **SLC** = Structure,
Level, Confirmation. The video contains **no statistical evidence**: one
after-the-fact worked example, n=1. No win rate, no sample, no profit factor
is claimed, and "works every single day" is unfalsifiable. It closes with a
lead-magnet funnel ("free SLC execution blueprint"), so its incentive is
engagement, not accuracy. The stochastic settings were shown **on screen and
never spoken** in the transcript, so they are not recoverable from text. The
video trades **Nasdaq futures (NQ)**; `get_futures_bars` is locked on this
credential, so the specified proxy is **QQQ** (Nasdaq-100 ETF) — the same
underlying index, a different book, cash session only. That is a caveat, not
a wash.

The rules as stated: read structure on 4H and trade only in a trend (long
only in an uptrend, short only in a downtrend; stand down in consolidation);
on 5m mark supply/demand zones as the last candle before a sharp aggressive
move; take the touch of a never-tested zone, or the retest after it is
broken once; confirm with a stochastic break of the band then a cross back;
enter at the zone in the structure's direction, stop slightly beyond the
zone, target 2R.

What is actually tested here is **one claim**, not the whole method: that the
stochastic confirmation filter has *selectivity* — that zone touches it
confirms behave differently from zone touches it does not confirm, **at the
same fixed zone set**. It is not a claim of edge for the source and not a
verdict on the SLC method as a whole.

Who loses: the trader who chased the aggressive move into the zone and is
now trapped/offside, exiting at breakeven into the zone, plus the breakout
trader who buys the retest. If the zone instead holds and the trend resumes,
the counterparty is this bot and the row is the wrong way round. Either
group's forward return may show either sign; the sign is not chosen after
looking.

Data: QQQ 5-minute RTH bars, category `US_ETF`, from the Webull free tier.
The SDK path (`/home/hlbot/webull-venv` + `WebullData.fetch_stock_history`)
and the `user-webull` MCP path were **both unavailable this pass**, so the
deepest reachable panel is the already-frozen
`reports/.webull_cache/QQQ_5m_RTH_frozen.json` (2026-01-29 → 2026-10-14,
13,914 RTH bars, 179 sessions). The documented boundary stands in the result
below; the intended ~2010 start was **not** reachable and is not fabricated.
4H structure is **resampled from the same 5m panel** (fixed 4-hour UTC
buckets) so the clocks are guaranteed consistent; no separate API call.

Locked (three rule-choices, frozen ex ante, before any scatter):

| # | rule | locked value |
|---|---|---|
| 1 | **Displacement** ("sharp aggressive move") | a run of at most `DISP_MAX_BARS = 3` consecutive 5m bars whose net move `\|close[j] − open[i]\|` is at least `DISP_ATR_K = 1.5 × ATR(14)[i]`, all bars moving the same way. The zone candle is bar `i − 1`; its band is `[low(i−1), high(i−1)]`. |
| 2 | **Stochastic** | `%K` period 14, smooth 3, `%D` 3, bands 80/20. Bands are the confirmed default for the source's unrecoverable on-screen settings; the grid is reported as a named parameter, not tuned to results. |
| 3 | **Structure lookback** | fractal swing of `STRUCT_P = 3` bars each side on the 4H (resampled) series; trend from the last two swing highs and last two swing lows. Uptrend = both rising; downtrend = both falling; anything else = consolidation. |

Other discretionary choices, each a **named parameter** and each disclosed as
**my operationalization, not the source's rule** (the source leaves all of
these undefined): touch = a bar whose `[low, high]` intersects the zone band,
starting a new touch only on the first bar of an excursion (no re-count while
price stays in the zone); far-edge break = a 5m **close** beyond the zone
band by `BREAK_TOL_ATR = 0.25 × ATR(14)`; a zone is retired after
`MAX_BREAKS = 2` such breaks ("chopped through multiple times"); confirmation
window `CONF_WIN = 6` bars; forward horizon `H = 12` bars (60 min); entry is
the **next bar's open** after the touch bar, identical for both groups; the
displacement direction sets the zone type (up leg → demand, down leg →
supply); a zone is kept only if its type matches the structure state at
creation (demand in an uptrend → long, supply in a downtrend → short). The
video's "slightly above the supply level" stop is set at the zone's far edge
± `STOP_TOL_ATR = 0.1 × ATR(14)`, the tight-stop convention the source's
worked examples use.

Costs: the equity row convention from the Q6 addendum — **0.6 bp round trip**
(1 tick each side on QQQ, zero commission), subtracted from both groups, so
to first order it cancels in the confirm-minus-unconfirmed gap but not in
either group's own sign. A matrix-default 15 bp scenario is reported beside
it.

Statistic: on the first 70% of calendar time, the **mean signed forward
return over H = 12 bars, in bp, cost subtracted**, for confirmed touches
minus unconfirmed touches, with a **day-clustered t-statistic** (touches
inside a session are not independent; the unit is one session). The full
decile distribution of each group is reported, not just the mean. The
fixed-horizon bp measure is the headline because it carries no path
dependency and no stop geometry; the actual 2R/stop simulated trades are
reported beside it as context only.

Kill: the confirmed-minus-unconfirmed mean is not significant (day-clustered
t < 2) on the first 70%, **or** the last 30% has the opposite sign. Rule 3
wants at least 120 sessions before the split; 179 sessions clear it.

The differential design rationale: comparing confirmed versus unconfirmed
touches **at the same zones**, holding the zone set completely fixed and
varying only the confirmation label, is the tightest available test of
*selectivity*. If the filter is decoration, the two groups' forward-return
distributions are the same and that can be said definitively. If the filter
genuinely selects, a gap appears. The video's story that the filter "saved
you from a losing trade" is currently an anecdote, n=1; this turns it into a
two-sample question over a fixed zone set. Holding the zone set fixed removes
the usual confound — a different filter changes which zones are *found* as
well as which touches are *taken* — and leaves only the filter's own
discrimination.

Status: **tested — verdict below.**

Result (QQQ 5m RTH, Webull frozen panel, 13,914 bars 2026-01-29 → 2026-10-14,
179 sessions; split at 2026-07-29 → 221 scatter touches / 106 sealed).
Harness: `scripts/experiments/slc_qqq_differential.py`.

The single fixed zone set: **339** structure-matched displacement zones,
**327** touches (288 first-touch / 39 retest). Confirmation is a property of
the touch, not the zone, so the zone set is identical for both groups.

| first 70% (221 touches, 81 confirmed / 140 unconfirmed) | confirmed | unconfirmed |
|---|---|---|
| mean signed forward return, H=12 bars (~60 min), net 0.6 bp | **−4.43 bp** | **+3.67 bp** |
| median | +3.90 bp | +10.68 bp |
| sd | 68.15 bp | 71.52 bp |
| deciles (p10/25/50/75/90) | −74/−35/+4/+33/+66 | −59/−26/+11/+44/+65 |

Headline differential: confirmed minus unconfirmed = **−8.10 bp**,
day-clustered **t = −0.88**. The gap is the *wrong sign* and nowhere near
significance; −8.1 bp is 13.5× the round-trip cost. **Kill met.** The
holdout (last 30%, after 2026-07-29) was **not opened** — no holdout
statistic was computed or printed, per matrix rule 5.

The split is ~37% confirmed, so the filter is not merely rare. But the
confirmed group is not better on any axis: lower mean, lower median, lower
decile spread. If anything the stochastic cross marks touches that go on to
do slightly *worse* than the unconfirmed ones — the opposite of the video's
"the filter saved you from a losing trade."

Sensitivity grid, each cell re-running the whole test on the 70% (reported
for transparency, not as a search for a winning cell):

| named parameter | values | gap (bp) | clustered t |
|---|---|---|---|
| displacement k (× ATR) | 1.0 / **1.5** / 2.0 / 2.5 | −5.96 / **−8.10** / −7.52 / +0.96 | −0.98 / **−0.88** / −0.81 / +0.06 |
| displacement window (bars) | 2 / **3** / 4 | −0.63 / **−8.10** / −5.47 | −0.05 / **−0.88** / −0.67 |
| structure fractal p | 2 / **3** / 4 | +6.92 / **−8.10** / +1.35 | +0.74 / **−0.88** / +0.17 |
| stochastic settings | **14,3,3** / 9,3,3 / 21,3,3 / 14,1,3 | **−8.10** / +0.95 / −7.55 / −21.59 | **−0.88** / +0.11 / −0.86 / −1.80 |
| confirmation window (bars) | 3 / **6** / 12 | −3.60 / **−8.10** / −45.18 | −0.28 / **−0.88** / −3.80 |
| cross type | **kd** / band | **−8.10** / −7.44 | **−0.88** / −0.82 |
| forward horizon H (bars) | 6 / **12** / 24 | −7.66 / **−8.10** / −13.32 | −1.16 / **−0.88** / −1.09 |
| zone lifetime (bars) | 130 / **390** / ∞ | −6.83 / **−8.10** / −2.65 | −0.66 / **−0.88** / −0.30 |
| max breaks before retired | 1 / **2** / 3 | −11.04 / **−8.10** / −5.12 | −1.02 / **−0.88** / −0.56 |

(**) = locked value.** No cell in any row produces a positive gap that is
significant, so the negative headline is not an artifact of the frozen
choices. Two cells are worth naming because they look like findings and are
not:

* `conf_win = 12` gives gap **−45.18 bp, t = −3.80** — the one *significant*
  cell, and it is significantly *negative*. A wider confirmation window
  admits many more touches (121 vs 81) and the extra ones are worse. This is
  the filter selecting *against* itself, not for itself, and it is a
  sensitivity cell, not the headline.
* `disp_k = 2.5` gives a positive gap (+0.96 bp) on 54 touches, t = +0.06 —
  noise, not a signal, and the smallest sample in the row.

Context-only: the simulated 2R/stop trades on the same 70% (not the
headline, which is the fixed-horizon bp measure) agree in direction —
confirmed meanR −0.128 (n=81, 38.3% win, 64 stop / 17 target) vs unconfirmed
+0.155 (n=140, 43.6% win, 91 stop / 49 target). Both groups' median trade is
about −1.01R; the unconfirmed group reaches the 2R target more often. The
2R geometry does not rescue the filter either.

What the numbers do and do **not** license:

* They **do** license: on this instrument, span and operationalization, the
  stochastic confirmation step shows **no selectivity**. The confirmed and
  unconfirmed touch populations have the same forward-return distribution to
  within noise, and the point estimate favours the unconfirmed group. The
  video's claim that the filter is the thing that separates winners from
  losers is not reproduced here; it is untested, not supported.
* They do **not** license: a verdict on the SLC method as a whole, or on the
  source. Only the confirmation step was isolated for a test; structure and
  level selection were frozen as *inputs* to the differential, not scored on
  their own. This is not "SLC loses money" — no equity curve was produced and
  no position was sized.
* They do **not** license: a claim about NQ. QQQ is the specified proxy — the
  same underlying index on a different book, cash session only — and the
  source trades NQ futures, which remain locked on this credential. A
  different book can have a different zone population.
* They do **not** license: a claim about 2010–2026. The intended deep panel
  was unreachable (both access routes down this pass), so the test covers
  2026-01-29 → 2026-10-14 only — **one regime, 179 sessions**. The sampler
  clears the rule-3 floor of 120 sessions but is short by the standard of
  Q5/Q6. A single-year negative is weaker evidence than a multi-year one.
* The **entire** specification is my operationalization, not the source's
  disclosed rules: "sharp and aggressive move", the trend definition, "clean
  break", "chopped through", "slightly above the supply level", the
  confirmation window, and the stop are all undefined in the video. The
  locked numbers are stated above and frozen ex ante, but a different reading
  of any of them is a different test.

The honest exit is a kill with a boundary: the confirmation filter does not
select on one year of QQQ, and the negative is robust across the grid. It is
not a strong kill, because the sample is one regime. If the family is
revisited, the order of work is to reach the deepest possible QQQ span
(multi-year, which needs the ~2010 floor that was unavailable this pass),
keep these frozen rules, and only then re-run the same differential.

### Reproducing the Q8 differential

```bash
.venv/bin/python scripts/experiments/slc_qqq_differential.py          # headline
.venv/bin/python scripts/experiments/slc_qqq_differential.py --grid   # + grids
```

`scripts/experiments/slc_qqq_differential.py` reads the gitignored panel
`reports/.webull_cache/QQQ_5m_RTH_frozen.json` (13,914 RTH bars, a
de-duplicated copy of `QQQ_5m_raw.json` — **not** touched the sibling
`SPY_1m` file). Zone detection (`build_zones`), confirmation
(`confirm_trigger`) and the comparison (`differential`) are separate
functions so the differential is auditable; `--grid` re-runs the whole test
per named parameter. It makes no network calls. The `--grid` cells do not
change any frozen default.

### Q9. Ross Cameron's "first pullback" momentum entry — differential test of the entry geometry

Source: a public YouTube video, "Growing a $2k Account to $65,662.04 in 30 Days
(My Day Trading Strategy)," **Ross Cameron, Warrior Trading**,
https://www.youtube.com/watch?v=xGIa8Vg0PWM. The video contains **no
statistical evidence at all**. It is a narrated worked example plus an
account-growth claim — $2k to $65,662.04 in 30 days — from one account, over
one month, self-reported and unaudited in the video. There is no win rate, no
sample size and no profit factor anywhere in it; he says explicitly that *"my
results aren't typical."* That growth figure is a **marketing artifact, not
evidence**, and it is repeatable only by taking extreme concentration risk.
Nothing below treats it as a result, and this row is not a claim that the
strategy makes money.

His five steps, as stated: **(1) stock selection** — relative volume ≥ 5× the
50-day average, high total volume, gapping up ≥ 2% from the prior close, price
$2–$20, and float < 20M shares; **(2) entry, the "first pullback"** — a pullback
that retraces no more than 50% of the initial move, has higher volume on green
candles than red, does not break below VWAP, does not break below the 9 EMA and
has no big topping tails, entered on the **"crossing candle"** (the first candle
to make a new high against the prior candle), stop at the pullback low, target
≥ 2R; **(3) execution** — read Level 2 for big/hidden sellers, discretionary;
**(4) exits** — six discretionary tape-and-chart signals, with no fixed cap on
winners; **(5) journalling** — and he stops trading after 10am because his own
statistics are negative after it. Only steps 1 and 2 are mechanical.

**The claim this row tests is not his universe, and that has to be said first.**
He locates the profitability in step 1 ("the most important step") and says the
pattern *"does not work very well"* on large-cap, high-liquidity names
("playing chess against the computer... you really don't have a good edge").
The float pillar — the supply-side half of that screen — **has no crypto
analog**: relative volume, total volume, the gap/momentum and the price band
all map onto a crypto pair, but share float does not exist on a spot pair whose
supply is transparent and elastic. No substitute is invented here. So the
frozen screen is relative volume plus the momentum move only, and the test
below is run **where the source says the pattern fails**, on liquid pairs. That
is a limitation of the test, not a refutation of the source.

What *is* testable, and decoupled from the screen, is his stated mechanism:
**patience in the pullback buys a much tighter stop, so the same move yields a
far better payoff ratio.** His words: *"if you jumped in right here, what's
your max loss? It's way back down before the move started... once it pulls
back... that becomes your low... and that is your stop."* The falsifiable claim
is therefore about **payoff geometry, not accuracy**:

> Given an **identical** momentum event, does entering on the first-pullback
> "crossing candle" beat **chasing** the extension? The pullback entry is
> supposed to win by paying less for the same stop risk, so it can win on
> realized R while losing on hit rate. A design that measures only win rate
> would miss his actual claim, which is why both are reported.

Who loses: **the momentum chaser who buys the extension** and is then forced to
put his stop at the base of the move, so a routine retrace stops him out for a
large loss — and, on the other side of the same tape, **the late short who is
squeezed out** as the move extends. If the extension instead continues without
a usable pullback, the counterparty is this bot and the row is the wrong way
round. The sign is not chosen after looking.

Data: Binance Vision 5-minute spot bars, `data-api.binance.vision`, 30 USDT
pairs spanning large, mid and small liquidity, frozen to disk under
`reports/.binance_cache/` **before** the first scatter. `api.binance.com`
returns HTTP 451 from this host; the `.vision` mirror does not, and it serves
monthly zips back to each pair's listing month, which the 1,000-bar REST
pagination cannot match for 30 pairs. The venue choice is a caveat: the pairs
are signal instruments, and a Binance-USDT book is not Cameron's US small-cap
tape. Universe and span are reported in the result; a pair added afterwards is
a new row.

Locked (three rule-groups, frozen ex ante, before any scatter; the
operationalizations the source leaves loose are named parameters in the
harness, each disclosed, and none is tuned to a result):

| # | rule | locked value |
|---|---|---|
| 1 | **Momentum event** ("the squeeze") + participation | a run of at most `DISP_MAX_BARS = 3` consecutive 5m bars, **all green**, whose net move `\|close[last] − open[first]\|` is at least `DISP_ATR_K = 1.5 × ATR(14)` measured at the bar before the run; the run's summed volume is at least `RVOL_MULT = 5.0` × (`DISP_MAX_BARS` × the median 5m volume of the prior `RVOL_WIN = 288` bars). 5× is his disclosed relative-volume number; that his figure is a *daily* ratio and this one is a *bar* ratio is disclosed, not hidden — the levels are not comparable, only the direction of the filter is. Long events only: his pattern is "gapping up," so the short mirror is **not** tested. |
| 2 | **Pullback constraint** | his disclosed numbers. The pullback retraces **≤ 50%** of the move (`RETRACE_MAX = 0.50`, measured leg-low to leg-high). Through the whole pullback every 5m bar's **low** must hold **at or above the session VWAP** (VWAP anchored to the **UTC day**) **and at or above the 9 EMA**. Any bar that trades below either line kills the pullback, and the event produces no pullback entry. |
| 3 | **Entry, geometry and ambiguity** | **PULLBACK** — the crossing candle is the first bar after the move's end that makes a new high against the immediately prior candle; entry is the **next bar's open**, stop is the **pullback low**, target exactly **2R**. **CHASE** — entry is the **next bar's open after the move ends** (the naive momentum buyer), stop is the **base of the move** (the lowest low from the base candle through the move's last bar — his "way back down before the move started"), target exactly **2R**. Both arms share a time stop of `MAX_HOLD = 36` bars (3 hours), then flat at that bar's close, so no event is silently dropped. Ambiguity is **pessimistic** (as `ifvg_gold_backtest.py`): if one bar touches **both** stop and target, the **stop** is taken; gaps through the stop are not improved and gaps beyond the target get no extra credit. |

Other named parameters, each disclosed and each reported in the sensitivity
grid (not searched): `PB_MAX_BARS = 12` bars after the move's end in which the
pullback and crossing candle must complete; the VWAP/EMA hold tested on the
bar **low** (the locked reading) with a bar **close** variant in the grid; the
source's rule (ii) — *higher volume on green candles than red* — and rule (v) —
*no big topping tails* — are **not applied**, and rule (ii) appears only as a
grid variant; `TARGET_R = 2.0`; the move's retracement is measured from the
leg low (the base candle) to the leg high.

Costs: the matrix taker row — 4.5 bp fee plus 3 bp slippage each side, **15 bp
round trip on notional** — charged on both arms on entry and exit, and
converted to R at each arm's own stop distance. Costs are in the scatter, not
added after; they are reported as gross R and net R so the size of the cost
relative to each arm's R is visible.

Statistic: a **paired differential on a fixed event population**. The
population is every momentum event that produced a qualifying pullback within
`PB_MAX_BARS`; both arms are scored on **the same events**, so the pairing
removes event selection from the comparison and leaves only the entry rule.
The headline is the **pullback-minus-chase difference in realized R per event,
net of cost**, with a **day-clustered t-statistic** (events on one UTC day are
not independent; the unit is one day), plus the differential **stop distance
in bp**, which is the direct measure of his "tighter stop" mechanism. The
**hit-rate** differential is reported beside it, not in place of it. The full
distribution of each arm is reported, not just the mean. Split 70/30 by
calendar time over the events' own span; the scatter is the first 70%.

Kill: the net-R difference is **not significant (day-clustered t < 2)** on the
first 70%, **or** the last 30% has the **opposite sign**. Rule 3 wants at least
`MIN_PAIRS = 120` paired events before the split. A pass would mean the
walk-forward number has the same sign; it is not a reason to trade it.

By-liquidity buckets: the 30 pairs are split into **large / mid / small by each
pair's median dollar volume over its own frozen panel**, terciles of the 30. The
differential is reported **per bucket**, because that is the fair proxy test of
his supply/demand thesis: if the pattern only pays in illiquid names, that is
evidence **for** his mechanism (illiquid = float-constrained in his language)
and must be reported as such; if it pays equally everywhere, that argues
against the float mechanism and the illiquidity premium story. Because the
newest small pairs listed after the oldest majors, bucket is partly confounded
with calendar time; the per-pair spans are reported so that is visible.

The differential design rationale: holding the **event set** completely fixed
and varying **only the entry rule** is the tightest available test of the
source's own mechanism, and it is the mirror of Q8, which held the zone set
fixed and varied the confirmation label. If the entry rule is decoration, the
two arms' R distributions are the same and that can be said definitively. If
the mechanism is real, the pullback arm shows a **smaller stop distance for the
same 2R target** and a correspondingly better realized R even at equal or lower
hit rate. Pairing is what makes the difference attributable to the entry rather
than to which events got traded.

Status: **tested — verdict below.** The locked numbers above were recorded
before the panel was pulled and before any statistic was computed. They were
not changed after the result.

Result (Binance Vision 5m spot, 30 USDT pairs, frozen 2017-08-17 →
2026-10-14, **19,393,098 bars**, split at **2024-01-15 → 38,245 scatter
pairs / sealed holdout unopened**). Harness:
`scripts/experiments/q9_first_pullback_differential.py`; bulk fetcher
`scripts/feeds/binance_vision_bulk.py`; cache `reports/.binance_cache/`
(gitignored).

Funnel: **169,889** momentum events, **81,244** with a qualifying pullback,
**70,261** scored on **both** arms (the paired set). The pairing is the
design: every number below is on the same events for both arms.

| first 70% (38,245 paired events, 2,145 clustered days) | PULLBACK | CHASE |
|---|---|---|
| gross mean R | −0.014 | +0.248 |
| **net mean R** (15 bp round trip) | **−0.527** | **+0.114** |
| net mean R, day-clustered t | −37.7 | +8.7 |
| median net R | −1.183 | −0.078 |
| hit rate (net R > 0) | 32.6% | 47.6% |
| **median stop distance** | **44.4 bp** | **143.1 bp** |

Headline differentials (pullback − chase), first 70%:
**gross R −0.262 (t −26.4)** · **net R −0.641 (t −58.8)** ·
**hit rate −15.0 pt (t −38.4)** · **stop distance −115.3 bp (t −77.2)**.
Per-event net-R differential deciles p10/p25/p50/p75/p90 =
−2.77 / −1.38 / −0.46 / −0.06 / +1.28, sd 1.43 — the loss is a large left
tail, not a uniform drag.

**His stop mechanism is confirmed, and it is the reason the entry loses.**
The pullback stop is genuinely tighter — **median 44.4 bp vs 143.1 bp, a
ratio of 0.305**, exactly the geometry he describes. But at the same 2R
target a 3.3× tighter stop means a 3.3× closer target, and price reaches it
far less often: the pullback arm hits target 21,922/70,261 times vs 16,377 for
the chase, yet its hit rate is still only 32.6% against the chase's 47.6%,
because it also stops out 65% of the time. The result is that the **same 15 bp
round trip is ~0.51R of cost in the pullback arm and ~0.13R in the chase arm**
on the 70% (gross minus net; ~0.57R vs ~0.15R over the whole span) — the
tighter stop is not free, and here the accuracy it costs exceeds the geometry
it buys. So the differential is **not** a fee artifact: at **zero** cost the
pullback−chase gap on the 70% is still **−0.262R, t = −26.4**. The
pullback arm's own gross expectancy is essentially zero (−0.014R) while the
chase arm's is positive (+0.248R); on the whole span the pullback arm's own
gross mean is −0.017R with t = −1.75, i.e. **not** significantly negative by
itself, which is why the decisive number here is the *differential*, not the
pullback arm's standalone R.

**Kill met.** The net-R difference is −0.641R, the wrong sign, t = −58.8.
The holdout (last 30%, after 2024-01-15) was **not opened** — no holdout
statistic was computed or printed, per matrix rule 5.
`scripts/experiments/q9_first_pullback_differential.py` holds the same freeze
and will only score the sealed 30% if the scatter clears its gate, which it
does not.

Sensitivity grid, each cell re-running the whole test on the 70% (reported for
transparency, not as a search for a winning cell):

| named parameter | values | gap (net R) | clustered t |
|---|---|---|---|
| squeeze k (× ATR) | 1.0 / **1.5** / 2.0 / 2.5 | −0.662 / **−0.641** / −0.632 / −0.619 | −65.1 / **−58.8** / −54.6 / −48.9 |
| squeeze window (bars) | 2 / **3** / 4 | −0.648 / **−0.641** / −0.640 | −59.6 / **−58.8** / −58.3 |
| rvol multiple (× prior median) | 3 / **5** / 10 | −0.690 / **−0.641** / −0.586 | −74.0 / **−58.8** / −39.7 |
| retracement cap | 0.382 / **0.50** / 0.618 | −0.681 / **−0.641** / −0.613 | −56.7 / **−58.8** / −59.0 |
| pullback window (bars) | 6 / **12** / 24 | −0.641 / **−0.641** / −0.641 | −58.7 / **−58.8** / −58.8 |
| VWAP/EMA hold test | **low** / close | **−0.641** / −0.645 | **−58.8** / −61.7 |
| crossing-candle test | **bar high** / close | **−0.641** / −0.584 | **−58.8** / −56.7 |
| target (R) | 1.5 / **2.0** / 3.0 | −0.661 / **−0.641** / −0.626 | −64.8 / **−58.8** / −53.3 |
| time stop (bars) | 12 / **36** / 72 | −0.646 / **−0.641** / −0.639 | −68.3 / **−58.8** / −50.7 |
| min-R filter (× ATR) | 0 / **0.25** / 0.5 | −1.918 / **−0.641** / −0.558 | −5.8 / **−58.8** / −53.5 |
| green-volume rule (ii) | off / on | **−0.641** / −0.648 | **−58.8** / −58.9 |

**(**) = locked value.** Every cell in every row is negative and significant.
No choice of the frozen parameters flips the sign. Two cells are worth naming:
`min_r_atr = 0` (no degenerate-R filter) gives the most **negative** gap
(−1.918R) on the largest sample, because the pullback's occasional
near-zero-R stops make the cost term explode — the ifvg artifact, here
strengthening the kill rather than an escape from it. The source's own rule
(ii), green volume over red, moves the gap by 0.007R.

**By-liquidity bucket — the float-thesis test, and the headline finding.**
The differential is **strongly negative in all three buckets**, and it does
**not** improve toward the illiquid end:

| bucket (whole span) | pairs | PULLBACK net R | CHASE net R | differential | clustered t |
|---|---|---|---|---|---|
| large (10 pairs) | 23,948 | −0.605 | +0.098 | −0.703 | −54.1 |
| mid (10 pairs) | 22,318 | −0.619 | +0.096 | −0.715 | −56.2 |
| **small (10 pairs)** | 23,995 | −0.551 | +0.101 | **−0.652** | −54.7 |

The small-cap bucket's differential (−0.652R) is **marginally less negative**
than mid (−0.715R) and large (−0.703R) — about 0.05R, inside a hair of the
scatter — and the pullback arm's *hit rate* is essentially identical across
buckets (32.2% / 32.2% / 32.4%). Median stop distance scales with the bucket,
as expected (pullback 40.9 bp small vs 38.4 bp large; chase 135.3 bp small vs
123.6 bp large), but the *ratio* does not move the verdict. **His stated
mechanism — that the edge lives in float-constrained, illiquid supply — is not
reproduced: the pullback entry does not become competitive as liquidity falls.**
That is adverse to the float thesis in the one direction this data can test it.
It is **not** a refutation of the source's screen: the float pillar itself has
no crypto analog and was never tested, the newest small pairs listed after the
oldest majors so bucket is partly confounded with calendar time (per-pair spans
are in the harness output), and a crypto spot pair's supply is transparent and
elastic in a way a US small-cap float is not.

What the numbers do and do **not** license:

* They **do** license: on 30 liquid-to-small crypto pairs over 2017–2024
  (the scatter span), **chasing the extension beat waiting for the first
  pullback** on the same 169,889-event population, on both hit rate and
  realized R, and the sign is robust across every frozen parameter. Cameron's
  *entry-timing mechanism* survives as a real effect — the pullback stop is
  3.3× tighter — but here that mechanism *costs* rather than pays, because the
  tighter stop forces a tighter target that price fails to reach.
* They do **not** license: a verdict on Cameron, on Warrior Trading, or on
  the five-step method. Only the entry geometry was isolated; the universe
  screen (step 1, which he calls the most important), the Level-2 read (step
  3), and the six discretionary exits (step 4) were **not** tested. This is
  not "the strategy loses money."
* They do **not** license: a claim about US small-cap momentum, which is what
  he actually trades. A Binance USDT pair is not a low-float US equity, and
  the float pillar — the supply-side half of his screen — has no crypto
  analog and was not substituted. The test ran where he says the pattern
  fails, on liquid names, so a negative here is the expected result and is
  weak evidence against his actual edge.
* They do **not** license: a claim that the holdout agrees. It was never
  opened.

The honest exit is a **kill of the entry-geometry claim in the one setting
that could be tested**, with a clear boundary: the source's own evidence is
one unaudited account-month with no win rate or sample, and the piece of his
method that his data and ours both point at — the universe screen — cannot be
built from crypto. If this family is revisited, the order of work is a
**US-equity small-cap panel with real float and relative-volume data** (the
free Webull tier reaches 2010 on SPY/QQQ but not on low-float small caps, and
there is no free source for the float/short-interest screen this strategy
needs), the same frozen entry definitions, and only then the differential.

### Reproducing the Q9 differential

```bash
.venv/bin/python scripts/feeds/binance_vision_bulk.py           # 30 pairs, 5m, monthly+daily zips
.venv/bin/python scripts/experiments/q9_first_pullback_differential.py --skip-download
.venv/bin/python scripts/experiments/q9_first_pullback_differential.py --skip-download --grid
```

`scripts/experiments/q9_first_pullback_differential.py` loads the gitignored
panels under `reports/.binance_cache/panels/` and makes no network calls when
`--skip-download` is passed. Event detection (`candidate_moves`,
`detect_events`), the pullback rule (`_attach_pullback`), the trade simulation
(`simulate`) and the comparison (`pair_up`, `report_arms`) are separate
functions so the differential is auditable; `--grid` re-runs the whole test per
named parameter. The vectorised `candidate_moves` scan was checked against a
brute-force loop over a 60,000-bar window (405 candidates, exact match on both
the set and the chosen run length). The cache dir is gitignored
(`.gitignore`, `reports/.binance_cache/`, verified with `git check-ignore -v`).

### Q10. The Trading Channel's trend-continuation strategy — two differential tests

Source: a public YouTube video, "The Only Day Trading Strategy You Will Ever
Need (Full Tutorial: Beginner To Advanced)," **The Trading Channel**,
https://www.youtube.com/watch?v=2h5ryPi6ZYo. The video is genuinely more
rigorous than most of the sources in this file: it spends its first half on a
*disclosed* method for identifying trend, and it concedes plainly that
*"there's not really any way to make this completely objective and I know that
sucks."* It also shows *choppy* examples and says *"this is the reality of
markets, it's not what you see on YouTube when people are cherry picking
stuff."* That honesty is worth crediting.

**The evidence is still a self-report, and it is unverified.** The claimed
performance — *"wins somewhere around a little bit less than 50, somewhere
around 50 of the time and has a two to one reward to risk ratio"* — is his own
description of his own results. There is **no backtest, no sample size, no
profit factor and no audited record** anywhere in the video; it ends with a
paid mentorship pitch (EAP training program, 60-day refund), so its incentive
is to reassure, not to be precise. The ~50%/2:1 number is treated below as
**an unverified claim, not evidence**, exactly as Q5's and Q9's headline
numbers are.

The rules, as he states them:

1. **Structure — major swing levels.** An impulsive move is one that **breaks
   previous structure** (a close beyond the prior major swing level). A swing
   point becomes a **MAJOR** swing level only **after** a later impulsive move
   breaks it — a pullback low cannot be called a "major swing low" until a
   subsequent break confirms it. **Trend** = major swings making HH/HL (up) or
   LH/LL (down). A **consolidation** is signalled by a break of the *wrong*
   way (in a downtrend, a close back above the prior major swing high); he
   stands down there and says that is where the losses are.
2. **Multi-timeframe.** Trade timeframe → look **exactly one timeframe up**:
   5m→15m, 15m→1h, 1h→4h, 4h→1d, "rarely higher."
3. **Entry — three rules.** (i) HTF trend alignment; (ii) **location** — price
   pulls back into the zone of the **last major swing level that was broken**
   on the HTF (he calls them *zones, not lines*, and splits the difference
   between two nearby levels); (iii) **rejection** — on the trading timeframe,
   a **double top** (short) or **double bottom** (long), then a confirming
   candle. His double-top zone is *"between the high of the wick and the
   highest bodies of this entire little area"*; wicks or bodies may **enter**
   the zone, but a **close above it is disqualifying** (that is continuation
   through, not rejection).
4. **Execution.** Entry at the close of the confirming candle / open of the
   next. **Stop** beyond the rejection swing low/high with an **ATR(14)
   buffer** (his worked example: swing low 185.20, ATR $2.31, stop ≈ 182.89).
   **Target** = the **previous major swing high** (long) / low (short) — the
   prior structure extreme, not a fixed R.

**Two independent claims, tested separately.** Unlike Q8/Q9 this strategy
asserts two things that can fail independently, so they get two tests and
neither is allowed to rescue the other:

> **Claim A — the trend filter has value:** touches aligned with the HTF trend
> beat touches against it, on the same population.
>
> **Claim B — the rejection pattern has selectivity:** touches that form a
> valid double-top/bottom + confirming candle beat touches at the same zones
> that do not, on the same population.

Claim B is the direct analogue of Q8's confirmed/unconfirmed differential.

Who loses: **the late continuation trader** who enters *after* the level is
already broken and retested — buying the second retest of a level that has
already flipped, with a stop beyond the rejection swing that a mean-reverting
tape reaches first — **and, on the other side of the same tape, the
mean-reversion seller who fades a level that has already flipped**, so that
what used to be resistance now acts as support. If the level instead fails
after the retest, the counterparty is this bot and the row is the wrong way
round. The sign is not chosen after looking. Under matrix rule 1, the
counterparty here is *named but generic* (a class of late trend participant),
which — as Q6 conceded for the same reason — is a weaker basis for a row than
Q5's stop/margin-call counterparty.

Data: Binance Vision 5-minute spot bars, `data-api.binance.vision`, the same
30-symbol frozen universe as Q5/Q9, already resident under
`reports/.binance_cache/` (verified populated: **30 panels, 2,215 monthly
zips, ~1.3 GB, gitignored**). The HTF is **15m resampled from the same 5m
bar tape** at fixed UTC buckets, so the two clocks are guaranteed consistent;
no separate pull. 15m is the ratio he specifies for a 5m trading timeframe.
The venue choice is a caveat, as in Q9: a Binance-USDT book is a signal
instrument, not the instrument the source trades. A 1h→4h arm was declared an
optional robustness variant and **was not run**; the headline is the 5m→15m
ratio the source specifies, on one frozen span.

**Locked (frozen ex ante, before any scatter).** The source's own swing rule
is admittedly non-objective, so **every rule below is OUR operationalization**
of it, marked as such, not a disclosed number. The numbers locked here are:

| # | rule | locked value |
|---|---|---|
| 1 | **Major swing / impulsive move** | A swing high is a bar whose **high** is the maximum of a symmetric `SWING_P = 3`-bar fractal (3 lower highs each side); a swing low is the mirror on **low**. Pivots are filtered to an alternating zigzag (between two highs the higher survives). A pivot is usable only `SWING_P` bars after it (no look-ahead) and becomes **MAJOR** only when a later bar's **close** goes beyond it by at least `IMPULSE_ATR_K = 0.5 × ATR(14)` (the impulsive break); the broken level is held until a close returns through the opposite side by the same buffer. A pullback needs **≥ 2 bars and ≥ `RETRACE_MIN_ATR = 0.5 × ATR(14)`** of retracement — his "at least two candles and some movement." |
| 2 | **HTF trend** | On the 15m series, trend state = the last two **confirmed** zigzag swing highs and last two swing lows. Up = both rising; down = both falling; anything else = **consolidation → stand down** (his rule). |
| 3 | **Zone width** | The "last broken major swing level" becomes a zone `[level − 0.5·ATR, level + 0.5·ATR]` on the 15m (≈ his "zone, not a line", with the level at the centre; he splits nearby levels, which a symmetric band approximates). A **touch** is a 5m bar whose `[low, high]` intersects the band **and enters from the correct side** (from above for a long, from below for a short), recording only the first bar of each excursion. |
| 4 | **Double top/bottom window** | Two peaks (short) / troughs (long) within `DOUBLE_WIN = 12` 5m bars of each other (≈1h); the two extremes must differ by ≤ `DOUBLE_TOL_ATR = 0.5 × ATR(14)`; the rejection zone is `[max(h1,h2) − 0.5·ATR, max(h1,h2) + 0.5·ATR]` (wick-to-bodies read); any bar closing **above** the top of that zone (below the bottom for a low) **disqualifies**. A confirming candle — **red** for a short, **green** for a long — must close after the second peak, within `CONF_BARS = 6`. |
| 5 | **Execution** | Entry = **next bar's open** after the confirming candle (or after the touch, for the unconfirmed control, which stops at the zone's far edge ± the buffer). Stop = the rejection swing extreme ± `STOP_ATR_K = 1.0 × ATR(14)` (his buffer). Target = the **previous structure extreme** (the most recent confirmed zigzag swing high for a long, low for a short). `MAX_HOLD = 96` bars (8h on 5m) time stop, flat at that close. |
| 6 | **Intrabar** | **Pessimistic**, per `ifvg_gold_backtest.py`: if one 5m bar touches both stop and target in either order, the **stop** is taken. Gaps through the stop are not improved; gaps beyond the target get no extra credit. |

Other named parameters (each my operationalization, each disclosed in the
harness, none tuned to a result): `CONF_BARS = 6` for the confirming candle;
`RETRACE_LOOKBACK = 24` bars for the move that pulled back; the target is kept
only when it lies beyond the entry (R > 0), so a trade without one is dropped
from the realized-R differential and still counted in the geometry-free bp
measure; `MIN_R_BP = 0` (no degenerate-R filter — the locked value); the HTF
layer is read on the last **closed** 15m bar. The geometry-free bp measure uses
a fixed 24-bar horizon (`H_BP = 24`), cost subtracted.

Costs: the matrix taker row — 4.5 bp fee plus 3 bp slippage each side, **15 bp
round trip on notional** — charged on both arms of both differentials, and
converted to R at each trade's own stop distance. Costs are in the scatter,
not added after. Both **hit rate** and **realized R** are reported, because
the source's claim is specifically *~50% at 2:1*, and a design that reported
only one of those would misstate it (a 2:1 payoff passes on hit rate at ~33%,
but the *claimed* 2:1 must be checked by the **realized R:R the target
geometry actually produces**, which is not 2:1 unless the target is exactly
2R — and here the target is the prior structure extreme, so R:R is an
**outcome**, not a setting, and is reported as a distribution).

Statistic: a **differential on a fixed event population**. The population is
every qualifying zone touch (the data-engineering funnel is reported, so the
population is visible). **Claim A** splits each touch by HTF alignment
(with-trend vs counter-trend); **Claim B** splits each touch by pattern
outcome (confirmed vs unconfirmed). The headline is the **difference in
realized R per touch, net of cost**, with a **day-clustered t-statistic**
(touches inside one UTC day are not independent; the unit is one day), plus
the **hit-rate** differential beside it. The full distribution of each arm is
reported, not just the mean. Split 70/30 by calendar time over the events'
own span; the scatter is the first 70%.

Kill: applied **separately to Claim A and Claim B**. Each fails if its
realized-R differential is **not significant (day-clustered t < 2)** on the
first 70%, **or** the last 30% has the opposite sign. A pass on one claim does
**not** license the other. Rule 3 wants at least `MIN_EVENTS = 120` touches in
the scatter before the split.

The differential rationale: holding the **touch population** completely fixed
and varying **only one label at a time** — alignment for Claim A, pattern
outcome for Claim B — is the tightest available test of each claim, and it is
the same design as Q8 (fixed zone set, varied confirmation) and Q9 (fixed
event set, varied entry). If the trend filter is decoration, aligned and
counter-aligned touches have the same R distribution and that can be said
definitively; if it selects, a gap appears. Claim B is *not* a test of the
whole method, and a positive Claim B does not make Claim A true — the two
gaps are quoted separately and neither rescues the other.

Status: **tested — both claims fail their kill rules; no claim licenses
anything.** The locked numbers above were recorded before the panel was
loaded and before any statistic was computed, and they were not changed after
the result. Two implementation fixes are disclosed below; no locked number moved.

Result (Binance Vision 5m spot, 30 USDT pairs, frozen 2017-08-17 →
2026-10-14, **19,393,098 bars**; HTF = 15m resampled from the same tape;
split at **2024-01-15 → 2,367 scatter touches / sealed holdout unopened**).
Harness: `scripts/experiments/q10_trend_continuation_differential.py`; cache
`reports/.binance_cache/` (gitignored).

Funnel (19,393,098 5m bars): the broken-major-level zone produces raw
first-touch excursions of **10,835 long / 9,497 short**, of which **4,540**
survive the structure-state, pullback (≥2 candles and ≥0.5×ATR15), and
target-existence filters — **3,236 long / 1,304 short**, **2,943 with-trend /
1,597 counter-trend** (Claim A), **2,768 confirmed / 1,772 unconfirmed**
(Claim B), **4,533** with a simulated trade. The long/short imbalance is the
expected consequence of a 2017-2026 crypto panel whose majors trended up for
most of it: broken prior highs are retested more often than broken prior lows.

The two differentials are both **not significant**, and **neither has the sign
the strategy needs across all four measures**:

| first 70% (2,367 touches, 1,087 clustered days, through 2024-01-15) | Claim A gap (with − counter) | Claim B gap (confirmed − unconfirmed) |
|---|---|---|
| **realized net R** (headline) | **−0.245R**, day-clustered **t = −1.30** | **+0.337R**, day-clustered **t = +1.22** |
| gross R | −0.048R, t = −0.78 | +0.162R, t = **+2.91** |
| hit-rate differential | −4.1 pt, t = −1.62 | +2.0 pt, t = +0.87 |
| fixed-horizon bp (geometry-free) | −8.13 bp, t = −0.94 | −2.10 bp, t = −0.27 |

Per-arm, first 70%:

| arm | n (scatter) | mean net R | median net R | hit rate |
|---|---|---|---|---|
| **with-trend** | 1,495 | −0.346 | −0.092 | 45.3% |
| **counter-trend** | 865 | −0.101 | −0.008 | 49.4% |
| **confirmed** (his entry) | 1,380 | −0.116 | −0.071 | 47.6% |
| **unconfirmed** (control) | 980 | −0.453 | −0.054 | 45.6% |

**Claim A fails, and it fails in the direction opposite to the claim.** Touches
taken *with* the higher-timeframe trend are **worse** than touches taken against
it, on the headline realized-R measure (−0.245R), on the hit-rate measure
(−4.1 pt), and on the geometry-free fixed-horizon measure (−8.13 bp). None is
near significance (largest |t| = 1.62, on hit rate), so the correct statement is
**no detectable value and a consistently adverse point estimate**, not a
significant reversal. Because Claim A's two arms mix the confirmed and
unconfirmed trade geometries, the same gap is shown **inside each Claim-B
subset**: inside the confirmed subset it is −0.040R (t −0.46), inside the
unconfirmed subset −0.534R (t −1.23), and the geometry-free bp gap is negative
in both (−2.5 bp and −16.0 bp). So the adverse sign is not an artifact of mixing
stop geometries — it appears within each geometry separately. The trend filter,
as operationalized here, **does not separate winners from losers; its point
estimate is faintly adverse.**

**Claim B is the mirror image and the more interesting failure.** The
confirmed double-top/bottom entry is *better on the headline realized R* than
its own control (+0.337R) and better on hit rate (+2.0 pt), but **worse on the
geometry-free fixed-horizon bp measure** (−2.10 bp). That split is precisely why
both measures are reported. The realized-R advantage is largely a
**stop-geometry effect**: the confirmed arm's rejection stop is tighter (median
R 71.2 bp vs the control's 90.5 bp), so the same 15 bp round trip costs it less
in price terms, and on the first 70% its **gross** gap is +0.162R — most of the
net +0.337R comes from the cost term, not from accuracy (the identity
net = gross − cost-in-R holds exactly: +0.337 = +0.162 − (−0.175)). The
horizon measure, which has no stop geometry, finds the **opposite (negative)
sign**. Neither realized-R nor hit-rate is significant (t = +1.22, +0.87).

**And the Claim-B point estimate is fragile to degenerate-R control trades.**
The unconfirmed control has a small tail of trades whose stop is very tight
relative to the entry (a control trade stops at the zone edge ± 1×ATR5, which
can be only a few bp away when the entry sits near the zone), and for those the
15 bp round trip is a large multiple of R. Excluding trades whose R is thinner
than 20 bp — a filter applied **identically to both arms**, Q9's convention —
collapses the Claim-B net-R gap from **+0.337R to +0.094R** (grid row `min-R
filter` below). So the entire apparent Claim-B advantage is carried by a
handful of degenerate control trades and is **not a robust selectivity effect**.
That is the decisive reading: **Claim B shows no selectivity.** The
double-top/bottom + confirming candle marks entries whose outcome is
statistically indistinguishable from the same zones touched without it, and the
one measure that looks favourable does not survive a neutral min-R filter.

| Claim | first-70% gate | verdict |
|---|---|---|
| **A — trend filter adds value** | realized-R gap negative, t −1.30 | **FAILS → holdout SEALED** |
| **B — rejection adds selectivity** | realized-R gap positive but t +1.22 < 2; geometry-free gap negative; positive point estimate collapses under a min-R filter | **FAILS → holdout SEALED** |

Both holdouts were **left sealed**. Neither claim's first 70% cleared its kill
rule (each needs day-clustered |t| ≥ 2 *and* the correct sign), so the last 30%
of calendar time — after 2024-01-15 — was **not opened** for either claim, and
no holdout statistic was computed or printed, per matrix rule 5.

**The source's ~50% / 2:1 claim, checked on the first 70%.** His hit-rate
ballpark is roughly reproduced; his payoff ratio is not:

| | his claim | confirmed arm (his entry) | unconfirmed control |
|---|---|---|---|
| hit rate | "~50%" | **47.6%** | 45.6% |
| realized R:R (target/risk) | "2 to 1" | **median 1.34, mean 1.76** | median 0.83 |
| average win / average loss (R) | (≈2:1) | **+1.07 / −1.19** | +0.72 / −1.43 |

The confirmed arm's **realized R:R is 1.34 at the median, not 2.00** — the
target is the prior structure extreme, not a fixed 2R, so the payoff he
describes is an *outcome* of where that extreme sits, and on this data it sits
closer than 2R about half the time (the all-trade R:R distribution has median
1.11 and p10/25/50/75/90 = 0.20/0.51/1.11/1.98/3.05). Even granting his
~48% hit rate, **the realized R:R is below 2:1, so the combination he claims —
~50% at 2:1 — is not what this data produces**; a ~48% win rate at a realized
1.34:1 is a net loser, which is what the arm's mean net R (−0.116) shows. The
hit-rate piece is the closest thing to a partial reproduction in the row, and
it still fails his own arithmetic.

**Two implementation notes, disclosed.** (1) A first smoke run on BTC alone
showed an absurd 3,232-long / 45-short split; the mirror for broken **swing
lows** had activation and invalidation as the same inequality, so the short side
was almost always already dead. The two conditions are now the correct
opposites (a low is broken by a close *below* it, invalidated by a close above
it), and the short side works (**3,236 long / 1,304 short**). (2) The pattern
scanner's peak search excluded its last eligible bar, and the target price could
see a swing pivot that had not yet been confirmed at the entry bar; both were
tightened (peaks now searched over the full window; targets use only
confirmed pivots). **No locked number was changed** — the frozen specification
in the section above is exactly what produced the result — and these were
corrections before the frozen run was recorded, not post-hoc parameter changes.

Sensitivity grid, each cell re-running the whole test on the 70% (reported for
transparency, not as a search for a winning cell). Every cell's gate requires
t ≥ 2:

| named parameter | values | A gap (R) / t | B gap (R) / t |
|---|---|---|---|
| swing fractal p | 2 / **3** / 4 | −0.078/−1.64 / **−0.245/−1.30** / +0.070/+0.91 | +0.009/+0.20 / **+0.337/+1.22** / −0.044/−0.61 |
| impulse k (× ATR) | 0.25 / **0.5** / 1.0 | −0.578/−0.92 / **−0.245/−1.30** / −0.132/−2.32 | +0.996/+1.09 / **+0.337/+1.22** / +0.116/+1.35 |
| zone half-width (× ATR) | 0.25 / **0.5** / 1.0 | −0.164/−2.33 / **−0.245/−1.30** / −0.056/−0.77 | +0.181/+2.29 / **+0.337/+1.22** / −0.010/−0.17 |
| double window (bars) | 6 / **12** / 24 | −0.210/−1.03 / **−0.245/−1.30** / −0.217/−1.16 | +0.073/+0.32 / **+0.337/+1.22** / +0.576/+1.83 |
| double tol (× ATR) | 0.25 / **0.5** / 1.0 | −0.270/−1.31 / **−0.245/−1.30** / −0.137/−0.68 | +0.291/+1.28 / **+0.337/+1.22** / +0.503/+1.21 |
| stop buffer (× ATR) | 0.5 / **1.0** / 1.5 | −0.042/−0.54 / **−0.245/−1.30** / −0.014/−0.22 | +0.061/+0.88 / **+0.337/+1.22** / +0.082/+1.37 |
| time stop (bars) | 48 / **96** / 192 | −0.241/−1.28 / **−0.245/−1.30** / −0.246/−1.30 | +0.320/+1.16 / **+0.337/+1.22** / +0.351/+1.28 |
| bp horizon (bars) | 12 / **24** / 96 | (R unaffected) **−0.245/−1.30** | **+0.337/+1.22** |
| retrace min (× ATR) | 0.25 / **0.5** / 1.0 | −0.254/−1.36 / **−0.245/−1.30** / −0.296/−1.31 | +0.318/+1.17 / **+0.337/+1.22** / +0.467/+1.34 |
| confirm window (bars) | 3 / **6** / 12 | −0.252/−1.32 / **−0.245/−1.30** / −0.248/−1.31 | +0.384/+1.46 / **+0.337/+1.22** / +0.327/+1.17 |
| min-R filter (bp) | **0** / 20 / 50 | **−0.245/−1.30** / −0.053/−0.85 / −0.145/−2.14 | **+0.337/+1.22** / +0.094/+1.67 / +0.094/+1.54 |

**(**) = locked value.** **No Claim-B cell reaches the gate**, and the largest
Claim-A magnitudes are the *wrong sign*. Every Claim-B cell is ≤ +0.996R and
|t| ≤ 1.83; the largest is `impulse_k=0.25` (+0.996R, t +1.09 on the smallest
sample, 701 touches) and the largest on the locked neighbourhood is
`double_window=24` (+0.576R, t +1.83) — neither significant, and both sit
outside a central band of ~+0.3R. Two Claim-A cells reach |t| ≥ 2, and **both
are adverse**: `impulse_k=1.0` (−0.132R, t −2.32 on 8,730 touches) and
`zone_half=0.25` (−0.164R, t −2.33). No positive Claim-A cell clears +0.070R
and none has t > 1. So on this grid the trend filter is either noise or, on two
larger-sample cells, **significantly worse than its own control**; there is no
cell in which it helps.

The consistency that matters is the **geometry-free bp gap**, which is negative
in every row and every cell of both claims (headline −8.13 bp for A, −2.10 bp
for B). It is the only measure that does not partly *contain* the stop geometry,
and it never favours either ruler. The one Claim-B cell that looks like a
finding — `min-R filter = 0` shows +0.337R — is the *locked* value, and it
**collapses to +0.094R** as soon as degenerate-R trades are trimmed, which is
why the fragility is reported in the headline rather than buried here.

What the numbers do and do **not** license:

* They **do** license: on 30 liquid-to-small crypto pairs over 2017-2024 (the
  scatter span), with every rule the source leaves loose operationalized as
  stated above, **the HTF trend filter does not add value** (Claim A fails; its
  point estimate is adverse on all four measures, and two larger-sample grid
  cells are *significantly* adverse), and **the double-top/bottom rejection step
  shows no selectivity** (Claim B fails; its realized-R point estimate is a
  stop-geometry artifact that does not survive a min-R filter, and the
  geometry-free measure is negative). The two claims are independent and **both
  fail**; neither result rescues the other.
* They **do** license: an arithmetic check of his self-report that is partly
  favourable and decisive where it counts. His hit-rate ballpark (~50%) is
  roughly in range (~48%), but the **realized R:R is ~1.34:1, not 2:1**, so
  **~50% at 2:1 is not reproduced** — and even the ~48% at a realized 1.34:1 is
  a net loser.
* They do **not** license: a verdict on The Trading Channel, on the EAP
  programme, or on the method as a whole. His trend definition is admittedly
  non-objective and every rule here is *our* operationalization of it; a
  different reading of "breaks previous structure" or "the last major swing
  level" is a different test, and a passing one is not excluded by this row.
* They do **not** license: a claim about a discretionary trader's own results.
  The video trades an unspecified instrument on an unspecified timeframe with
  discretionary level selection and split-nearby-levels judgement calls; the
  test runs a mechanical proxy on crypto spot at 5m→15m. A discretionary human
  can decline trades the rule takes and hold winners the rule exits.
* They do **not** license: a claim that the holdout agrees. It was never
  opened, for either claim.
* They do **not** license: the 1h→4h arm, which was declared optional and was
  **not run**. This row tested one ratio on one span.

The honest exit is a **kill of both claims in the one setting that was
testable**, with a clear boundary. The trend filter is the piece the source
spends most of the video *disclosing*, and it is the piece that fails most
cleanly (adverse point estimate on all four measures, and two larger-sample grid
cells significantly adverse). The rejection pattern is a coin-flip on realized
R once its stop-geometry artifact is removed, and negative on the horizon
measure. If this family is revisited, the order of work is a **1h→4h arm**
(declared but not run), and a **different objective reading of "break of
structure"** — written as a **new row**, with its own frozen numbers, before it
is run. Adding one now, after seeing these numbers, is the overfit the matrix
refuses.

### Reproducing the Q10 differential

```bash
.venv/bin/python scripts/experiments/q10_trend_continuation_differential.py --skip-download
.venv/bin/python scripts/experiments/q10_trend_continuation_differential.py --skip-download --grid
```

`scripts/experiments/q10_trend_continuation_differential.py` loads the
gitignored panels under `reports/.binance_cache/panels/` and makes no network
calls when `--skip-download` is passed. Structure detection (`zigzag`,
`build_htf` → the last broken major level and the trend state), zone building
and touch detection (`build_touches`), pattern confirmation (`confirm_pattern`),
trade simulation (`simulate`) and the two differentials (`differential`,
`cluster_t`) are separate functions so the logic is auditable; `--grid`
re-runs the whole test per named parameter. The unit tests in
`tests/test_q10_trend_continuation_differential.py` run on synthetic bars and
pin the structure arithmetic, the double-top confirmation (including the
close-above disqualifier), the pessimistic intrabar convention and the
day-clustered statistic. The cache dir is gitignored (`git check-ignore -v`
verified).

### Q11. Oliver Velez's "whale" — the failed bounce after a power bar down, and the zone rule

Source: a public YouTube video, "THE STRATEGY BEHIND MY BIGGEST DAY TRADING
PROFITS," **Oliver Velez Trading**, https://www.youtube.com/watch?v=KmO2rUG6mIk.
He calls the pattern one of his "four bread and butter concepts", *"responsible
for more daily profits than almost everything else combined."* The core
pattern, which he repeats three times in the same words: **"Power bar down,
bounce nothing to be afraid of, and secondary move to a new low."**

**The video contains no statistical evidence.** It is narrated worked examples
(Meta, Amazon, Apple) with no win rate, no sample size, no profit factor and no
verified track record. He explicitly concedes that the amount of red needed to
confirm weakness is a **judgement call** ("comparable", "solid", "powerful") and
that SMA slope is **"judgemental"** — *"flatish"* versus *"like a 45° angle"* —
and he **repeatedly declines to make either objective** ("let's not be too
strict with this"). The video ends with a lead magnet ("The Market's Alphabet"
free PDF plus a bonus class), so its incentive is engagement, not accuracy.
Nothing below treats the video as evidence.

**Two substitutions are material and must be read before the numbers.** They are
not a wash, and they bound what this row can license:

* **The instrument and the gap.** His instrument is a gapping single stock on a
  **2-minute** chart, and his **entire trigger depends on an opening gap up into
  a round number** that then gets painted over. Crypto spot trades 24/7 and
  **does not gap** the way a single stock does. There is no clean crypto analog
  of "gapped up into the ceiling", so the gap half of the setup is **not
  reproduced**; what is reproduced is the mechanical part that does not need a
  gap — *power bar down → bounce → secondary move to a new low*, with the
  "ceiling" proxy being the high of the down-move itself. A test of the gap
  mechanic is out of reach with this data.
* **The round-number mechanic.** A $322 stock has a psychological whole-dollar
  grid; a 24/7 crypto pair with a float price does not have a convention of the
  same salience. The mechanical grid below is **ours**, stated as such.

The 2-minute chart is a floor-timeframe convention, so the natural proxy is
**5m**, which is what this row uses; the optional 1m arm is declared below.

The rules as stated, and what this row does with each:

1. **Context** — 20 and 200 SMA state (position and slope), explicitly
   judgemental. Read here as a mechanical **zone class** (below) and, in the
   sensitivity grid, as a **slope** filter. No slope number is locked: his is
   not a number.
2. **The zone rule** — Zone 1 (below/at the MAs) is the one he **refuses** to
   short; Zone 2 (above) and Zone 3 (significantly higher) are shortable.
3. **Round numbers** — mechanical grid, ours.
4. **The trigger** — after the down-move, a bounce, then a red that "paints
   over" the green; enter short on that red.
5. **Stop** — above the high of the move, capped ("I don't want more than a 75
   cent stop out").
6. **"Enough red"** — his operational test is to compare the new red with *"the
   last several big reds of the prior day"*. Operationalized below.
7. **Target / management** — take profit near the next round number below, then
   **re-short the bounce**; riding drop-bounce-drop is the "whale".
8. **Sizes** — super micro (inside one bar), micro, macro.

**Two separable claims, tested independently; neither rescues the other.**

> **Claim A — the bounce fails (the dead-cat mechanism).** After a powerful
> down-move the bounce does *not* recover, so shorting the failed bounce beats
> shorting the power bar directly, on the same events. This is a
> mean-reversion-after-impulse hypothesis.
>
> **Claim B — the zone rule matters.** The same setup performs *worse* when the
> event is in Zone 1 (below the MAs) than in Zone 2/3, which is what validates
> *"never short in zone 1"*.

Who loses: **the dip-buyer / mean-reverter who buys the bounce expecting a full
recovery after a vertical drop** and is left holding as price rolls to a new
low — plus **the breakout buyer who chased the extension** of the preceding
up-leg and is now trapped in the same drop. This is the same counterparty class
as Q5 (a stop or a margin call that has to sell into the move), named rather
than generic, which is the standard matrix rule 1 asks for. If the bounce
instead recovers and price makes a new high, the counterparty is this bot and
the row is the wrong way round; the sign is not chosen after looking.

Data: Binance Vision 5-minute spot bars, `data-api.binance.vision`, the same
**frozen 30-pair Q9/Q10 universe**, already resident under
`reports/.binance_cache/` (verified populated: 30 panels, 2,215 monthly zips,
~1.3 GB, gitignored). No new pair is added. 20/200 SMA and ATR(14) are computed
**on the 5m series itself**, per his "read the MAs on the trading timeframe"
convention; 5m is the proxy for his 2m floor. The venue is a caveat, as in
Q9/Q10: a Binance-USDT book is a signal instrument, not the instrument the
source trades.

**Locked (frozen ex ante, before any scatter; each is OUR operationalization
because the source leaves all of these loose, and each is disclosed as ours):**

| # | rule | locked value |
|---|---|---|
| 1 | **Power drop** ("power bar down") | a run of at most `POWER_MAX_BARS = 3` consecutive 5m bars, **all red**, whose net move `open[first] − close[last]` is at least `POWER_ATR_K = 1.5 × ATR(14)` measured at the bar *before* the run, **and** whose final close sits in the bottom `POWER_CLOSE_Q = 0.25` of the run's own range (`min(low)..max(high)`, the "close in the bottom q of its range" quality test). Direct mirror of the Q9 displacement definition, with the sign flipped and the close-location test added; his bar is a single "power bar", a 1–3 bar run is the operational tolerance for a 5m proxy. |
| 2 | **"Enough red"** | the run's **widest** bar range must be ≥ `RED_SIZE_C = 1.0` × the **median range of the red bars among the prior `RED_REF = 288` 5m bars** (one UTC day). 1.0× the prior day's typical red is the mechanical reading of "comparable to the last several big reds of the prior day"; `RED_SIZE_C` and the reference window are the two dials and both are named. |
| 3 | **Bounce-failure entry** | the bounce = the **first green bar** after the run ends, occurring within `BOUNCE_MAX_BARS = 6` bars. **Locked (conservative)**: a sell-stop at `bounce_low − ENTRY_TOL_ATR × ATR` (`ENTRY_TOL_ATR = 0.05`), his *"short right under the green"*; it fills on the first later bar whose low reaches the trigger, within `SETUP_MAX_BARS = 12` bars, and fills **at the trigger price, never better** (pessimistic). The **aggressive** variant, his *"cut the green bar in half"*, is a sell-limit at the **midpoint of the bounce bar's range** — a **labelled sensitivity arm**, not a second look. |
| 4 | **Zone** | at the bar before the run: **Zone 1** = close ≤ SMA20 (below/at the faster MA — the zone he refuses); **Zone 2/3** = close > SMA20; **Zone 3** = close > SMA20 + `ZONE3_ATR = 1.0` × ATR(14), reported as context only. His rule is stated on the **session open**; crypto has no session open, so the *current price* at the event is the analog. That substitution is ours and is disclosed. |
| 5 | **Stop** | the high of the move (the highest high from the run start through the entry bar) + `STOP_ATR_K = 0.5` × ATR(14), his "above the high of the day" with his ATR buffer convention (as Q10). **Max-stop cap**: trades whose stop distance exceeds `MAX_STOP_ATR = 3.0` × ATR(14) are dropped — the mechanical stand-in for his "I don't want more than a 75 cent stop out". Applied **identically to both arms**. |
| 6 | **Target** | the next level of a **mechanical grid of spacing `GRID_ATR = 1.0` × ATR(14)** strictly below the entry (`floor(entry / grid) × grid`), his "next round number below"; plus a time stop of `MAX_HOLD = 96` bars (8 h) and flat at that close. |
| 7 | **Intrabar** | **PESSIMISTIC**, as `ifvg_gold_backtest.py` documents: if one 5m bar touches both stop and target in either order, the **stop** is taken. Gaps through the stop are not improved; gaps beyond the target get no extra credit. |

Other named parameters (each ours, each disclosed, none tuned to a result):
`ATR_LEN = 14`; `COST_BP = 15.0` (the matrix taker row — 4.5 bp fee + 3 bp
slippage each side); `H_BP = 24` bars for the geometry-free bp measure;
`SCATTER_FRAC = 0.70`; `MIN_EVENTS = 120` (rule 3 floor, on **paired** events).
The control arm is **not** a free choice to be tuned: it is fixed as his
naive alternative — **short at the open of the bar after the power run ends**
(the continuation short), with the **same stop rule, the same grid target and
the same time stop**, so only the entry timing differs.

Statistic: a **differential on a fixed event population**. The population is
every power-drop event that clears the enough-red filter and produces **both** a
control entry and a bounce-failure entry; both arms are scored on **the same
events**, so pairing removes event selection from the comparison and leaves only
the entry. For **Claim A** the headline is the **bounce-minus-control difference
in realized R per event, net of cost**, with a **day-clustered t-statistic**
(events on one UTC day are not independent; the unit is one day); the
**hit-rate** differential and the **geometry-free fixed-horizon bp** differential
(`H_BP = 24` bars from each arm's own entry, cost subtracted — the measure that
does not partly *contain* the stop geometry, Q10's house standard) are reported
beside it. For **Claim B** the same three numbers are computed as
**Zone 2/3 minus Zone 1** on the bounce arm; his rule predicts a **positive**
gap. Also reported, geometry-free and independent of any entry: the
**bounce-recovery distribution** — the fraction of bounces that retrace ≥ 50% and
≥ 100% of the drop within `RECOVERY_WIN = 96` bars, measured as
`(max high after the run − run low) / (run high − run low)`. Split 70/30 by
calendar time over the events' own span; the scatter is the first 70%.

Kill: applied **separately to Claim A and Claim B**. Each fails if its
realized-R differential is **not significant (day-clustered t < 2) with the
sign its claim needs** on the first 70%, **or** the last 30% has the
**opposite sign**. A pass on one claim does **not** license the other. Wants at
least `MIN_EVENTS = 120` paired events in the scatter before the split.

The differential rationale: holding the **event set** completely fixed and
varying **only the entry rule** (Claim A), and holding the **event set and the
entry rule both fixed** and varying **only the zone label** (Claim B), is the
tightest available test of each claim. This is the same design as Q8 (fixed zone
set, varied confirmation) and Q9 (fixed event set, varied entry). If the bounce
adds nothing, the two arms' R distributions are the same and that can be said
definitively; if his zone rule is real, Zone 1 is materially worse and the gap
is positive. Claim B is *not* a test of the whole method, and a positive Claim A
does not make Claim B true.

**Declared optional arms, and whether they were run:**

* **Round-number check.** Whether proximity to a mechanical grid (the locked
  `1.0 × ATR` grid versus a finer `0.1 × ATR` grid — a finer grid is the nearest
  available stand-in for "less round") changes the bounce-failure odds. Cheap
  on the same panel; labelled; **not** part of either kill rule.
* **The intrabar "super micro whale" (1m).** He claims a whale can occur
  **inside a single bar**. That is unobservable at 5m, and 1m zips exist. The
  1m arm is declared here with **time-equivalent** constants (`POWER_MAX_BARS`
  15, `MAX_HOLD` 480, `H_BP` 120, `RED_REF` 1440 = one day), on a **fixed
  subset — BTCUSDT and ETHUSDT only, 2024-01 → 2026-09** — because a 1m pull of
  the full 30-pair universe over 2017–2026 is disproportionate to a secondary
  check. It is a **confirmation, not the headline**, and it is reported with
  the 5m result, never instead of it.

Status: **tested — both claims fail; no claim licenses anything.** The locked
numbers above were recorded before the panel was loaded and before any statistic
was computed, and they were not changed after the result. One material
implementation bug was found and fixed before the frozen run was recorded (it is
disclosed below, with the direction of its effect); no locked number moved.

Result (Binance Vision 5m spot, 30 USDT pairs, frozen 2017-08-17 → 2026-10-14,
**19,393,098 bars**; 20/200 SMA and ATR(14) on the 5m tape itself; split at
**2024-01-14 → 84,699 scatter events / holdout per claim**). Harness:
`scripts/experiments/q11_velez_whale_differential.py`; cache
`reports/.binance_cache/` (gitignored). Unit tests:
`tests/test_q11_velez_whale_differential.py` (17 tests, synthetic bars only).

Funnel (19,393,098 bars): **1,459,161** raw vectorised power-run candidates →
**60,863** dropped by the "enough red" size rule → **915,990** qualifying
power-drop events → of those, **17,154** produced no green bounce inside the
window, **300,527** never triggered the entry stop, **439,366** were dropped by
the max-stop cap or a degenerate grid target → **158,943** with a bounce entry;
**555,861** had a valid control entry (of which **360,129** dropped by the cap).
**158,558 events carry both arms** and are the frozen population. On the first
70% (**through 2024-01-14**) that is **84,699 paired events / 2,340 clustered
UTC days**.

**Claim A fails, and it fails in the direction *opposite* to the claim.** The
"short right under the green" bounce entry is **worse** than the naive
continuation short on every measure, and significantly so:

| first 70% (84,699 paired events, 2,340 clustered days, through 2024-01-14) | BOUNCE (his entry) | CONTROL (naive short at the run-end open) | differential (bounce − control) |
|---|---|---|---|
| **realized net R** (headline) | **−0.221R** | −0.136R | **−0.085R, day-clustered t = −59.22** |
| gross R | −0.015R | +0.093R | −0.109R, t = −78.55 |
| hit rate | **40.5%** | 44.0% | **−3.5 pt, t = −18.37** |
| **fixed-horizon bp** (geometry-free) | −16.61 bp | −6.45 bp | **−10.16 bp, t = −26.34** |

His entry loses money outright (−0.221R net per event) *and* loses to the
control it was supposed to beat. The geometry-free fixed-horizon measure — the
one that does not partly contain the stop geometry — agrees with realized R in
sign and is highly significant (−10.16 bp, t −26.34), so the adverse result is
**not** a stop-geometry artifact. In fact the small geometry difference runs
*for* the bounce arm, not against it: its median stop is wider (92.1 bp vs the
control's 83.4 bp), so the fixed 15 bp round trip costs it marginally *less* in R
(≈0.163R vs ≈0.180R, a ≈+0.017R advantage in the cost term). That means the
**gross** gap is even more negative than the net (**−0.109R** gross vs −0.085R
net; the ±0.017R cost term accounts for most of the difference between them). So
neither measure, and neither the gross move nor the cost arithmetic, leaves the
bounce entry anything to stand on. **The bounce entry does not beat the control;
it is worse, decisively.**

**Claim B is a nominal first-70% pass that dies in its own holdout.** Partitioned
by zone on the *same* bounce events:

| first 70% (84,699 events; Zone 2/3 = 56,330, Zone 1 = 28,369) | Zone 2/3 | Zone 1 | differential (Z2/3 − Z1) |
|---|---|---|---|
| **realized net R** | −0.214R | −0.234R | **+0.020R, day-clustered t = +4.70** |
| hit rate | 40.5% | 40.4% | +0.1 pt, t = +0.22 |
| **fixed-horizon bp** (geometry-free) | −14.56 bp | −20.68 bp | **+6.12 bp, t = +3.17** |

The scatter gap has the **sign his rule needs** (Zone 2/3 less bad than Zone 1)
and clears the prong-1 gate (t ≥ +2). But **both zone arms lose money net of
cost on the first 70%** (−0.214R and −0.234R), so even the "good" zone is not a
profitable short — the gap is a difference between two losing arms
(0.020R on a −0.22R base), not an edge. Opening the holdout once for prong 2:
on the last 30% (**through 2026-10-14**, 73,859 events / 1,004 clustered days)
the gap **reverses sign**:

| Claim B holdout (last 30%, 73,859 events) | Zone 2/3 | Zone 1 | differential |
|---|---|---|---|
| realized net R | −0.234R | −0.231R | **−0.003R, t = −0.54** |
| hit rate | 37.0% | 38.3% | −1.3 pt, t = −2.96 |
| fixed-horizon bp | −14.37 bp | −17.43 bp | +3.06 bp, t = +1.29 |

The scatter sign was positive (+0.020R); the holdout is negative (−0.003R). By
the kill rule's second prong (**opposite sign in the last 30%**) **Claim B
fails**, and the nominal 70% result does not carry into the last 30% of calendar
time. The zone arms are statistically indistinguishable in the holdout, exactly
as they would be if his zone rule is not real.

| Claim | gate | verdict |
|---|---|---|
| **A — the bounce fails (short the failed bounce beats the control)** | realized-R gap **−0.085R, t −59.22** (wrong sign); bp −10.16, t −26.34 | **FAILS prong 1 → holdout SEALED** |
| **B — the zone rule (Zone 2/3 beats Zone 1)** | scatter +0.020R, t +4.70 (right sign) but holdout −0.003R (opposite sign) | **FAILS prong 2 → holdout opened once, reversed** |

**The bounce-recovery distribution — his most direct, geometry-free claim.** He
says the bounce will not give the drop back. It usually does:

| fraction of the DROP retraced within 96 bars (first 70%, n = 84,699) | median | ≥50% | ≥100% (drop fully undone) |
|---|---|---|---|
| back to the run's *own* high only (24-bar window) | 0.975 | **86.9%** | **49.0%** |
| drop = run low … run high | 1.939 | 93.9% | 75.1% |
| drop = run low … prior 96-bar high (strictest) | 0.718 | 64.2% | 37.5% |

Against his literal clause "secondary move to a new low", a post-drop new low
*before the holding window closes* **does** occur in **98.4%** of events — but
that is exactly the selection trap below: a power drop almost always gets a
lower low eventually at 5m, so "it makes a new low" is not evidence of a
shortable *bounce failure*. Meanwhile the bounce **halves the drop 87% of the
time and fully undoes it 49% of the time** — the rubber ball bounces back about
as often as it doesn't.

**Selection diagnostic (disclosed, not a kill rule).** The event population is
only defined when a bounce entry *fills*, and a sell-stop below the bounce bar
fills only if price falls through it — so the population is partly selected *for*
continuation. Splitting on whether the new low arrived **before** the bounce bar:
17.4% of events had the new low **before** the entry could exist; 82.6% only
at/after. A separate, fully unconditional read — a short held from the run-end
close to the fixed horizon with **no entry, no stop, on every qualifying event**
— returns **−8.72 bp, t −7.31**: the power drop, on its own, **mean-reverts**
(net of the 15 bp round trip), it does not continue. So the "bounce fails"
mechanism has no unconditional support in this data either; the drop's own
forward drift is *against* a short.

**Round-number check (optional, OUR grid, not his).** Proximity to the locked
`1.0 × ATR` grid makes no material difference: the new-low rate is 98.3% near vs
98.4% far, the recovery fraction is 1.296 vs 1.292 (gap +0.003, t +0.38), and the
bounce-arm net-R gap is +0.014R (t +3.61 — nominally significant but tiny and on
a losing arm). The finer `0.1 × ATR` grid had insufficient split (the candidate
prices are almost never within 0.15 ATR of a 0.1-ATR level on a float price, which
is itself the point: crypto has no psychological round grid of the kind a $322
stock does). **The round-number mechanic is not reproducible and no support for
it is claimed.**

**1m secondary arm (declared subset, the intrabar "super micro whale").** On
BTCUSDT + ETHUSDT, 2024-01 → 2026-09 (2,891,520 1m bars, time-equivalent
constants), **both claims fail prong 1**: Claim A realized-R gap −0.005R
(t −1.79, right sign for a kill but short of the gate), bp −1.61 bp (t −14.27);
Claim B +/− not significant (−0.006R, t −0.41). At 1m the median stop collapses
to ~19 bp, so the fixed 15 bp round trip is ~80% of R and both arms are
catastrophically net negative (**−0.97R** mean, ~2.5% win) — the 1m arm says
nothing favourable about a sub-bar whale, and the cost model buries it. This is
reported as a confirmation, never as the headline.

Sensitivity grid, each cell re-running the whole test on the 70% (reported for
transparency, **not** a search for a winning cell). Claim A's adverse gap is
**stable across every cell** (always negative, always |t| ≥ 1.63, and the
significance only falls below 2 on the thinnest sample, `power_k=2.5` at 356
events):

| named parameter | values | A gap (R) / t | B gap (R) / t |
|---|---|---|---|
| power k (× ATR) | 1.0 / **1.5** / 2.0 / 2.5 | −0.086/−68.74 / **−0.085/−59.22** / −0.068/−19.94 / −0.049/−1.63 | +0.020/+7.01 / **+0.020/+4.70** / +0.009/+0.80 / +0.080/+1.42 |
| power window (bars) | 2 / **3** / 4 | −0.086/−51.68 / **−0.085/−59.22** / −0.085/−62.47 | +0.016/+3.41 / **+0.020/+4.70** / +0.023/+5.53 |
| close-in-range q | 0.0 / **0.25** / 0.5 | −0.072/−24.97 / **−0.085/−59.22** / −0.086/−62.27 | +0.028/+3.60 / **+0.020/+4.70** / +0.021/+5.25 |
| enough-red c (× prior-day red median) | 0.75 / **1.0** / 1.5 | −0.084/−60.55 / **−0.085/−59.22** / −0.090/−50.28 | +0.020/+4.87 / **+0.020/+4.70** / +0.023/+4.42 |
| bounce window (bars) | 3 / **6** / 12 | −0.085/−59.58 / **−0.085/−59.22** / −0.085/−58.92 | +0.019/+4.39 / **+0.020/+4.70** / +0.020/+4.79 |
| entry tol (× ATR) | 0.0 / **0.05** / 0.15 | −0.079/−63.28 / **−0.085/−59.22** / −0.102/−59.06 | +0.019/+4.65 / **+0.020/+4.70** / +0.026/+5.59 |
| **entry mode** | **conservative** / aggressive | **−0.085/−59.22** / −0.083/−61.95 | **+0.020/+4.70** / +0.034/+8.69 |
| stop buffer (× ATR) | 0.25 / **0.5** / 1.0 | −0.078/−51.79 / **−0.085/−59.22** / −0.068/−31.34 | +0.020/+5.19 / **+0.020/+4.70** / +0.029/+4.11 |
| max-stop cap (× ATR) | 0.0 / **3.0** / 6.0 | −0.077/−60.02 / **−0.085/−59.22** / −0.080/−64.23 | −0.005/−1.99 / **+0.020/+4.70** / +0.001/+0.51 |
| target grid (× ATR) | 0.5 / **1.0** / 2.0 | −0.079/−56.28 / **−0.085/−59.22** / −0.086/−60.44 | +0.016/+4.59 / **+0.020/+4.70** / +0.027/+4.74 |
| time stop (bars) | 48 / **96** / 192 | −0.085/−59.28 / **−0.085/−59.22** / −0.085/−59.20 | +0.020/+4.79 / **+0.020/+4.70** / +0.020/+4.68 |
| bp horizon (bars) | 12 / **24** / 96 | (R unaffected) **−0.085/−59.22** | **+0.020/+4.70** |
| min-R filter (bp) | **0** / 20 / 50 | **−0.085/−59.22** / −0.088/−62.57 / −0.093/−62.98 | **+0.020/+4.70** / +0.021/+5.07 / +0.022/+4.94 |
| zone1 definition | **pos** / pos_slope | **−0.085/−59.22** / −0.085/−59.22 | **+0.020/+4.70** / +0.015/+3.11 |

**(**) = locked value.** Two honest reads of the grid:

1. **The Claim-B sign is fragile where it matters and the sample moves under it.**
   When the max-stop cap is removed (`max_stop_cap=0.0`, the largest sample at
   314,925 events) the Claim-B net-R gap **flips negative** (−0.005R, t −1.99),
   and at `6.0×ATR` it collapses to +0.001R (t +0.51). The zone gap only exists
   inside the locked `3.0×ATR` cap. That is not robustness; it is a dependence on
   the discard rule, and combined with the holdout sign-flip it is why Claim B
   fails rather than merely "wins on the first 70%."
2. **The lone cell where a measure turns positive is the aggressive arm's bp —
   and it contradicts the realized R.** `entry_mode=aggressive` (`cut the green
   in half`, a sell-limit at the bounce bar's midpoint) shows a *positive*
   fixed-horizon bp gap of +8.18 bp while its realized R is still negative
   (−0.083R). That is the geometry-free measure pointing the *other way* from
   realized R, so the aggressive entry's slightly better fill price is offset by
   a materially worse stop distance and negative net R. It is **not** a
   profitable arm; it is reported because the two house-standard measures
   disagree in exactly the cell the source emphasises as his preferred
   management, and the disagreement should be visible rather than smoothed over.

**The implementation bug, disclosed.** A first internal smoke run (before the
frozen run recorded above; nothing is committed, so it exists only in this
writeup) handed the bounce arm the *better* fill: `attach_bounce` computed the
pessimistic fill `min(trigger, open)` but `simulate` then re-derived the entry
from `p.o[entry_idx]`, silently giving the short the bar's open rather than its
own stop trigger. For a short, `open` is usually *better* than the trigger on a
down-gap, so the pre-fix run **flattered** the bounce arm. Correcting it flips
Claim A from a spurious **+0.197R (t +95)** pass to the **−0.085R (t −59)** fail
above, and moves Claim B from a −0.015R fail to a +0.020R first-70% pass whose
holdout then reverses. The fix is pinned by
`test_bounce_entry_fills_at_the_trigger_not_the_bar_open` and
`test_intrabar_entry_bar_gets_no_target_credit`, and **no locked number was
changed** — the frozen spec is exactly what produced the result above. The
lesson is recorded here deliberately: a sign-flipping bug lived between the
trigger and the simulator, and only the pessimistic-fill convention caught it.

**Answer to the question the row was written to answer: does this reproduce a
profitable edge? No.** Both separable claims fail, for independent reasons:

- **Claim A (the dead-cat mechanism) is simply false on this data, and backwards.**
  Shorting the failed bounce is *worse* than shorting the power drop directly —
  −0.085R per event, day-clustered t −59.22 — and its geometry-free fixed-horizon
  bp is −10.16 bp. Neither short is profitable after the 15 bp round trip; the
  naive short is merely less bad. The unconditional forward move after a power
  drop is **−8.72 bp** (i.e. the drop mean-reverts), so there is not even an
  unconditional continuation for the bounce entry to time.
- **Claim B (the zone rule) does not survive its own holdout.** The first 70%
  gives the sign his rule needs (+0.020R, t +4.70), but opening the last 30% once
  reverses it (−0.003R, t −0.54), and the zone arms are both net losers. A gap
  between two losing arms that itself flips sign out of sample is not support for
  *"never short in zone 1."*

The video carries no statistics; this row supplies them, and they do not
reproduce the claim. The debunk also lands exactly where the source's own
hedging predicted it would: **the setup's trigger and its "enough red" / slope
judgement are not objective enough to define, and once made mechanical on a
venue that does not gap, the mechanism has no edge.** This is a clean negative
result and is treated as one.

## Level 2 order flow

The tape is `hlbot-runtime/live/state/hl_microstructure.jsonl`.
`ofi_5` is `(bid depth − ask depth) / (bid depth + ask depth)` on the
top 5 levels. `taker_ratio_5m` is buy volume over total volume in the
trailing 5 minutes. Snapshots are 30 seconds apart. As of the last
line used to write this file, both BTC and ETH have about 29 hours.

Shared gate for L1, L2, and P3: do not open the scatter until the file
contains 20 full UTC days for that coin. Do not download a substitute
L2 history from somewhere else and quietly change the row. If the
recorder stops, the clock pauses with it.

### L1. Book imbalance

Who loses: a market order that lifts the bid or hits the ask and pays
the spread to a resting quote that was already heavy on the other side.
This is a queue and inventory effect. It dies if the imbalance is
someone else's informed flow rather than a stale quote.

Data: BTC `ofi_5` and `mid`. ETH is not this row.

Locked: signal is `ofi_5` now. Forward return is the mid 5 minutes
later (10 snapshots), minus the taker cost below. No smoothing, no
second depth, no threshold on the imbalance.

Costs: taker fee 4.5 bp plus half of `spread_bps` at the signal
snapshot, each side. On this tape the quoted BTC spread is often about
0.1 bp, so the fee dominates. A 5-minute edge smaller than about 9 bp
round trip is a description, not a trade.

Scatter: after 20 days, on the first 70% of those days, mean forward
return by quintile of `ofi_5`, costs subtracted, plus the
day-clustered t-statistic of the top quintile minus the bottom.
Snapshots inside a day are not independent samples. The t-statistic
uses one number per day.

Walk-forward: last 30% of the 20 days, once. Twenty days is the
minimum, not a suggestion to keep adding days until it passes.

Kill: the top-minus-bottom spread is at or below zero on the first
70%, or its day-clustered t-statistic is below 2. A pass that is
smaller than the 9 bp round trip is also a kill.

### L2. Aggressive flow

Who loses: a liquidity provider who was paid to be on the wrong side
of a short burst of market buys, if that burst exhausts rather than
continues. If the burst continues, the same person is the winner and
this row is the wrong way around. The scatter is allowed to show
either sign. The sign is not chosen after looking.

Data: BTC `taker_ratio_5m` and `mid`, same file and same 20-day gate.
This row is not computed on the L1 holdout, and L1's result is not a
reason to flip this one.

Locked: signal is the raw ratio. Forward window is the next 5 minutes.
Quintiles, same as L1. No cutoff such as "only when ratio > 0.7."

Costs: same taker round trip as L1.

Scatter: top-minus-bottom quintile of the next 5-minute mid, costs in,
day-clustered.

Walk-forward: its own 70/30 split of the same 20 days. Looking at L1
first and then deciding whether to open L2 is a violation. They are
alternatives, and only one of them may be the first L2 test. L1 is
the one that is first, because the book imbalance is the recorder's
actual order-book field. L2 waits until L1 is closed, pass or kill,
and then uses the following 20 days, not the same 20.

Kill: same rule as L1.

### L3. Do not cross a wide, thin book

Who loses: the bot itself, when it pays a spread that is wide relative
to its own edge. This row does not predict a return. It is a constraint
on Q2, Q3, and any later taker row.

Data: `spread_bps`, `bid_depth_usd`, `ask_depth_usd` once those rows
are tested. The pairs bot does not see this file today.

Locked: skip a taker entry when `spread_bps` is above 2, or when the
top-5 depth on the side being lifted is below $50,000. Those two
numbers are the constraint. They are not fitted.

Costs: the skipped trade is cash. No credit is taken for an imagined
better fill.

Scatter: none. This is not an alpha row. It is applied only inside a
row that has already passed its own scatter, as a robustness check on
that row's walk-forward. If the pass disappears once wide books are
skipped, the pass was the spread, not the signal.

Walk-forward: inherited from the parent row.

Kill: not applicable on its own.

## Behavioral inefficiencies

### B1. Round numbers

Who loses: a stop or a take-profit parked on a round price, which
leans on the book and then snaps back if the level was only a cluster
of orders.

Data: trade prints with prices, not just the top of book. The
microstructure file stores a 5-minute taker ratio, not the print
prices. Hyperliquid's public trade stream can supply this, and it is
not in the file yet.

Locked, for when a print tape exists: a round level is a BTC price
divisible by 100. Window is 1 minute before the touch through
5 minutes after. The bet is reversion of the mid back through the
level. No other modulus. ETH is a different row.

Costs: taker round trip. A 100-dollar level on a 80,000-dollar price
is about 12 bp, which is the same order as the round trip. The scatter
has to clear that, not merely show a wiggle.

Scatter: mean net return of the fade, one event per touch, clustered
by day.

Walk-forward: 70/30 by day, after at least 120 days of prints.

Kill: mean net return at or below zero on the first 70%.

Status: blocked. Do not approximate this with candle highs and lows.

### B2. UTC-day boundary

Who loses: a inventory holder who flattens or rebalances because the
exchange's day rolled, not because of a view.

Data: BTC 5-minute candles. The window is 23:30–00:30 UTC.

Locked: measure the mean return of that hour, and nothing else.
No "only on Mondays," no volatility filter.

Costs: if the row is only a description of drift, costs are zero and
the result cannot be promoted to a trade. If it is later traded, the
taker round trip applies and will likely swallow an hour-long drift.
Those are two different uses, and the description is the one this row
allows.

Scatter: mean return of that clock hour minus the mean return of the
other hours, t-statistic across days.

Walk-forward: 70/30, minimum 120 days.

Kill: the difference is at or below zero, or the t-statistic on the
first 70% is below 2. Promoting it to a traded row without a written
follow-up row is a kill of its own.

## Proprietary and external data

### P1. Skylark as a day-trade side

Who loses: nobody identified. FLP is a weekly and monthly macro print.
It does not arrive during the session, so it cannot be the reason a
day-trade counterparty loses today.

Data: the published FLP series already used by the pairs bot as a
size input.

Locked: none granted.

Costs: not reached.

Scatter: not opened. Using today's FLP bucket to pick a long or a
short for the cash session is a second use of a series whose BTC
timing test was already a weak result against buy-and-hold, and whose
clock is wrong for this matrix.

Walk-forward: not opened.

Kill: closed. FLP may remain a size dial on a slower book. It is not
an entry in this file.

### P2. The pairs journal

Who loses: the model, if its predicted trade return is larger than
the trade it actually got. That is the calibration the bot already
defined and has not been able to update, because closed rows still
have a blank `exp_return_at_entry`.

Data: `hlbot-runtime/live/reports/trades.csv` and `signals.csv`.

Locked: the existing calibration, prior mean 0.5, prior sd 0.25.
No new feature.

Costs: already inside the live PnL.

Scatter: predicted return against realized `pnl_pct`, once new closes
store the prediction. Until those cells fill in, there is nothing to plot.

Walk-forward: the next 30 closed trades after the column starts
filling, scored once against the prior. Not a day-trade entry.

Kill: if those 30 pull the calibration factor to the floor, the
model's edge stays shrunk. That is a pairs-bot result, and it does
not create a day-trade row.

### P3. Kalshi 15-minute contract versus the Hyperliquid mid

The contract-clock tape started on 2026-10-08 is a different row from this
one. Its locked rules are `config/kalshi_hl_hypothesis.yaml`: first-minute
Hyperliquid imbalance versus the official settlement, scored as
settlement minus the ask minus the one-contract taker fee. This P3
mid-versus-mid comparison stays unopened.

Who loses: a slower quote. If the Kalshi contract is a poll of the
same 15-minute window and the Hyperliquid mid is the underlying, one
of them can be late. Competing on that lateness with a professional
taker is the wrong use. The only version that fits a solo account is
a slow disagreement: the contract and the mid diverge by a wide
margin and the mid follows over minutes, not milliseconds.

Data: `kalshi_mid` joined to `mid` in the same microstructure file.
Same 20-day gate as L1. This row does not open on L1's 20 days. It
waits until L1 is closed and then uses the next 20.

Locked: signal is Kalshi mid minus 0.5, interpreted only as the sign
of the contract's lean. Forward window is 15 minutes of BTC mid, to
match the contract's own horizon. One lag is allowed in the scatter
and must be chosen from the recorder's own timestamp, not by searching
lags. The lag is one snapshot (30 seconds), precommitted, meaning the
Kalshi print is stale by at least that much before it is allowed to
"lead."

Costs: taker round trip. A lead that is smaller than the cost is a
kill even if the sign is right.

Scatter: mean 15-minute BTC return conditional on the sign of
`kalshi_mid - 0.5`, costs subtracted, day-clustered, first 70%.

Walk-forward: last 30%, once.

Kill: the conditional mean is at or below zero, or the day-clustered
t-statistic is below 2, or the edge is inside the round trip.

## What this file refuses

- A leaderboard of all rows on one sample.
- A second horizon after a kill. Q4 is the example.
- Using ETH, or a second pair, as confirmation of a BTC row that was
  already inspected.
- Maker fills at the mid.
- Level 2 conclusions from the current one-day file.
- Treating Skylark, the sector clocks, or the pairs z-score as day
  trades by changing the hold and keeping the story.
