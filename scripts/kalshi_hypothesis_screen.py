"""One-sided screen on the microstructure file already recorded.

This is not the locked test and it does not write a verdict. The old file
has a Kalshi midpoint every 30 seconds, not the ask at second 60, and only
one or two imbalance prints in the first minute. Paying the midpoint is
easier than crossing the ask, so a loss here is evidence against continuing.
A gain here is not a pass.

Rules, fixed before the run:
- BTC series KXBTC15M only.
- Window times come from the ticker, interpreted in America/New_York.
- Signal is the mean ofi_5 of rows timestamped in [open, open+60s).
- At least one such row. Zero mean is a skip.
- Price is the last non-null kalshi_mid inside that same minute.
- Outcome is the official Kalshi result.
- Fee is the one-contract taker fee on that midpoint.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path("/home/hlbot/hyperliquid-stat-arb-bot")
sys.path.insert(0, str(ROOT / "src"))

from hyperliquid_bot.kalshi_hypothesis import contract_pnl, taker_fee  # noqa: E402

TAPE = Path("/home/hlbot/hlbot-runtime/live/state/hl_microstructure.jsonl")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
NY = ZoneInfo("America/New_York")


def window_times(ticker: str) -> tuple[datetime, datetime]:
    stamp = ticker.split("-", 1)[1].split("-", 1)[0]
    close_local = datetime.strptime(stamp, "%y%b%d%H%M").replace(tzinfo=NY)
    close = close_local.astimezone(timezone.utc)
    return close - timedelta(minutes=15), close


def result_for(ticker: str) -> str | None:
    try:
        response = requests.get(f"{KALSHI}/markets/{ticker}", timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"  {ticker} lookup failed: {exc}", flush=True)
        return None
    market = response.json().get("market") or {}
    result = (market.get("result") or "").lower()
    return result if result in ("yes", "no") else None


def main() -> None:
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    with TAPE.open() as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("coin") != "BTC":
                continue
            ticker = row.get("kalshi_ticker") or ""
            if ticker.startswith("KXBTC15M-"):
                by_ticker[ticker].append(row)

    pnls: list[float] = []
    skipped = defaultdict(int)
    for ticker in sorted(by_ticker):
        try:
            open_time, _close = window_times(ticker)
        except ValueError:
            skipped["bad_ticker"] += 1
            continue
        decision_at = open_time + timedelta(seconds=60)
        inside = []
        for row in by_ticker[ticker]:
            when = datetime.fromisoformat(row["ts"])
            if open_time <= when < decision_at:
                inside.append(row)
        ofis = [float(row["ofi_5"]) for row in inside if row.get("ofi_5") is not None]
        mids = [float(row["kalshi_mid"]) for row in inside if row.get("kalshi_mid") is not None]
        if not ofis or not mids:
            skipped["no_first_minute"] += 1
            continue
        ofi = sum(ofis) / len(ofis)
        if ofi == 0:
            skipped["flat"] += 1
            continue
        price = mids[-1]
        if not 0.0 < price < 1.0:
            skipped["bad_price"] += 1
            continue
        result = result_for(ticker)
        if result is None:
            skipped["unsettled"] += 1
            continue
        side = "yes" if ofi > 0 else "no"
        pnls.append(contract_pnl(side, price, result, taker_fee(price)))

    n = len(pnls)
    print(f"contracts scored {n}  skipped {dict(skipped)}")
    if n == 0:
        return
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / max(n - 1, 1)
    stderr = (var ** 0.5) / (n ** 0.5)
    wins = sum(1 for p in pnls if p > 0)
    print(f"mean pnl per contract ${mean:+.4f}")
    print(f"stderr ${stderr:.4f}   mean/stderr {mean / stderr if stderr else float('nan'):+.2f}")
    print(f"wins {wins}/{n} = {wins / n:.1%}")
    print("price used is the Kalshi midpoint, not the ask.")
    if mean < -2 * stderr:
        print("screen: the optimistic version is already losing. Not worth a longer test.")
    else:
        print("screen: not a kill. One or two days cannot show that the real taker trade pays.")


if __name__ == "__main__":
    main()
