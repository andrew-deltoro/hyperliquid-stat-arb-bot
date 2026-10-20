# Connecting Webull OpenAPI

Written 2026-10-15. The goal is market data, not order placement.

## The one thing to understand first

There are two independent gates, and a paid Webull app subscription
opens **neither** by itself:

1. **OpenAPI access** — self-serve, free, takes 1–2 business days. Gives
   you an App Key + App Secret.
2. **An OpenAPI market-data subscription** — paid, and bought
   separately. Webull's own wording:

   > Market data subscriptions purchased through the Webull mobile app
   > or desktop platform (QT) are independent of OpenAPI. You need a
   > separate subscription specifically enabled for OpenAPI usage.

So the practical first step is checking the Advanced Quotes Center price
list before committing, because that is the real cost.

## Step 1 — apply for OpenAPI access

1. Log in at the Webull portal, then `OpenAPI Management → My
   Application`, and apply. An opened brokerage account is required
   first.
2. Wait for review (1–2 working days).
3. Generate the key pair. This needs SMS **and** an MFA code, so it has
   to be done by you in a browser — it cannot be scripted.

## Step 2 — decide what to subscribe to

| Data | Subscription needed | Unlocks here |
|---|---|---|
| Crypto | **none** | free end-to-end test of the connector |
| US futures | CBOT / CME / COMEX / NYMEX individually, or CME Group bundle | MNQ/NQ bars — the instrument Q7 was written for |
| Order Flow | Order Flow Non-display | footprint + time-price-opportunity — Q7's confirmation layer |
| US options | OPRA Real-Time Non-display | chains + greeks → real GEX — Q7's context layer |
| Stocks/ETFs | Nasdaq Basic (L1) or Nasdaq TotalView (L2) | equity bars and depth |

Two constraints worth knowing: only **one device** may use L1/L2 data at
a time, and the sandbox serves 15-minute delayed data until a real
subscription exists.

## Step 3 — install credentials (not in the repo)

The connector reads credentials from the environment, or from
`~/.config/webull/credentials.json`:

```json
{"app_key": "...", "app_secret": "...", "region_id": "us"}
```

or

```bash
export WEBULL_APP_KEY=... WEBULL_APP_SECRET=... WEBULL_REGION_ID=us
```

Do not put either into the repository. An App Secret is a bearer
credential for your brokerage account.

## Step 4 — prove what works before paying

```bash
/home/hlbot/webull-venv/bin/python scripts/feeds/webull_connector.py
```

The smoke test reports, per layer, either a row count or `LOCKED`, and
tags each layer `free` or `sub` so the crypto path is obvious. Run it once
before subscribing and once after: the difference tells you exactly what
money bought, rather than assuming.

Crypto is the one category with no OpenAPI subscription requirement, so
`crypto bars` reaching a live row count proves auth, signing, endpoint
resolution, and the `Candle` mapping all work — for $0.

## What crypto does and does not buy us

Crypto is worth doing first, but for the right reason: it proves the
plumbing, not the research.

**Does:** validates auth, HMAC signing, endpoint resolution, the
`Candle` mapping, and the error taxonomy, at zero cost. It also gives a
second venue's BTC/ETH bars, which supports cross-venue basis work
(Webull vs Hyperliquid).

**Does not:** upgrade our crypto research. Two documented limits:

- `count` maxes at **1200 bars**. At 5 minutes that is ~4.2 days, versus
  Hyperliquid's own ~17.4 days of 5m history. It is *less* history, not
  more, and there is no pagination parameter on the crypto endpoint.
- `get_crypto_history_bar` returns only **intraday** candles by default;
  the docs note daily-and-above come weight-adjusted while minute bars
  come unweighted, which matters if the two are ever mixed.

So the crawl-back problem from Q5 is not solved by Webull crypto. The
feeds that would actually move a day-trade row (footprint, depth, option
chains, MNQ bars) are all in the paid tiers.

## Network reachability (verified 2026-10-15)

Worth recording, because a first reading of the hostnames suggests a
block that is not there:

| Host | Port | State |
|---|---|---|
| `api.webull.com` | 443 | **OPEN** — this is where every REST call goes |
| `api.webull.com` | 80 | OPEN |
| `data-api.webull.com` | 1883, 8883 | **OPEN** — MQTT streaming, TLS verified |
| `data-api.webull.com` | 443 | closed |
| `api.sandbox.webull.com` | 443 | OPEN (test environment) |
| `data-api.sandbox.webull.com` | 443 | closed |

`data-api.webull.com` resolves to an ELB that serves the MQTT broker on
1883/8883 and does not answer on 443. That is its design, **not** an
egress restriction. All SDK REST requests resolve to `api.webull.com`
(confirmed by calling `ApiClient._resolve_endpoint`), which is reachable.

A live signed request with placeholder credentials returns
`HTTP 401 UNAUTHORIZED / Invalid credentials`. That is the useful
baseline: networking and endpoint selection are correct, and credentials
are the only remaining gate. A `401` after real keys means the keys are
wrong, not the connection.

## Environment note

The SDK pins `grpcio` and `protobuf`, so it lives in its own venv at
`/home/hlbot/webull-venv`. The bot's `.venv` is deliberately left
without it, so no dependency pin is disturbed. This is why the connector
is invoked with the Webull venv's Python.

## What is already built

`scripts/feeds/webull_connector.py`:

