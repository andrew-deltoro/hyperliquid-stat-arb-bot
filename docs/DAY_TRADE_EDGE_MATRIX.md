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
| Hyperliquid `candleSnapshot` | Intraday statistical rows, once a horizon is pulled. Hourly history is already how the pairs bot fits. | A finished intraday study. The bars are not sitting in a day-trade panel yet. |
| Funding cache, 10 Jun–6 Oct 2026 | The funding rows below. Daily and hourly funding for the perp universe. | A claim about the next price. That daily decile already failed. |
| `hl_microstructure.jsonl` | BTC and ETH, top-5 book, from 2026-10-07 01:31 UTC through 2026-10-08 06:14 UTC. 3,443 snapshots each, every 30 seconds. Fields: `ofi_5`, `taker_ratio_5m`, `spread_bps`, `vwap_dev_bps`, `kalshi_mid`. The recorder is still appending. | A walk-forward. One day is one regime. No L2 row opens its holdout until 20 full UTC days are in the file. |
| Yahoo daily bars | Nothing in this matrix. | Day trades in ETFs or FX. |
| Webull, TradingView | Nothing. Webull needs an OpenAPI key and a separate quote subscription. TradingView has no public price endpoint. | Those rows. |
| Pairs journal | The bot's own predicted-versus-realized record, once `exp_return_at_entry` is filled in. | A new intraday signal. |
| Skylark FLP | A size dial decided before the session, from the latest published weekly or monthly print. | An intraday entry. The print does not update during the day. |

## Status

| ID | Source | Claim, one line | Status |
|---|---|---|---|
| Q1 | Statistical | A pair residual reverts inside one session and is flat by the end of it | Closed |
| Q2 | Statistical | The first 30 minutes of range predicts the rest of the UTC day | Ready to design a pull, not run |
| Q3 | Statistical | Yesterday's close-to-open gap fills today | Ready to design a pull, not run |
| Q4 | Statistical | Funding paid since UTC midnight predicts the rest of today's price | Closed |
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
