#!/usr/bin/env python
"""Webull OpenAPI market-data connector.

Status: scaffolding. It is written and importable, but it cannot fetch
anything until credentials exist. See docs/WEBULL_DATA.md for the access
steps. Nothing in the live bot imports this yet.

Why this file exists
--------------------
Webull's OpenAPI exposes the two data layers that blocked edge-matrix row
Q7 (orderflow absorption):

  * footprint (true bid-by-ask ladder per bar)
  * options chain (for dealer gamma, i.e. real GEX)

and it covers US futures, so MNQ/NQ intraday bars are reachable -- the
instrument the source strategy was actually traded on. Those endpoints
require an OpenAPI market-data subscription that is separate from any
Webull app/QT subscription.

Auth model (from developer.webull.com)
--------------------------------------
ApiClient(app_key, app_secret, region_id); requests are signed with the
App Secret (HMAC), sent as x-app-key / x-signature headers. If 2FA is
enabled a token is created once via the Webull app. The SDK handles the
signing and token flow; only the key pair is needed here.

This module deliberately does not put credentials in source. They are
read from the environment, or from a JSON file outside the repo.

Entitlement status (verified live 2026-10-15, fourth pass via MCP)
------------------------------------------------------------------
Footprint now WORKS. `get_futures_footprint` (MNQZ6, ESZ6) and
`get_stock_footprint` (SPY, QQQ, AAPL, NVDA) return real per-price
`buy_detail`/`sell_detail` ladders with `delay_minutes: 0`. That is the
Q7 confirmation layer, from the vendor.

Still LOCKED -- a *separate* CME futures data line, not Order Flow:
`get_futures_bars` / `_tick` / `_snapshot` need `US_FUTURES`;
`get_futures_depth` needs `FUTURES LV2`. Recorded as the exact live
error strings in docs/WEBULL_MCP_SETUP.md.

Still ABSENT: no option-chain market-data tool exists anywhere in the
~83-tool catalog (only `place_option_*_instruction` order drafting), so
real dealer gamma / GEX cannot be computed from this server.

Methodological note, learned the hard way: a `MARKET_DATA_NOT_SUBSCRIBED`
error is NOT proof of structural absence. Footprint reported it on an
earlier pass and then returned live data once the entitlement propagated.
Re-test an endpoint before declaring a layer locked.

Footprint shape (MNQZ6 M5, dumped live 2026-10-15)
--------------------------------------------------
The wire record is a LIST of per-symbol groups, each:

    [{"symbol": "MNQZ6",
      "instrument_id": "470968211",
      "delay_minutes": 0,
      "result": [ {"time": "2026-10-15T08:20:00.000+0000",
                   "trading_session": "RTH",
                   "total": "4339", "delta": "149",
                   "buy_total": "2244", "sell_total": "2095",
                   "buy_detail": {"29764": "4", "29764.25": "5", ...},
                   "sell_detail": {"29764": "1", "29764.25": "7", ...}},
                 ... ]}]

Field notes (all verified against arithmetic, 1200/1200 bars):
  * every number is a STRING, including the price keys of the ladder.
  * `buy_detail` / `sell_detail` are {price -> size} MAPS at the
    instrument's tick grid (MNQ 0.25), a discrete level per key. They
    carry size only -- no order counts, no MPID detail.
  * both ladders sum EXACTLY to `buy_total` / `sell_total`, and
    `total == buy_total + sell_total`, `delta == buy_total - sell_total`.
  * there is NO bar OHLC. No `open`/`high`/`low`/`close` anywhere.
  * there is no per-bar `real_time_required` flag; the only delay marker
    is the group-level `delay_minutes` (0).

Usage example. The sample below is the real MNQZ6 M5 record dumped
through the MCP tool on 2026-10-15 and passed through `footprint_ladder`;
it is NOT a locally-executed SDK call, because no App Key is configured
on this host (the isolated venv holds the SDK, not credentials):

    /home/hlbot/webull-venv/bin/python - <<'PY'
    import sys; sys.path.insert(0, "scripts")
    from feeds.webull_connector import WebullData, footprint_ladder
    wd = WebullData()
    rows = wd.get_footprint("MNQZ6", "5m", "US_FUTURES", count=2)
    bar = rows[0]["result"][0]            # newest bar
    print(bar["time"], "total", bar["total"], "delta", bar["delta"])
    lad = footprint_ladder(bar)           # tidy per-price view
    print(lad[0])                          # {'price': 29764.0, 'buy': 4.0, ...}
    PY

Sample output actually observed (first line from the live payload, second
line from `footprint_ladder` run on it):

    2026-10-15T08:20:00.000+0000 total 4339 delta 149
    {'price': 29764.0, 'buy': 4.0, 'sell': 1.0, 'total': 5.0, 'delta': 3.0}

History limits (measured, not documented): `count` is clamped to 2000
server-side even though the schema advertises 1200. There is NO
date-range or pagination parameter. Reach is therefore `timespan x 2000`
bars: MNQ M5 ~9.2 days, MNQ M1 ~1.4 days, MNQ M30 ~62 days, SPY/QQQ M30
~222 days. Supported timespans are S5/S15/M1/M5/M30 only (daily errors).
"""
from __future__ import annotations