- `Credentials` — env-or-file loader, never hard-coded.
- `WebullData.whoami()` — cheapest signed call, separates "bad key" from
  "no subscription".
- `get_crypto_bars` / `get_crypto_snapshot` / `list_crypto_instruments`
  — the free tier, no subscription required.
- `get_bars` / `get_footprint` / `get_options` / `get_depth`.
- `fetch_candles_webull` — returns the bot's own `Candle` type, so it is
  a drop-in for `ohlcv.fetch_candles`, and routes `US_CRYPTO` to the
  crypto client automatically.

Nothing in the live bot imports it yet. Wiring it into the pairs bot is
a separate decision and has not been made.

## Second pass: re-tested after a subscription was activated (2026-10-15)

An OpenAPI subscription was activated and every layer re-exercised live.
Result: **the equity microstructure group turned on; the layers Q7 needs
did not.** `get_stock_quotes` now returns full priced L1/L2 ladders
(`depth=1/5/50`), and `get_stock_tick`, `get_stock_noii_bars`,
`get_stock_capital_flow` return live data — an L1/L2 equity entitlement.
But `get_futures_footprint` and `get_stock_footprint` still fail with
`MARKET_DATA_NOT_SUBSCRIBED:Insufficient permission, please subscribe to
FOOTPRINT`; futures bars/tick/snapshot still need `US_FUTURES` and
futures depth still needs `FUTURES LV2`; and there is **no option-chain
market-data tool** in the catalog at all. So footprint and GEX — the two
Q7 blockers — both remain shut. See `WEBULL_MCP_SETUP.md` for the full
before/after table.

## SPY 1m RTH freeze attempt — BLOCKED, nothing harvested (2026-10-15)

The goal was to freeze the deepest available SPY 1-minute RTH panel
(1m is known to reach ~mid-2021) to
`reports/.webull_cache/SPY_1m_RTH_frozen.json`, reporting **structure
only** (counts and timestamps), per the no-peeking rule at the top of
`DAY_TRADE_EDGE_MATRIX.md`.

**Result: zero bars pulled. No panel file exists, so no structure report
is possible.** This is a partial/empty freeze with a clear boundary, not
a complete span. No price, return, VWAP, deviation, decile or performance
statistic was computed — there was no data to compute it from.

### Why it is blocked: both access routes are down

The Webull OpenAPI is reachable here two ways, and **neither works right
now**:

| Route | State (2026-10-15, ~17:30–17:36 UTC) | Evidence |
|---|---|---|
| Python SDK via `/home/hlbot/webull-venv` (`WebullData.fetch_stock_history`) | **BLOCKED — no credentials** | `Credentials.load()` raises `RuntimeError: No Webull credentials`. No `WEBULL_APP_KEY`/`WEBULL_APP_SECRET` in env; no `~/.config/webull/credentials.json`; no creds anywhere on the host (searched `/home/hlbot`, `~/.config`, `~/.cursor-server`, pip cache, systemd units, all `.env` files). |
| Cloud MCP `user-webull` (`get_stock_bars`) | **BLOCKED — OAuth token expired** | Every call returns `HTTP 403 {"code":"auth.token.expire"}`. `mcp_auth` reports success but re-issues no working token (several attempts). Server log: earlier session at 07:50 UTC was healthy; by 17:30 UTC the token is `auth.token.expire` on all tools, including non-market-data ones. |

`api.webull.com:443` is reachable (returns HTTP 404 on `/mcp`), so this
is an **entitlement/token** problem, not a network one.

### What this is NOT

A `403 auth.token.expire` is **not** proof the 1m endpoint is gone — it
is the credential layer failing before the data layer is reached. Per the
methodological note recorded earlier in this file, an entitlement error
must be re-tested before a layer is declared locked. So the ~mid-2021 1m
floor (recorded earlier as "SPY 1m history back to ~mid-2021") is
**neither confirmed nor refuted by this pass** — it was never queried.

### Unblocking it

Either:

1. **SDK path (preferred for a ~200–400-page bulk walk).** Supply OpenAPI
   credentials outside the repo (env vars, or
   `~/.config/webull/credentials.json`), then run the harvest script
   below with the isolated interpreter. This is the established pattern;
   do not install the SDK anywhere new and never into the repo `.venv`.
2. **MCP path.** Complete a fresh browser OAuth consent for the
   `user-webull` namespace so a live token is issued, then harvest pages
   via `get_stock_bars` (M1, `US_ETF`, `trading_sessions=RTH`,
   `start_time`/`end_time` paging) and merge them with
   `--source pages`.

### Harvest script (written this pass, runnable once access returns)

`scripts/feeds/freeze_spy_1m.py` — reusable, idempotent (checkpointed
per page; re-running resumes or no-ops), reports structure only.

```bash
# SDK path (needs Webull credentials outside the repo):
/home/hlbot/webull-venv/bin/python \
  /home/hlbot/hyperliquid-stat-arb-bot/scripts/feeds/freeze_spy_1m.py
#   -> reports/.webull_cache/SPY_1m_RTH_frozen.json

# MCP-page path (merge raw get_stock_bars dumps from disk):
/home/hlbot/webull-venv/bin/python scripts/feeds/freeze_spy_1m.py \
  --source pages --pages-dir reports/.webull_cache/pages
```

The cache path is already gitignored (`.gitignore` line 62,
`reports/.webull_cache/`). Files written this pass: the script only —
**no data file was created**, because no data could be fetched.
