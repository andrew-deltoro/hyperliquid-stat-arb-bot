"""Track Kalshi 15-minute contracts on their own clock.

Writes state/kalshi_contract_tape.jsonl. Does not open
hl_microstructure.jsonl and does not touch the microstructure service.

For each window the process was already watching at the open:
  - average Hyperliquid top-5 imbalance over the first 60 seconds
  - at second 60, record the yes and no asks
  - after the close, record the official yes/no result

A window discovered late is a miss. The quote from later in the window
is not used.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import websockets
import yaml

ROOT = Path("/home/hlbot/hyperliquid-stat-arb-bot")
sys.path.insert(0, str(ROOT / "src"))

from hyperliquid_bot.kalshi_hypothesis import taker_fee  # noqa: E402
CONFIG_PATH = ROOT / "config" / "kalshi_hl_hypothesis.yaml"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
HL_WS = "wss://api.hyperliquid.xyz/ws"
TOP_N = 5


def _utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _f(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ofi_from_levels(levels) -> tuple[float, float, float, float] | None:
    """Top-5 dollar imbalance, same definition as the microstructure recorder.

    Returns (ofi, mid, spread_bps, bid_depth_usd).
    """
    if not levels or len(levels) < 2:
        return None
    bids, asks = levels[0], levels[1]
    if not bids or not asks:
        return None
    bid = float(bids[0]["px"])
    ask = float(asks[0]["px"])
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    bid_depth = sum(float(b["px"]) * float(b["sz"]) for b in bids[:TOP_N])
    ask_depth = sum(float(a["px"]) * float(a["sz"]) for a in asks[:TOP_N])
    total = bid_depth + ask_depth
    ofi = (bid_depth - ask_depth) / total if total > 0 else 0.0
    spread_bps = (ask - bid) / mid * 10000.0
    return ofi, mid, spread_bps, bid_depth


@dataclass
class Book:
    ofi: float = 0.0
    mid: float = 0.0
    spread_bps: float = 0.0
    ready: bool = False
    trades: list[tuple[float, str, float]] = field(default_factory=list)


@dataclass
class Window:
    series: str
    coin: str
    in_test: bool
    ticker: str | None
    open_time: datetime
    close_time: datetime
    floor_strike: float | None
    samples: list[tuple[datetime, float]] = field(default_factory=list)
    trade_prints: list[tuple[datetime, str, float]] = field(default_factory=list)
    collecting: bool = False
    covered_open: bool = False
    decision_written: bool = False
    settlement_written: bool = False


class Cycle:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        runtime = Path(os.environ.get("HLBOT_RUNTIME_DIR", "/home/hlbot/hlbot-runtime/live"))
        self.tape = Path(cfg["outputs"]["tape"])
        self.state_path = runtime / "state" / "kalshi_contract_state.json"
        self.tape.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.books: dict[str, Book] = {}
        self.windows: dict[str, Window] = {}
        self.done: set[str] = set()
        self.series_meta: dict[str, dict] = {}
        self._load_done()
        specs = [{"series": cfg["test"]["series"], "coin": cfg["test"]["coin"], "in_test": True}]
        for extra in cfg.get("record_also") or []:
            specs.append({"series": extra["series"], "coin": extra["coin"], "in_test": False})
        self.specs = specs
        for spec in specs:
            self.books[spec["coin"]] = Book()

    def _load_done(self) -> None:
        if not self.tape.exists():
            return
        decisions: dict[str, dict] = {}
        with self.tape.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                ticker = row.get("ticker")
                if not ticker:
                    continue
                if row.get("type") == "decision":
                    self.done.add(f"d:{ticker}")
                    decisions[ticker] = row
                elif row.get("type") == "settlement":
                    self.done.add(f"s:{ticker}")
        for ticker, row in decisions.items():
            if f"s:{ticker}" in self.done:
                continue
            self.windows[ticker] = Window(
                series=row["series"],
                coin=row["coin"],
                in_test=bool(row.get("in_test")),
                ticker=ticker,
                open_time=_utc(row["open_time"]),
                close_time=_utc(row["close_time"]),
                floor_strike=row.get("floor_strike"),
                decision_written=True,
            )

    def append(self, row: dict) -> None:
        with self.tape.open("a") as handle:
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def on_book(self, coin: str, levels, ts_ms: int) -> None:
        parsed = ofi_from_levels(levels)
        if parsed is None:
            return
        ofi, mid, spread_bps, _ = parsed
        book = self.books[coin]
        book.ofi = ofi
        book.mid = mid
        book.spread_bps = spread_bps
        book.ready = True
        when = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc) if ts_ms else _now()
        for window in self.windows.values():
            if window.coin != coin or not window.collecting:
                continue
            if window.open_time <= when < window.open_time + timedelta(seconds=self.cfg["signal"]["window_sec"]):
                window.samples.append((when, ofi))

    def on_trades(self, coin: str, trades: list) -> None:
        for trade in trades:
            try:
                when = datetime.fromtimestamp(int(trade.get("time", 0)) / 1000.0, tz=timezone.utc)
                side = str(trade.get("side", ""))
                size = float(trade.get("sz", 0))
            except (TypeError, ValueError):
                continue
            for window in self.windows.values():
                if window.coin != coin or not window.collecting:
                    continue
                end = window.open_time + timedelta(seconds=self.cfg["signal"]["window_sec"])
                if window.open_time <= when < end:
                    window.trade_prints.append((when, side, size))

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        try:
            response = requests.get(f"{KALSHI}{path}", params=params or {}, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            print(f"[KALSHI] {path} failed: {exc}", flush=True)
            return None

    def active_market(self, series: str) -> dict | None:
        payload = self._get("/markets", {"series_ticker": series, "status": "open", "limit": 20})
        if not payload:
            return None
        now_iso = _now().isoformat()
        markets = payload.get("markets") or []
        active = [
            market for market in markets
            if (market.get("open_time") or "") <= now_iso < (market.get("close_time") or "")
        ]
        if not active:
            return None
        active.sort(key=lambda market: market.get("open_time") or "")
        return active[-1]

    def arm_from_clock(self) -> None:
        """Start the next window at the previous close, before Kalshi lists it.

        The exchange publishes the new contract at its open. Waiting for that
        response is what marked live windows as missed. The book sample does
        not need the ticker. The ticker is attached when the listing arrives.
        """
        now = _now()
        cover = timedelta(seconds=self.cfg["signal"]["must_cover_from_sec"])
        for window in list(self.windows.values()):
            nxt = window.close_time
            if not (nxt - timedelta(seconds=2) <= now <= nxt + cover):
                continue
            self._arm_placeholder(window.series, window.coin, window.in_test, nxt)
        for window in self.windows.values():
            if window.decision_written or window.collecting:
                continue
            if window.open_time <= now <= window.open_time + cover:
                window.collecting = True
                window.covered_open = True
                label = window.ticker or window.open_time.isoformat()
                print(f"[WINDOW] collecting {window.series} from {label}", flush=True)

    def _arm_placeholder(self, series: str, coin: str, in_test: bool, open_time: datetime) -> None:
        for window in self.windows.values():
            if window.series == series and window.open_time == open_time:
                return
        key = f"pending:{series}:{open_time.isoformat()}"
        self.windows[key] = Window(
            series=series,
            coin=coin,
            in_test=in_test,
            ticker=None,
            open_time=open_time,
            close_time=open_time + timedelta(minutes=15),
            floor_strike=None,
        )

    def ensure_window(self, spec: dict, market: dict) -> None:
        ticker = market.get("ticker")
        if not ticker:
            return
        open_time = _utc(market["open_time"])
        for key, window in list(self.windows.items()):
            if window.series == spec["series"] and window.open_time == open_time and window.ticker is None:
                window.ticker = ticker
                window.floor_strike = _f(market.get("floor_strike"))
                self.windows[ticker] = window
                if key != ticker:
                    self.windows.pop(key, None)
                print(f"[WINDOW] bound {ticker}", flush=True)
                return
        if ticker in self.windows:
            return
        if f"d:{ticker}" in self.done and f"s:{ticker}" in self.done:
            return
        window = Window(
            series=spec["series"],
            coin=spec["coin"],
            in_test=spec["in_test"],
            ticker=ticker,
            open_time=_utc(market["open_time"]),
            close_time=_utc(market["close_time"]),
            floor_strike=_f(market.get("floor_strike")),
        )
        now = _now()
        cover_by = window.open_time + timedelta(seconds=self.cfg["signal"]["must_cover_from_sec"])
        if now <= cover_by:
            window.collecting = True
            window.covered_open = True
            print(f"[WINDOW] watching {ticker} open={window.open_time.isoformat()}", flush=True)
        else:
            print(f"[WINDOW] late for {ticker}, decision will be a miss", flush=True)
        self.windows[ticker] = window

    def _taker_ratio(self, window: Window) -> float | None:
        buy = sum(size for _, side, size in window.trade_prints if side == "B")
        sell = sum(size for _, side, size in window.trade_prints if side == "A")
        total = buy + sell
        if total <= 0:
            return None
        return buy / total

    def _quote(self, ticker: str) -> dict | None:
        payload = self._get(f"/markets/{ticker}")
        if not payload:
            return None
        return payload.get("market") or payload

    def write_decision(self, window: Window, side: str, reason: str, quote: dict | None, ofi_mean: float | None) -> None:
        yes_bid = _f((quote or {}).get("yes_bid_dollars"))
        yes_ask = _f((quote or {}).get("yes_ask_dollars"))
        no_bid = _f((quote or {}).get("no_bid_dollars"))
        no_ask = _f((quote or {}).get("no_ask_dollars"))
        price = yes_ask if side == "yes" else no_ask if side == "no" else None
        book = self.books[window.coin]
        series = self.series_meta.get(window.series)
        if series is None:
            fee_meta = self._get(f"/series/{window.series}") or {}
            series = fee_meta.get("series") or {}
            self.series_meta[window.series] = series
        row = {
            "type": "decision",
            "hypothesis_id": self.cfg["hypothesis_id"],
            "ticker": window.ticker,
            "series": window.series,
            "coin": window.coin,
            "in_test": window.in_test,
            "open_time": window.open_time.isoformat(),
            "close_time": window.close_time.isoformat(),
            "decision_time": _now().isoformat(),
            "floor_strike": window.floor_strike,
            "side": side,
            "reason": reason,
            "ofi_mean": None if ofi_mean is None else round(ofi_mean, 6),
            "ofi_n": len(window.samples),
            "taker_ratio_first_minute": self._taker_ratio(window),
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "price": price,
            "contracts": int(self.cfg["execution"]["contracts"]),
            "fee_rate": float(self.cfg["execution"]["fee_rate"]),
            "fee_multiplier": _f(series.get("fee_multiplier")) or 1.0,
            "fee_type": series.get("fee_type"),
            "hl_mid": round(book.mid, 4) if book.ready else None,
            "hl_spread_bps": round(book.spread_bps, 3) if book.ready else None,
        }
        if side in ("yes", "no") and price is not None and 0.0 < price < 1.0:
            row["fee"] = taker_fee(price, contracts=row["contracts"], multiplier=row["fee_multiplier"], rate=row["fee_rate"])
        self.append(row)
        self.done.add(f"d:{window.ticker}")
        window.decision_written = True
        window.collecting = False
        print(f"[DECISION] {window.ticker} side={side} ofi={row['ofi_mean']} price={price} ({reason})", flush=True)

    def maybe_decide(self, window: Window) -> None:
        if window.decision_written or f"d:{window.ticker}" in self.done:
            window.decision_written = True
            window.collecting = False
            return
        now = _now()
        decision_at = window.open_time + timedelta(seconds=self.cfg["decision"]["offset_sec"])
        grace = timedelta(seconds=self.cfg["decision"]["grace_sec"])
        if now < decision_at:
            return
        if window.ticker is None:
            if now > decision_at + grace:
                window.decision_written = True
                window.settlement_written = True
                window.collecting = False
            return
        if not window.covered_open:
            self.write_decision(window, "miss", "discovered_after_open", None, None)
            return
        if now > decision_at + grace:
            self.write_decision(window, "miss", "quote_grace_elapsed", None, None)
            return
        samples = [ofi for when, ofi in window.samples if window.open_time <= when < decision_at]
        ofi_mean = sum(samples) / len(samples) if samples else None
        if ofi_mean is None or len(samples) < int(self.cfg["signal"]["min_samples"]):
            self.write_decision(window, "flat", "too_few_book_samples", None, ofi_mean)
            return
        if self.cfg["signal"].get("flat_if_zero", True) and ofi_mean == 0.0:
            self.write_decision(window, "flat", "ofi_zero", None, ofi_mean)
            return
        quote = self._quote(window.ticker)
        if not quote:
            return
        side = "yes" if ofi_mean > 0 else "no"
        price = _f(quote.get("yes_ask_dollars") if side == "yes" else quote.get("no_ask_dollars"))
        bid = _f(quote.get("yes_bid_dollars") if side == "yes" else quote.get("no_bid_dollars"))
        if price is None or bid is None or not (0.0 < bid < price < 1.0):
            self.write_decision(window, "flat", "no_tradable_ask", quote, ofi_mean)
            return
        self.write_decision(window, side, "ofi_sign", quote, ofi_mean)

    def maybe_settle(self, window: Window) -> None:
        if window.ticker is None:
            return
        if window.settlement_written or f"s:{window.ticker}" in self.done:
            window.settlement_written = True
            return
        if _now() < window.close_time + timedelta(seconds=30):
            return
        quote = self._quote(window.ticker)
        if not quote:
            return
        result = (quote.get("result") or "").lower()
        if result not in ("yes", "no"):
            return
        self.append({
            "type": "settlement",
            "hypothesis_id": self.cfg["hypothesis_id"],
            "ticker": window.ticker,
            "series": window.series,
            "result": result,
            "status": quote.get("status"),
            "expiration_value": quote.get("expiration_value"),
            "settlement_value_dollars": quote.get("settlement_value_dollars"),
            "floor_strike": _f(quote.get("floor_strike")),
            "recorded_at": _now().isoformat(),
        })
        self.done.add(f"s:{window.ticker}")
        window.settlement_written = True
        print(f"[SETTLE] {window.ticker} result={result}", flush=True)

    def sweep(self) -> None:
        for spec in self.specs:
            market = self.active_market(spec["series"])
            if market:
                self.ensure_window(spec, market)
        finished = []
        for ticker, window in self.windows.items():
            self.maybe_decide(window)
            self.maybe_settle(window)
            if window.decision_written and window.settlement_written:
                finished.append(ticker)
        for ticker in finished:
            self.windows.pop(ticker, None)


async def ws_worker(cycle: Cycle, stop: asyncio.Event) -> None:
    backoff = 1.0
    coins = list(cycle.books)
    while not stop.is_set():
        try:
            async with websockets.connect(HL_WS, ping_interval=30, ping_timeout=10, max_size=10 * 1024 * 1024) as ws:
                for coin in coins:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}))
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}))
                print(f"[HL-WS] subscribed {coins}", flush=True)
                backoff = 1.0
                async for raw in ws:
                    if stop.is_set():
                        return
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    channel = msg.get("channel")
                    data = msg.get("data")
                    if channel == "l2Book" and isinstance(data, dict) and data.get("coin") in cycle.books:
                        cycle.on_book(data["coin"], data.get("levels", []), int(data.get("time", 0) or 0))
                    elif channel == "trades":
                        items = data if isinstance(data, list) else [data]
                        by_coin: dict[str, list] = {}
                        for item in items:
                            if isinstance(item, dict) and item.get("coin") in cycle.books:
                                by_coin.setdefault(item["coin"], []).append(item)
                        for coin, batch in by_coin.items():
                            cycle.on_trades(coin, batch)
        except Exception as exc:
            print(f"[HL-WS] {type(exc).__name__}: {exc}", flush=True)
        if stop.is_set():
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, 30.0)


async def arm_worker(cycle: Cycle, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            cycle.arm_from_clock()
        except Exception as exc:
            print(f"[ARM] {type(exc).__name__}: {exc}", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass


async def clock_worker(cycle: Cycle, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(cycle.sweep)
        except Exception as exc:
            print(f"[CLOCK] {type(exc).__name__}: {exc}", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass


async def amain() -> None:
    with CONFIG_PATH.open() as handle:
        cfg = yaml.safe_load(handle)
    cycle = Cycle(cfg)
    print(f"[CYCLE] tape={cycle.tape}", flush=True)
    print(f"[CYCLE] test={cfg['test']['series']} also={[s['series'] for s in cfg.get('record_also') or []]}", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await asyncio.gather(ws_worker(cycle, stop), arm_worker(cycle, stop), clock_worker(cycle, stop))


if __name__ == "__main__":
    asyncio.run(amain())