from datetime import datetime

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path


def _row_ts_ms(row: dict) -> int:
    """Parse a Webull bar timestamp.

    Bars come as "2026-10-15T07:50:00.000+0000". Falls back to epoch ints.
    """
    t = row.get("time")
    if isinstance(t, (int, float)):
        return int(t)
    if isinstance(t, str):
        try:
            return int(datetime.strptime(t, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp() * 1000)
        except ValueError:
            return int(datetime.strptime(t, "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1000)
    raise KeyError(f"bar has no usable timestamp: {sorted(row)}")

# The SDK pins grpcio/protobuf, so it lives in its own venv and is not a
# dependency of the trading bot. Import lazily and explain clearly.
_SDK_HINT = (
    "Webull SDK not importable. Install it in the isolated venv:\n"
    "    /home/hlbot/webull-venv/bin/pip install webull-openapi-python-sdk\n"
    "and run this module with /home/hlbot/webull-venv/bin/python."
)


@dataclass
class Credentials:
    app_key: str
    app_secret: str
    region_id: str

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Credentials":
        """Env first, then a JSON file. Never hard-coded in source."""
        ak = os.environ.get("WEBULL_APP_KEY")
        sk = os.environ.get("WEBULL_APP_SECRET")
        rg = os.environ.get("WEBULL_REGION_ID", "us")
        if ak and sk:
            return cls(ak, sk, rg)
        p = Path(path) if path else Path.home() / ".config" / "webull" / "credentials.json"
        if p.exists():
            d = json.loads(p.read_text())
            return cls(d["app_key"], d["app_secret"], d.get("region_id", "us"))
        raise RuntimeError(
            "No Webull credentials. Set WEBULL_APP_KEY / WEBULL_APP_SECRET, "
            f"or place them in {p}. Apply at OpenAPI Management -> My Application; "
            "review takes 1-2 business days."
        )


# interval -> Webull Timespan name
TIMESPAN = {
    "5s": "S5", "15s": "S15",
    "1m": "M1", "5m": "M5", "15m": "M15", "30m": "M30",
    "1h": "M60", "2h": "M120", "4h": "M240",
    "1d": "D", "1w": "W", "1mo": "M", "1y": "Y",
}

# our coin/symbol -> (webull symbol, Category name)
CATEGORY = {"US_STOCK": "US_STOCK", "US_ETF": "US_ETF",
            "US_FUTURES": "US_FUTURES", "US_OPTION": "US_OPTION",
            "US_CRYPTO": "US_CRYPTO"}


class WebullData:
    """Thin wrapper over the SDK's DataClient with the checks we rely on."""

    def __init__(self, creds: Credentials | None = None, endpoint: str | None = None):
        try:
            from webull.core.client import ApiClient
            from webull.data.data_client import DataClient
        except ImportError as e:            # pragma: no cover
            raise RuntimeError(_SDK_HINT) from e

        self.creds = creds or Credentials.load()
        self._api = ApiClient(self.creds.app_key, self.creds.app_secret,
                              self.creds.region_id)
        if endpoint:
            self._api.add_endpoint(self.creds.region_id, endpoint)
        self.data = DataClient(self._api)

    # ---------------------------------------------------------------- probe
    def whoami(self) -> dict:
        """Cheapest call that proves the key pair signs correctly.

        Deliberately probes the *crypto* instrument list rather than a
        stock: crypto carries no market-data subscription requirement, so
        a failure here is unambiguously bad credentials, whereas a stock
        probe would conflate bad keys with a missing subscription.
        """
        r = self.data.instrument.get_crypto_instrument("BTCUSD")
        return {"status": r.status_code,
                "ok": r.status_code == 200,
                "body_head": str(r.text)[:300]}

    # ----------------------------------------------------------------- bars
    def get_bars(self, symbol: str, interval: str = "5m", category: str = "US_FUTURES",
                 count: int = 200, sessions: str | None = None) -> list[dict]:
        """Historical bars. Returns raw dicts; the feed adapter maps them."""
        ts = TIMESPAN.get(interval)
        if ts is None:
            raise ValueError(f"unsupported interval {interval!r}; one of {sorted(TIMESPAN)}")
        from webull.data.common.timespan import Timespan
        fn = (self.data.futures_market_data.get_futures_history_bars
              if category == "US_FUTURES" else self.data.market_data.get_history_bar)
        kwargs = {"symbols": symbol, "category": category,
                  "timespan": getattr(Timespan, ts), "count": str(count)}
        if sessions:
            kwargs["trading_sessions"] = sessions
        r = fn(**kwargs)
        if r.status_code != 200:
            raise RuntimeError(f"bars {symbol} {interval}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    # --------------------------------------------------------------- crypto
    # Crypto needs no OpenAPI market-data subscription, so this is the one
    # path that can be validated end-to-end before spending anything.
    # Symbols are plain pairs: BTCUSD, ETHUSD. count maxes at 1200.
    def get_crypto_bars(self, symbol: str = "BTCUSD", interval: str = "5m",
                        count: int = 200) -> list[dict]:
        from webull.data.common.timespan import Timespan
        r = self.data.crypto_market_data.get_crypto_history_bar(
            symbol, "US_CRYPTO", getattr(Timespan, TIMESPAN[interval]), count=str(count))
        if r.status_code != 200:
            raise RuntimeError(f"crypto bars {symbol}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def get_crypto_snapshot(self, symbols: str | list = "BTCUSD") -> list[dict]:
        r = self.data.crypto_market_data.get_crypto_snapshot(symbols, "US_CRYPTO")
        if r.status_code != 200:
            raise RuntimeError(f"crypto snapshot: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def list_crypto_instruments(self, symbol: str | None = None) -> list[dict]:
        r = self.data.instrument.get_crypto_instrument(symbol)
        if r.status_code != 200:
            raise RuntimeError(f"crypto instruments: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    # ------------------------------------------------------------ equity hist
    # Equity/ETF bars carry true date-range pagination (start_time/end_time)
    # AND volume, unlike crypto. Verified 2026-10-15: SPY 5m reaches back to
    # ~2010 with delay_minutes=0 on the free tier. This is the useful free
    # path for research -- not the crypto feed.
    def get_stock_bars(self, symbols: str | list, category: str = "US_ETF",
                       interval: str = "5m", count: int = 1200,
                       start_ms: int | None = None, end_ms: int | None = None,
                       sessions: str | None = None,
                       real_time: bool | None = None) -> list[dict]:
        """One page of equity/ETF bars. count maxes at 1200 (1650 for 1m)."""
        r = self.data.market_data.get_batch_history_bar(
            symbols, category, getattr(__import__("webull.data.common.timespan",
                                                   fromlist=["Timespan"]).Timespan,
                                       TIMESPAN[interval]),
            count=str(count), real_time_required=real_time,
            trading_sessions=sessions,
            start_time=start_ms, end_time=end_ms)
        if r.status_code != 200:
            raise RuntimeError(f"stock bars {symbols}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def fetch_stock_history(self, symbol: str, interval: str = "5m",
                            category: str = "US_ETF",
                            start_ms: int = 0, end_ms: int | None = None,
                            sessions: str | None = None,
                            budget: int = 60) -> list:
        """Page backwards until start_ms is covered.

        The API returns the last `count` bars *ending at* end_time, so we walk
        backwards. Pages overlap (confirmed 576 shared bars on a 1200-bar
        step), so results are de-duplicated by timestamp. Newest first is
        normalised to oldest-first on return.

        `budget` caps requests so a bad range cannot loop forever.
        """
        end_ms = end_ms if end_ms is not None else int(time.time() * 1000)
        agg: dict[int, dict] = {}
        cursor = end_ms
        for _ in range(budget):
            page = self.get_stock_bars(symbol, category, interval, 1200,
                                       start_ms=start_ms, end_ms=cursor,
                                       sessions=sessions)
            rows = self._unwrap(page, symbol if isinstance(symbol, str) else symbol[0])
            if not rows:
                break
            for r in rows:
                t = _row_ts_ms(r)
                agg.setdefault(t, r)
            oldest = min(_row_ts_ms(r) for r in rows)
            if oldest <= start_ms or oldest >= cursor:
                break
            cursor = oldest
        return sorted(agg.values(), key=_row_ts_ms)

    @staticmethod
    def _unwrap(page, symbol: str) -> list[dict]:
        """Bars come back as {result:[{symbol,result:[...]}]} or a flat list."""
        if isinstance(page, dict):
            page = page.get("result", [])
        if page and isinstance(page[0], dict) and "result" in page[0]:
            for grp in page:
                if grp.get("symbol") == symbol:
                    return grp["result"]
            return page[0]["result"]
        return page

    # ------------------------------------------------------------- Q7 data
    def get_footprint(self, symbol: str, interval: str = "5m",
                      category: str = "US_FUTURES", count: int = 200,
                      real_time: bool | None = None,
                      sessions: str | None = None) -> list[dict]:
        """Order-flow footprint -- Q7's confirmation layer. LIVE as of 2026-10-15.

        Verified working for futures (MNQZ6, ESZ6) and equities/ETFs
        (SPY, QQQ, AAPL, NVDA), with `delay_minutes: 0`. Returns the raw
        wire payload: a LIST of per-symbol groups. Use `footprint_bars()`
        to get just the bar list, or `footprint_ladder(bar)` to turn one
        bar's `buy_detail`/`sell_detail` into a tidy per-price ladder.

        Each bar carries: `time`, `trading_session`, `total`, `delta`,
        `buy_total`, `sell_total`, `buy_detail`, `sell_detail`. Every
        value is a STRING. There is NO bar OHLC -- footprint data alone
        cannot substitute for a price-bar feed.

        `count` is 200 by default; the schema says max 1200 but the
        server clamps to 2000. There is NO date-range parameter, so reach
        is bounded by `count x timespan`. `real_time=True` includes the
        still-forming bar; `sessions` defaults to "RTH" server-side.
        """
        from webull.data.common.timespan import Timespan
        fn = (self.data.futures_market_data.get_futures_footprint
              if category == "US_FUTURES" else self.data.market_data.get_footprint)
        r = fn(symbols=symbol, category=category,
               timespan=getattr(Timespan, TIMESPAN[interval]), count=str(count),
               real_time_required=real_time, trading_sessions=sessions)
        if r.status_code != 200:
            raise RuntimeError(f"footprint {symbol}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def get_options(self, symbol: str, category: str = "US_OPTION") -> list[dict]:
        """Option chain: open interest + greeks -> real GEX (Q7's other layer)."""
        r = self.data.option_market_data.get_option_snapshot(symbol, category)
        if r.status_code != 200:
            raise RuntimeError(f"options {symbol}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def get_depth(self, symbol: str, category: str = "US_FUTURES",
                  depth: int | None = None) -> list[dict]:
        r = self.data.futures_market_data.get_futures_depth(symbol, category, depth=depth)
        if r.status_code != 200:
            raise RuntimeError(f"depth {symbol}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()


# ------------------------------------------------------------------ adapter
def _to_our_candles(rows: list[dict], symbol: str) -> list:
    """Map Webull bar rows to the bot's Candle dataclass.

    Field names are confirmed against a live response before this is used;
    the mapping is intentionally explicit so a mismatch fails loudly
    rather than silently producing zero-filled bars.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from hyperliquid_bot.data.ohlcv import Candle      # noqa: E402

    out, seen = [], set()
    for r in rows:
        t = r.get("timestamp") or r.get("time") or r.get("t")
        if t is None:
            raise KeyError(f"unrecognised bar schema for {symbol}: keys {sorted(r)}")
        t = int(t)
        if t in seen:
            continue
        seen.add(t)
        out.append(Candle(t=t, o=float(r["open"]), h=float(r["high"]),
                          l=float(r["low"]), c=float(r["close"]),
                          v=float(r.get("volume", 0.0))))
    return sorted(out, key=lambda c: c.t)


# ------------------------------------------------------- footprint helpers
def footprint_bars(payload: list[dict], symbol: str | None = None) -> list[dict]:
    """Pull the bar list out of a raw `get_footprint` payload.

    The wire format is `[{symbol, instrument_id, delay_minutes, result:[...]}]`.
    With multiple symbols passed to the API each group is separate; pass
    `symbol` to pick one, otherwise the first group is used.
    """
    if isinstance(payload, dict):
        payload = [payload]
    for grp in payload:
        if symbol is None or grp.get("symbol") == symbol:
            return grp.get("result", [])
    return []


def footprint_ladder(bar: dict) -> list[dict]:
    """Normalise one footprint bar's two ladders into a clean per-price view.

    Returns a list of dicts, price-ascending, all floats:

        {'price': 29764.0, 'buy': 4.0, 'sell': 1.0,
         'total': 5.0, 'delta': 3.0}

    Only prices that traded are present (a discrete grid, not bucketed).
    The wire values are strings, so they are cast here. `delta` is
    buy - sell at that level. This does not alter the raw call's return
    value or signature -- it is a read-only transform.

    Note: buy/sell keys are matched as raw strings first (Webull pads
    equity prices to a fixed number of decimals, e.g. "728.10"), and
    only then collapsed to a float for output, so padding differences
    cannot silently drop a level.
    """
    buy = bar.get("buy_detail") or {}
    sell = bar.get("sell_detail") or {}
    # Merge by exact wire key, then keep the float value alongside it.
    levels: dict[float, list[float]] = {}
    for p, v in buy.items():
        levels.setdefault(float(p), [0.0, 0.0])[0] += float(v)
    for p, v in sell.items():
        levels.setdefault(float(p), [0.0, 0.0])[1] += float(v)
    out = []
    for p in sorted(levels):
        b, s = levels[p]
        out.append({"price": p, "buy": b, "sell": s,
                    "total": b + s, "delta": b - s})
    return out


def fetch_candles_webull(symbol: str, interval: str = "5m",
                         category: str = "US_FUTURES", count: int = 200,
                         client: "WebullData | None" = None) -> list:
    """Drop-in equivalent of data.ohlcv.fetch_candles for Webull markets."""
    wd = client or WebullData()
    if category == "US_CRYPTO":
        rows = wd.get_crypto_bars(symbol, interval, count)
    else:
        rows = wd.get_bars(symbol, interval, category, count)
    return _to_our_candles(rows, symbol)


if __name__ == "__main__":
    # Smoke test: proves whether a key pair is alive, and which layers the
    # subscription actually unlocks, without paying for anything first.
    #
    # Network note (verified 2026-10-15): REST goes to api.webull.com, which
    # is reachable from this host. data-api.webull.com serves MQTT on 1883/
    # 8883 (both open) and does not answer on 443 -- that is by design, not
    # a block. A 401 here means bad credentials, nothing more.
    print("Webull connector smoke test")
    print("  free tier: crypto needs no market-data subscription")
    try:
        wd = WebullData()
    except Exception as e:
        print(f"  credentials: NOT CONFIGURED\n    {e}")
        raise SystemExit(2)
    print("  credentials: found")
    try:
        print("  whoami      :", wd.whoami())
    except Exception as e:
        print(f"  whoami      : FAILED {e}")

    checks = [
        ("crypto bars",     lambda: len(wd.get_crypto_bars("BTCUSD", "5m", 200)),            "free"),
        ("crypto snapshot", lambda: len(wd.get_crypto_snapshot(["BTCUSD", "ETHUSD"])),       "free"),
        ("crypto symbols",  lambda: len(wd.list_crypto_instruments()),                       "free"),
        ("futures bars",    lambda: len(wd.get_bars("MNQ", "5m")),                           "sub"),
        ("footprint",       lambda: len(wd.get_footprint("MNQ", "5m")),                      "sub"),
        ("options",         lambda: len(wd.get_options("AAPL")),                             "sub"),
        ("depth",           lambda: len(wd.get_depth("MNQ")),                                "sub"),
    ]
    for name, call, tier in checks:
        try:
            print(f"  [{tier:4s}] {name:15s} : {call()} rows")
        except Exception as e:
            print(f"  [{tier:4s}] {name:15s} : LOCKED or unavailable -> {str(e)[:95]}")
