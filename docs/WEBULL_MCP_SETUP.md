# Webull Cloud MCP (read-only)

Configured 2026-10-15. Server entry lives in `~/.cursor/mcp.json`:

```json
{ "mcpServers": { "webull": { "url": "https://api.webull.com/mcp" } } }
```

Global scope was chosen deliberately: there is no
`hyperliquid-stat-arb-bot/.cursor/mcp.json`, so nothing shadows it, and
the entry applies to every workspace.

## Status: AUTHORIZED and working (verified 2026-10-15)

Authorization is live. `mcp_auth` succeeded and the namespace reports
`namespaceStatus: "ready"` with 83 tools. No browser step was required,
so an existing Webull/Cursor OAuth session was reused — see the security
note below.

## Verified entitlement map

Every row below was exercised against the live API, not inferred. The
"Before" column is the state recorded earlier on 2026-10-15; "After" is a
re-test after the user activated OpenAPI subscriptions. Every "After"
cell is a literal live response, not an inference.

| Layer | Before | After (re-tested 2026-10-15) |
|---|---|---|
| Crypto bars (`get_crypto_bars` BTCUSD M5) | WORKS | **WORKS** — live bars |
| Crypto snapshot | WORKS | **WORKS** — live bid/ask |
| Stock bars (`get_stock_bars` AAPL M5) | WORKS — `delay_minutes: 0` | **WORKS** |
| Stock depth (`get_stock_quotes`) | Call succeeds but `bids`/`asks` empty (L1 not entitled) | **WORKS — L1 *and* L2.** `depth=1`, `5`, `50` all return priced ladders with per-MPID `order` detail (AAPL, SPY) |
| Stock tick tape (`get_stock_tick`) | not tested | **WORKS** — trades with `side` + `trading_session` |
| Stock NOII (`get_stock_noii_bars`) | not tested | **WORKS** — `imbalance_*` rows returned |
| Stock capital flow (`get_stock_capital_flow`) | not tested | **WORKS** — 5 days of `large/medium/small in/out` |
| Futures bars (`get_futures_bars` MNQZ6) | LOCKED — subscribe US_FUTURES | **still LOCKED** — `MARKET_DATA_NOT_SUBSCRIBED:Insufficient permission, please subscribe to US_FUTURES.` |
| Futures depth (`get_futures_depth` MNQZ6) | LOCKED — subscribe FUTURES LV2 | **still LOCKED** — `MARKET_DATA_NOT_SUBSCRIBED:Insufficient permission, please subscribe to FUTURES LV2.` |
| Futures tick (`get_futures_tick` MNQZ6) | not tested | **still LOCKED** — `MARKET_DATA_NOT_SUBSCRIBED:Insufficient permission, please subscribe to US_FUTURES.` |
| Futures snapshot (`get_futures_snapshot` MNQZ6) | not tested | **still LOCKED** — `MARKET_DATA_NOT_SUBSCRIBED:Insufficient permission, please subscribe to US_FUTURES.` |
| Futures footprint (`get_futures_footprint`) | LOCKED — subscribe FOOTPRINT | **still LOCKED** — `MARKET_DATA_NOT_SUBSCRIBED:Insufficient permission, please subscribe to FOOTPRINT.` (tested at M1 and M5) |
| Stock footprint (`get_stock_footprint`) | LOCKED — subscribe FOOTPRINT | **still LOCKED** — `MARKET_DATA_NOT_SUBSCRIBED:Insufficient permission, please subscribe to FOOTPRINT.` (tested AAPL, SPY) |
| Option chain (any `get_option*`) | unavailable | **no such tool exists** — the 83-tool catalog has only `place_option_single_instruction` / `place_option_strategy_instruction` (order drafting), no market-data option endpoint |

So the real change is **narrower than the "Order Flow" label suggests**:
the equity depth/tick/NOII/capital-flow group flipped on, and that is
consistent with a **Nasdaq L1/L2 (TotalView)** entitlement. The two
layers Q7 actually needs — **footprint and options/GEX** — are both
still dark. `get_futures_footprint` still asks for `FOOTPRINT`, and there
is no option-chain tool to subscribe to. Note also that one L1/L2
subscription permits **one device** at a time; a second consumer may
evict this one.

