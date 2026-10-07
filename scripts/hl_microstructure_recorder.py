"""
Hyperliquid microstructure + Kalshi 15M price recorder.

Runs two async workers:
  1. WebSocket to HL: subscribes to l2Book + trades for BTC, ETH
  2. Every 30s: snapshots rolling metrics, polls Kalshi for current 15M mid,
     appends one JSONL line per coin.

Output: $HLBOT_RUNTIME_DIR/state/hl_microstructure.jsonl
Logs:   stdout (systemd captures)

Metrics computed per snapshot:
  - mid: HL best bid/ask midpoint
  - spread_bps: (ask - bid) / mid * 10000
  - ofi_5: depth imbalance over top 5 levels
           (bid_depth - ask_depth) / (bid_depth + ask_depth)
  - taker_ratio_5m: buy_vol / (buy_vol + sell_vol) over rolling 5min
  - vwap_dev_bps: (vwap_5m - mid) / mid * 10000
  - bid_depth_usd, ask_depth_usd: top-5-level notional
  - n_trades_5m: trade count in rolling window
  - kalshi_mid: current Kalshi 15M YES midpoint (0..1)
"""
from __future__ import annotations
import asyncio, json, os, signal, sys, time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import requests
import websockets


HL_WS = "wss://api.hyperliquid.xyz/ws"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

COINS = ["BTC", "ETH"]
KALSHI_SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M"}

WINDOW_SEC = 300          # rolling window for taker flow / vwap
SNAPSHOT_INTERVAL = 30    # seconds between snapshots
TOP_N_LEVELS = 5          # depth levels for OFI

STATE_DIR = Path(os.getenv("HLBOT_RUNTIME_DIR", "/home/hlbot/hlbot-runtime/live")) / "state"
OUT_FILE = STATE_DIR / "hl_microstructure.jsonl"


# ---------------------------------------------------------------------------
# Per-coin rolling state
# ---------------------------------------------------------------------------
class CoinState:
    def __init__(self, window_sec: int = WINDOW_SEC):
        self.window_sec = window_sec
        self.best_bid: float | None = None
        self.best_ask: float | None = None
        self.bid_depth_usd: float = 0.0
        self.ask_depth_usd: float = 0.0
        self.trades: deque = deque()
        self.last_l2_ts: int = 0
        self.last_l2_iso: str = ""

    def update_book(self, levels, ts_ms: int):
        if not levels or len(levels) < 2:
            return
        bids, asks = levels[0], levels[1]
        if not bids or not asks:
            return
        self.best_bid = float(bids[0]["px"])
        self.best_ask = float(asks[0]["px"])
        self.bid_depth_usd = sum(float(b["px"]) * float(b["sz"])
                                  for b in bids[:TOP_N_LEVELS])
        self.ask_depth_usd = sum(float(a["px"]) * float(a["sz"])
                                  for a in asks[:TOP_N_LEVELS])
        self.last_l2_ts = ts_ms
        if ts_ms:
            self.last_l2_iso = datetime.fromtimestamp(
                ts_ms / 1000, tz=timezone.utc).isoformat()

    def add_trades(self, trades: list):
        now_ms = int(time.time() * 1000)
        for t in trades:
            try:
                self.trades.append((
                    int(t.get("time", now_ms)),
                    str(t.get("side", "")),      # "B" = taker bought, "A" = taker sold
                    float(t.get("px", 0)),
                    float(t.get("sz", 0)),
                ))
            except (TypeError, ValueError):
                continue
        self._trim()

    def _trim(self):
        cutoff_ms = int(time.time() * 1000) - self.window_sec * 1000
        while self.trades and self.trades[0][0] < cutoff_ms:
            self.trades.popleft()

    def compute_metrics(self) -> dict | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        mid = (self.best_bid + self.best_ask) / 2.0
        spread_bps = (self.best_ask - self.best_bid) / mid * 10000.0

        total_depth = self.bid_depth_usd + self.ask_depth_usd
        ofi = (self.bid_depth_usd - self.ask_depth_usd) / total_depth \
            if total_depth > 0 else 0.0

        self._trim()
        buy_vol = sum(sz for _, side, _, sz in self.trades if side == "B")
        sell_vol = sum(sz for _, side, _, sz in self.trades if side == "A")
        total_vol = buy_vol + sell_vol
        taker_ratio = buy_vol / total_vol if total_vol > 0 else 0.5

        if self.trades:
            notional = sum(px * sz for _, _, px, sz in self.trades)
            total_sz = sum(sz for _, _, _, sz in self.trades)
            vwap = notional / total_sz if total_sz > 0 else mid
        else:
            vwap = mid
        vwap_dev_bps = (vwap - mid) / mid * 10000.0

        return {
            "mid": round(mid, 4),
            "best_bid": round(self.best_bid, 4),
            "best_ask": round(self.best_ask, 4),
            "spread_bps": round(spread_bps, 3),
            "ofi_5": round(ofi, 6),
            "taker_ratio_5m": round(taker_ratio, 6),
            "vwap_dev_bps": round(vwap_dev_bps, 3),
            "bid_depth_usd": round(self.bid_depth_usd, 2),
            "ask_depth_usd": round(self.ask_depth_usd, 2),
            "n_trades_5m": len(self.trades),
        }