### Futures symbols need contract codes

`MNQ` is rejected as `INVALID_SYMBOL`. Use the dated contract, obtained
from `get_futures_instruments` with `code=MNQ`:

| Symbol | Contract |
|---|---|
| `MNQZ6` | Micro E-mini Nasdaq-100, Dec 2026 |
| `MNQH7` | Micro E-mini Nasdaq-100, Mar 2027 |

## Security posture — read this

Two things to be aware of:

**1. The authorization is `Trading`, not read-only.** The account list
returns `authorizationCategory: "Trading"` for the cash account
(account number redacted here). I had recommended granting only the four
`:read` scopes. That is not what is granted.

**2. No direct order placement exists, which limits the blast radius.**
Every order tool is named `place_*_instruction`, and each description
states plainly:

> This does NOT place an order directly: it creates an instruction that
> the user must confirm in the Webull App or Desktop before it becomes a
> real order. Only LIMIT orders are supported.

So an agent cannot execute a trade unilaterally — it can only draft a
pending instruction that a human must confirm in Webull. `revoke_instruction`
exists to cancel a pending one. There is no "market order now" path.

Still: to reduce the grant to what research needs, revoke the Webull
authorization and re-consent without `order:write` / `instruction:all`.
Doing so removes even the ability to draft instructions.

## Getting access for the bot (not just chat)

This MCP authorizes *the agent's tool calls*. It does not give the bot
process in `scripts/feeds/webull_connector.py` anything — the bot needs
an App Key/Secret via the OpenAPI application, which is a separate path.
MCP is for interactive work; the connector is for scheduled research.



## Cross-venue check: Webull crypto vs Hyperliquid (2026-10-15)

Ran the free tier against the bot's own data source. Pulled 1,200 Webull
5m bars (the max) and 1,440 Hyperliquid 5m bars, then compared the
overlapping 1,199 bars.

**Alignment was verified before anything else.** A lag sweep showed
correlation peaking sharply at lag 0 and collapsing to ~0 at every other
lag (BTC: 0.9533 at 0, and -0.0071 / 0.0162 / -0.0716 / 0.0369 at
-2/-1/+1/+2). So the timestamps line up and the numbers below are not
an artifact of a one-bar offset.

| Metric | BTCUSD vs BTC | ETHUSD vs ETH |
|---|---|---|
| 5m return correlation | 0.9533 | 0.9758 |
| Tracking error | 3.44 bps / bar | 3.69 bps / bar |
| Mean abs return diff | 1.62 bps | 1.26 bps |
| Mean basis (Webull−HL) | −49.80 USD (−6.01 bps) | −1.43 USD (−5.74 bps) |
| Basis sd | 22.44 USD (2.75 bps) | 0.62 USD (2.60 bps) |

### What this means

The venues agree closely: ~95–98% return correlation with ~3.5 bps
tracking error per bar. Consistent with two properly functioning venues.
Webull is **spot**, Hyperliquid is **perp**, so the ~−6 bps mean is the
perp trading at a premium — and that is roughly the size of a typical
funding/basis, not a data error.

### Why this is NOT a tradeable edge

Tempting but wrong. The basis deviations are extremely sparse:

| Threshold (BTC) | Bars | Clusters |
|---|---|---|
| \|basis − median\| > 10 bps | 7 (0.6%) | 5 |
| \|basis − median\| > 25 bps | 2 (0.2%) | 1 |
| \|basis − median\| > 50 bps | 0 | 0 |

Zero events past 50 bps in 4.2 days. Round-trip taker cost on these
venues is ~15 bps, so a deviation would have to clear ~15 bps to be worth
touching, and the observed tail essentially never gets there. The large
basis prints (−329, +179 USD) are **isolated bad ticks** clustered in
minutes like 02:15–02:35 on a single day, not persistent divergence —
which is exactly what a naive basis strategy would trade into.

### Two hard limits found

1. **No volume.** Webull crypto bars return only
   `time, open, high, low, close`. Hyperliquid bars carry volume. Any
   volume- or flow-based study is impossible on this feed.