# ---------------------------------------------------------------------------
# HL WebSocket worker
# ---------------------------------------------------------------------------
async def hl_ws_worker(states: dict[str, CoinState], stop_event: asyncio.Event):
    backoff = 1.0
    while not stop_event.is_set():
        try:
            print(f"[HL-WS] connecting...", flush=True)
            async with websockets.connect(
                HL_WS, ping_interval=30, ping_timeout=10,
                close_timeout=5, max_size=10 * 1024 * 1024,
            ) as ws:
                for coin in COINS:
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "l2Book", "coin": coin},
                    }))
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "trades", "coin": coin},
                    }))
                print(f"[HL-WS] subscribed: {COINS}", flush=True)
                backoff = 1.0  # reset on successful connect

                async for raw in ws:
                    if stop_event.is_set():
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    ch = msg.get("channel")
                    data = msg.get("data")
                    if not hasattr(hl_ws_worker, "_msg_count"):
                        hl_ws_worker._msg_count = 0
                    hl_ws_worker._msg_count += 1
                    if hl_ws_worker._msg_count <= 10:
                        print(f"[HL-WS] msg#{hl_ws_worker._msg_count}: channel={ch}", flush=True)
                    if ch == "l2Book" and isinstance(data, dict):
                        coin = data.get("coin")
                        if coin in states:
                            states[coin].update_book(
                                data.get("levels", []),
                                int(data.get("time", 0) or 0),
                            )
                    elif ch == "trades":
                        items = data if isinstance(data, list) else [data]
                        # HL batches trades; group by coin
                        by_coin: dict[str, list] = {}
                        for t in items:
                            if not isinstance(t, dict):
                                continue
                            c = t.get("coin")
                            if c in states:
                                by_coin.setdefault(c, []).append(t)
                        for c, batch in by_coin.items():
                            states[c].add_trades(batch)

        except Exception as e:
            print(f"[HL-WS] error: {type(e).__name__}: {e}", flush=True)

        if stop_event.is_set():
            break
        print(f"[HL-WS] reconnecting in {backoff:.1f}s", flush=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, 30.0)


# ---------------------------------------------------------------------------
# Kalshi lookup
# ---------------------------------------------------------------------------
def kalshi_current_market(series_ticker: str) -> dict | None:
    """Pick the market whose observation window contains NOW.
    If none active, return the next one opening soonest."""
    try:
        r = requests.get(
            f"{KALSHI_BASE}/markets",
            params={"series_ticker": series_ticker, "status": "open", "limit": 50},
            timeout=8,
        )
        r.raise_for_status()
        mkts = r.json().get("markets", [])
        if not mkts:
            return None
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Prefer markets whose window contains now:
        #    open_time <= now < close_time
        active = [m for m in mkts
                  if (m.get("open_time") or "") <= now_iso < (m.get("close_time") or "")]
        if active:
            # Among active, pick the one with highest 24h volume
            active.sort(key=lambda m: -float(m.get("volume_24h_fp", 0) or 0))
            return active[0]

        # 2. Fallback: pick the soonest opening market
        future = [m for m in mkts if (m.get("open_time") or "") > now_iso]
        future.sort(key=lambda m: m.get("open_time", ""))
        return future[0] if future else None
    except Exception as e:
        print(f"[KALSHI] fetch failed for {series_ticker}: {e}", flush=True)
        return None


def kalshi_mid(market: dict | None) -> tuple[float | None, str | None]:
    if not market:
        return None, None
    yb = market.get("yes_bid_dollars") or "0"
    ya = market.get("yes_ask_dollars") or "0"
    try:
        yb_f = float(yb)
        ya_f = float(ya)
        if 0 < yb_f < ya_f < 1:
            return round((yb_f + ya_f) / 2.0, 4), market.get("ticker")
    except (TypeError, ValueError):
        pass
    return None, market.get("ticker")


# ---------------------------------------------------------------------------
# Snapshot writer
# ---------------------------------------------------------------------------
async def writer_worker(states: dict[str, CoinState], stop_event: asyncio.Event,
                         interval: int = SNAPSHOT_INTERVAL):
    # cache Kalshi markets per coin to avoid hammering REST
    while not stop_event.is_set():
        cycle_start = time.time()
        try:
            for coin in COINS:
                m = states[coin].compute_metrics()
                if m is None:
                    print(f"[WRITER] {coin}: no metrics yet (book not received)", flush=True)
                    continue

                # Run blocking REST call in a thread to avoid freezing the loop
                try:
                    market = await asyncio.wait_for(
                        asyncio.to_thread(kalshi_current_market, KALSHI_SERIES[coin]),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    print(f"[WRITER] {coin}: kalshi fetch timed out", flush=True)
                    market = None
                kmid, kticker = kalshi_mid(market)

                rec = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "ts_epoch": cycle_start,
                    "coin": coin,
                    "kalshi_ticker": kticker,
                    "kalshi_mid": kmid,
                    "hl_last_l2_iso": states[coin].last_l2_iso,
                    **m,
                }
                try:
                    STATE_DIR.mkdir(parents=True, exist_ok=True)
                    with open(OUT_FILE, "a") as f:
                        f.write(json.dumps(rec) + "\n")
                    print(f"[WRITER] {coin}: mid={m['mid']} "
                          f"ofi={m['ofi_5']:+.3f} taker={m['taker_ratio_5m']:.3f} "
                          f"kalshi={kmid}", flush=True)
                except Exception as e:
                    print(f"[WRITER] write failed: {e}", flush=True)
        except Exception as e:
            print(f"[WRITER] cycle error: {type(e).__name__}: {e}", flush=True)

        elapsed = time.time() - cycle_start
        sleep_for = max(1.0, interval - elapsed)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def amain():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[RECORDER] starting", flush=True)
    print(f"[RECORDER] coins={COINS}", flush=True)
    print(f"[RECORDER] output={OUT_FILE}", flush=True)
    print(f"[RECORDER] snapshot every {SNAPSHOT_INTERVAL}s", flush=True)

    states = {c: CoinState(window_sec=WINDOW_SEC) for c in COINS}
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await asyncio.gather(
        hl_ws_worker(states, stop_event),
        writer_worker(states, stop_event),
    )
    print("[RECORDER] stopped cleanly", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