2. **No lag/synthetic-feed exploit.** `corr(Webull_ret[t], HL_ret[t+1])`
   is **−0.0716** — negative. Webull is not echoing Hyperliquid late,
   so there is no latency arb in this pair.

### Still unresolved

`close` matches `snapshot.price` in one check, so `close` is likely the
last trade. But with no volume, whether each bar is a true
interval-aggregated OHLC or a point-in-time sample is only partly
settled. Treat bar-level OHLC on this feed as directionally reliable
(hence the high correlation) but not audited as execution-grade.

Files: `reports/.webull_cache/` (gitignored).

## Pricing (as of 2026-10-15, from Webull's published FAQ)

Per **OpenAPI**, separate from any app/QT subscription:

| Data | Price | Unlocks here |
|---|---|---|
| Nasdaq Basic (L1 stocks) | **Free** | equity bars + best bid/ask |
| Nasdaq TotalView (L2) | $2.99/mo non-entity · $108.99/mo entity | L2 depth, MBO |
| OPRA real-time options | $2.99/mo non-pro · $45/mo pro · **free** if you trade options once in a calendar month | option chains → real GEX |
| CME Futures L1 | **$11.99/mo** | MNQ/NQ bars |
| CME Futures L2 | **$38.99/mo** | 10 levels of futures depth |
| Order Flow (footprint) | **not published** — view in Advanced Quotes Center | footprint / TPO |

The one that matters most for Q7 is **Order Flow**, and Webull does not
publish its price. It has to be read from the Advanced Quotes Center in
your account. That is the single number blocking a decision on Q7.

Realistic bundles:

- **Z7 test (MNQ + footprint):** Futures L1 $11.99 + Order Flow (?) ≈
  **$12/mo + unknown**
- **Q7 full (adds real GEX):** + OPRA, likely free if you trade options
  monthly, else $2.99
- Ceiling with futures L2: ~$52/mo + Order Flow

### The buried lever

**OPRA is free if you trade options at least once per calendar month.**
That is the true dealer-gamma layer for Q7, effectively at no cost, via a
single qualifying options trade. Worth knowing before paying for anything
else.

## Free tier: how far it actually reaches (verified 2026-10-15)

I mapped the free entitlement properly, and the useful finding is that
**equity/ETF bars, not crypto, are the real free asset.**

| Capability | Free tier result |
|---|---|
| SPY 5m history | **back to ~2010** (empty at Jan 2010, data from 2011 on) |
| SPY 1m history | **back to ~mid-2021** |
| SPY daily | 1,200 bars, 2022-01-03 → 2026-10-14 |
| Volume | **present** (`volume` field on every equity bar) |
| `delay_minutes` | **0** — real-time, not the 15-min sandbox delay |
| Routing | **RTH verified**; `PRE`/`ATH` sessions work with `trading_sessions` |
| Date-range pagination | **works** (`start_time`/`end_time`), pages overlap and de-dupe cleanly |

Verified page-to-page behaviour: two 1,200-bar requests stepping 1,000
seconds produced **1,824 unique bars spanning 2025-05-12 → 2025-06-13**
with 576 expected overlapping bars, and a gap histogram that is 1,800
exact 300s gaps plus only 23 session boundaries (overnight/weekend). RTH
bars only. That is clean, contiguous, session-aware 5m data.

### Why this matters more than it looks

This retires a constraint recorded earlier in the matrix. The old note
was that Hyperliquid 5m holds only ~17.4 days and no free source offered
deep intraday equity history. That is no longer true: **free SPY/equity
5m back to ~2010 with volume, real-time, paginatable.**

That makes day-trade rows on US equities (Q6's MACD/200-EMA family, Q7)
newly testable over multi-regime periods and with volume-based filters —
things the crypto feeds could never support. Gaps of 63,300s and larger
are weekends/holidays, so any equity study must be session-aware from the
start.

## What it is NOT



Not the data spine for research. Verified by reading the
`webull-openapi-mcp` package source:

- Tool output is **formatted prose** with an investment disclaimer
  prepended, not raw JSON.
- `format_futures_footprint` prints only `total / delta / buy_total /
  sell_total`. The `buy_detail` / `sell_detail` sub-ladder is **dropped**
  before the model sees it.
- Tool descriptions map 1:1 onto the paid OpenAPI endpoints
  (`get_futures_footprint` is described as "capital flow data").

For backtests and bulk pulls, use `scripts/feeds/webull_connector.py`,
which keeps the raw response and maps it to the bot's `Candle` type.

## Read-only is enforced by scopes

The server advertises exactly six scopes (from
`.well-known/oauth-protected-resource`):

    account:read      order:read       order:write
    market:read       instrument:read  instruction:all

**To stay read-only, authorize WITHOUT `order:write` and WITHOUT
`instruction:all`.** The remaining four (`account:read`, `order:read`,
`market:read`, `instrument:read`) are all read capabilities and are
enough for market data and portfolio inspection.

Cloud MCP also uses an explicit instruction model: order-placing tools
are named `place_*_instruction` and only *draft* an order, requiring
confirmation in the Webull app. That is a second safety net, but it is
not a reason to grant write scopes you do not need.

## Headless authorization caveat

This host is Cursor **remote-server on headless Linux** (no browser, no
`agent` binary on PATH). The OAuth flow is Authorization Code + PKCE
(S256, public client — `token_endpoint_auth_methods_supported: none`)
with dynamic client registration.

Endpoints discovered:

| Purpose | URL |
|---|---|
| Authorization | `https://passport.webull.com/oauth2/ai-mcp-new/login` |
| Token | `https://u1suserauth.webullfintech.com/api/userauth/oauth/token/token` |
| Registration | `https://u1suserauth.webullfintech.com/api/userauth/oauth/client/register` |

Consequence: the browser leg has to be completed on a machine with a
browser, then the resulting token supplied to this host. Practical
routes, easiest first:

1. Authorize from Cursor **Desktop** on your own machine against the same
   account, then copy the resulting MCP auth state to this host.
2. Run the OAuth dance elsewhere, capture the token, and place it in the
   config as a header (see below). Tokens expire; expect to redo this.

`mcp.json` also accepts headers, which is the escape hatch when
interactive OAuth is impossible:

```json
{"mcpServers": {"webull": {"url": "https://api.webull.com/mcp",
  "headers": {"Authorization": "Bearer ${env:WEBULL_MCP_TOKEN}"}}}}
```

## Verify it works, for $0

`get_crypto_bars` is the cheapest proof: crypto needs no market-data
subscription. If it returns bars, authorization is intact. This was
confirmed working on 2026-10-15 (BTCUSD 5m, live prices).

## How to re-authorize or change scopes

1. In Webull: revoke the existing AI/MCP authorization. The current state
   is readable via `get_account_authorization`.
2. In Cursor: the server entry is already in `~/.cursor/mcp.json`. OAuth
   attempt state lives under
   `~/.cursor-server/data/User/globalStorage/anysphere.cursor-mcp/`.
3. Trigger `mcp_auth` for the `user-webull` namespace.
4. Re-consent on Webull's page, choosing **only** `account:read`,
   `order:read`, `market:read`, `instrument:read`.
5. Confirm via `get_account_authorization` that the category is no longer
   `Trading`.

A prior authorization may be silently reused, so if you expect a consent
screen and see none, that is why.

## Entitlement identity: what this credential is, and what it can see (2026-10-15, third pass)

Forensic follow-up to the two-pass entitlement map above. The question
was whether the Cloud MCP credential is the *same* identity/app that holds
the paid subscriptions, and whether an in-app Order Flow purchase can
reach it.

**Headline: the premise changed.** `get_futures_footprint` and
`get_stock_footprint` now return **live data**, not
`MARKET_DATA_NOT_SUBSCRIBED:...FOOTPRINT`. The Order Flow / footprint
entitlement **is** reachable by this credential. What is still dark is a
*different* subscription line — CME futures exchange data — and there is
still no option-chain tool.

### 1. What the MCP session is authenticated as

| Tool | Returned fields (verbatim shape) | What it proves |
|---|---|---|
| `get_account_list` | one object: `account_type: "CASH"`, `account_class: "INDIVIDUAL_CASH"`, `account_label: "Individual Cash"`, plus an `account_id` / `account_number` pair and a `user_id` | The session is linked to a real, personal Webull **brokerage account** (values deliberately not repeated here — see the security note in the section above). |
| `get_account_authorization` | `accounts: [{ authorizationCategory: "Trading" }]` + an `authorizationDescriptions` map (`Non-Trading`, `Trading`) | The OAuth grant requested **`Trading`** (superset of `Non-Trading`). It returns **only** the trading category — it does **not** enumerate market-data permissions. |
| `get_account_balance` | `total_net_liquidation_value` ≈ `0.07`, one `USD` currency asset row | The credential can read live account state, confirming it is not a stale/placeholder token. |

What this does **not** prove, stated plainly: `get_account_authorization`
is the single most diagnostic-looking artifact here, but it is a
**trading-scope** report, not a market-data entitlement report. Nothing in
the 83-tool catalog exposes a "what data am I entitled to" endpoint. So
account identity is proven; market-data entitlement is **not** directly
enumerable — it is only observable by *calling data endpoints and seeing
which succeed*.

The OAuth layer is capability-scoped, not data-scoped. From
`.well-known/oauth-protected-resource` (fetched live this pass):

    account:read  order:read  order:write  market:read
    instrument:read  instruction:all

Those six are MCP **capability groups** (`market:read` = "may call market
data tools"), not market-data product entitlements. They say nothing about
FOOTPRINT, US_FUTURES or FUTURES LV2.

### 2. Are OpenAPI entitlements per-app?

Partly documented, partly inference. Webull's docs are explicit that the
*subscription* is separate from app/QT purchases, and that the Data API is
an app-scoped credential:

| Claim | Source | Status |
|---|---|---|
| In-app/QT market-data subscriptions do **not** apply to OpenAPI; you must buy a separate "OpenAPI Advanced Quotes" subscription | `developer.webull.com/apis/docs/market-data-api/overview.md`; `.../subscribe-quotes.md` | **Documented, verbatim** |
| Order Flow is a distinct line item: "Subscribe **Order Flow Non-display** for Footprint and Time Price Opportunity indicators" | `.../market-data-api/overview.md` (Permissions table) | **Documented** |
| Futures are a separate line item: "Subscribe CBOT / CME / COMEX / NYMEX individually or CME Group bundle" | same table | **Documented** |
| Data API is keyed per application: "Most endpoints are rate-limited at the application level. All requests sharing the same `app_key` count toward a single quota." | `developer.webull.com/apis/docs/rate-limits.md` | **Documented** |
| Cloud MCP rides OpenAPI under the hood | `.../AI-friendly-Resources/mcp.md` architecture diagram: `MCP -> API: OpenAPI Request` | **Documented** |

**Inference (labelled as such):** Cloud MCP users "do not need to … manage
API credentials" (`.../AI-friendly-Resources/mcp.md`), yet the MCP server
still issues OpenAPI requests. That implies the Cloud MCP backend calls
Webull OpenAPI with **Webull's own hosted app credential**, acting on
behalf of the OAuth'd user — not with a user-generated App Key. Webull does
**not** document how a per-account "OpenAPI Advanced Quotes" purchase is
resolved against that hosted app. So the binding is **undocumented**; the
strongest evidence-based reading is: *subscriptions attach to the user
account, and the hosted Cloud MCP app is granted data on that account's
behalf*. Critically — and this is now settled empirically — whatever the
wiring is, **Order Flow data reached this credential.**

### 3. The diagnosis

The hypothesis in the brief ("an in-app Order Flow Pro purchase will never
propagate to it") is **falsified by the live re-test.** Footprint works.

| Layer | This pass (2026-10-15, ~08:1x UTC) | Evidence |
|---|---|---|
| `get_futures_footprint` MNQZ6 | **WORKS** — real per-price ladder | `buy_detail`/`sell_detail` price→size maps, `delay_minutes: 0`, bar time = same UTC hour |
| `get_futures_footprint` ESZ6 S5 | **WORKS** | full ladder, `delay_minutes: 0` |
| `get_stock_footprint` AAPL / SPY / NVDA | **WORKS** | per-price `buy_detail`/`sell_detail`, live |
| `get_stock_quotes` AAPL depth=50 | **WORKS** — full L2 with per-MPID `order` | `quote_time` epoch decodes to current UTC |
| `get_stock_capital_flow` AAPL | **WORKS** | 2 days of large/medium/small in/out |
| `get_futures_bars` / `_tick` / `_snapshot` MNQZ6 | **LOCKED** — `subscribe to US_FUTURES` | separate CME L1 line item |
| `get_futures_depth` MNQZ6 | **LOCKED** — `subscribe to FUTURES LV2` | separate CME L2 line item |
| Option chain | **no tool exists** | catalog has only `place_option_*_instruction` |

**Determinism (the discriminating test the brief asked for):** the
footprint result is **stable and deterministic, not intermittent**. Repeat
calls are byte-identical: MNQZ6 M5 returned the same 5,428-total ladder on
three separate calls; AAPL M1 returned the same 496,363-total ladder twice.
A stable *success* means the grant is present and resolved — this is not a
caching artifact. (Note the failure mode is now inverted from the brief:
previously the error was stable; now the *data* is stable.)

So the honest reading is:

- **Not** "purchase never registers" — it registered, and it propagated.
- **Not** "registered at account level but structurally unreachable" — it is reachable *now*.
- **Inference, flagged:** between the second-pass test (which saw the
  `FOOTPRINT` error) and this third pass, either the entitlement finished
  propagating, or the second pass ran before it landed. Webull does not
  publish propagation SLA, so *which* of those it was is **not
  determinable** from here. A fresh OAuth completion at `07:50:19` UTC
  (see the MCP server log, below) is consistent with a mid-morning
  activation; it is not proof of causation.
- What *remains* unreachable is unrelated to Order Flow: **CME futures
  L1/L2** (`US_FUTURES`, `FUTURES LV2`) and options chains (no tool).

### 4. Exact next action for the user
1. Open the **Advanced Quotes Center** → **OpenAPI Advanced Quotes**:
   `https://www.webullapp.com/quote` (avatar, upper-right → *Advanced
   Quotes*). Webull's own step-by-step: `developer.webull.com/apis/docs/market-data-api/subscribe-quotes.md`.
   Confirm which **OpenAPI** line items show as active — you are looking
   for **Order Flow Non-display** (footprint) and, if you also want MNQ
   bars/depth, a **CME/CME Group** futures line.
2. If `get_futures_bars` on MNQZ6 still returns
   `MARKET_DATA_NOT_SUBSCRIBED:...US_FUTURES`, that is the CME futures
   subscription missing — **not** Order Flow. Add the CME Group bundle (or
   CBOT/CME/COMEX/NYMEX individually) if the futures layer matters.
3. If any OpenAPI line item you *paid for* is absent from that page, that
   is the propagation question to raise with Webull support, quoting the
   account and the purchase date. Do not send App Key / App Secret.

No further in-app "Order Flow" purchase is needed — it is already live on
this credential.

### Search paths tried for the MCP package (found: not installed locally)

| Path | Result |
|---|---|
| `/home/hlbot/webull-venv/.../site-packages/webull` | **Only** `webull_openapi_python_sdk-3.0.3` — the REST SDK, not the MCP server |
| `.venv`, `/home/hlbot/vision-venv`, `/tmp/obbtest`, all `site-packages` | no `webull_openapi_mcp`, no `formatters.py` |
| `/home/hlbot/.cache/pip`, `/home/hlbot/.cache/uv`, `~/.local/share/uv` | no Webull wheels/uv tools |
| `find / -iname "*webull*mcp*"`, `webull_openapi_mcp*`, `webull-openapi-mcp*` | **no matches** |
| npm/npx caches | none |

Conclusion: Cloud MCP is a **remote** server (`https://api.webull.com/mcp`);
no MCP source is installed here, so no local formatter/scopes inspection is
possible. What the local SDK *does* show: footprint maps to
`/market-data/{stocks,futures}/footprints/list` (v3). The SDK contains **no
entitlement enums** for `FOOTPRINT`/`US_FUTURES`/`FUTURES LV2` — those
strings are entirely server-side, which is why the entitlement model can
only be probed empirically.

### Re-auth: not informative, and not attempted

`mcp_auth` exists for the namespace. The MCP server log
(`.../anysphere.cursor-mcp/MCP user-webull...log`) shows a full
logout → re-register → OAuth callback completing at `07:50:19` UTC, then
`Successfully connected to streamableHttp server`. Footprint works a few
minutes later. Re-running `mcp_auth` would risk disturbing a **working**
session for no diagnostic gain, so it was **deliberately not re-run**.

### Console side (Task 3): not reachable without credentials

The Advanced Quotes Center page requires login; unauthenticated it renders
the public quotes page with a `Log in` / `Sign up` pair and no subscription
UI. Per the investigation's own rules, no login/passkey/captcha was
attempted. **The app/console entitlement list could not be read from here
and is unverified** — that is the one open gap, and item 1 above is exactly
how the user closes it.

## Entitlement state, corrected (2026-10-15, fourth pass — footprint characterized)

Append to the sections above; the "Entitlement identity" table still holds,
this refines it after dumping real footprint records.

| Layer | Fourth-pass state (live) |
|---|---|
| `get_futures_footprint` MNQZ6 / ESZ6 (S5, M1, M5, M30) | **WORKS** — per-price `buy_detail`/`sell_detail` ladders, `delay_minutes: 0` |
| `get_stock_footprint` SPY / QQQ / AAPL / NVDA | **WORKS** — same shape |
| `get_futures_bars` / `_tick` / `_snapshot` MNQZ6 | **still LOCKED** — `subscribe to US_FUTURES` (separate CME line, re-tested fresh this pass) |
| `get_futures_depth` MNQZ6 | **still LOCKED** — `subscribe to FUTURES LV2` (re-tested fresh) |
| Option chain (any `get_option*`) | **ABSENT** — the ~83-tool catalog has only `place_option_single_instruction` / `place_option_strategy_instruction`. No chain, no greeks, no OI. Real GEX is not computable here. |
| `get_stock_quotes` / `get_stock_tick` / `get_stock_noii_bars` / `get_stock_capital_flow` | **WORKS** — equity microstructure group (unchanged from pass 3) |

**Footprint shape (MNQZ6 M5, verbatim):** the payload is a list of
per-symbol groups `{symbol, instrument_id, delay_minutes, result:[bars]}`.
Each bar has exactly eight keys — `time`, `trading_session`, `total`,
`delta`, `buy_total`, `sell_total`, `buy_detail`, `sell_detail`. All
values are strings. `buy_detail`/`sell_detail` are `{price -> size}` maps
on the instrument tick grid (discrete, not bucketed), summing exactly to
`buy_total`/`sell_total`. **There is no OHLC** — so footprint cannot
substitute for the locked futures bars line. `delay_minutes` is 0; there
is no per-bar `real_time_required` flag (it is a request param, and
`real_time_required: true` does include the still-forming bar).

**Footprint history limits (measured):** no date-range or pagination
parameter exists. `count` is documented as max 1200 but the server
**clamps to 2000** (verified: `count=5000` and `count=100000` both
return exactly 2000). Reach is therefore `timespan × 2000`: MNQ 5m
≈ 9.2 days, MNQ 1m ≈ 1.4 days, MNQ 30m ≈ 62 days, SPY/QQQ 30m ≈ 222
days. Supported timespans are `S5/S15/M1/M5/M30` only (`D` returns
`UNSUPPORTED_TIMESPAN`). Both footprints are RTH-only in practice
(`trading_sessions: "RTH"`), with gaps `17:00–18:00 ET` daily and
weekend gaps.

**Methodological lesson (recorded deliberately).** A
`MARKET_DATA_NOT_SUBSCRIBED` error is **not** proof of structural
absence. Two earlier passes declared footprint "still absolutely
unavailable" from a single early error, and it was wrong — the
entitlement propagated. The correct procedure is to **re-test a layer
live before declaring it locked**, and to treat a stable *success* as
stronger evidence than a stable *error*. The one thing that remains
structurally absent (no option-chain tool) is absent because **no tool
exists in the catalog**, which is a different and checkable claim from
"the endpoint returned an error once."
