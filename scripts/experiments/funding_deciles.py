"""Funding-decile diagnostic for Hyperliquid perps.

Question, locked before the run: if we sort perps by the funding they
just paid, does the next day's return line up?

This is a scatter, not a strategy. It does not place orders.

Rules, set before looking at the result:
- Universe: coins in the main perp meta. Builder-deployed xyz markets
  are a different book and are left out.
- History: the last 120 days the endpoints return. fundingHistory
  allows 500 rows a call and is weighted at 20 plus one per 20 rows
  under a shared 1,200-per-minute budget, so a pull back to 2023
  is a separate job. The 120-day window was set for that limit,
  not after seeing a result.
- Signal on day D: sum of hourly funding rates timestamped on day D.
  A positive rate means longs pay shorts. A coin-day needs at least
  12 hourly prints and candle notional (base volume x close) of at
  least $100,000.
- Next price return: close[D+1] / close[D] - 1, using only candles
  whose end time is already in the past.
- Next funding: the same sum on day D+1 (the carry a short receives
  if the rate stays positive).
- Short total: next funding minus next price. That is the one-day
  result of being short, before fees.
- A day with fewer than 30 eligible coins is skipped.
- Ten equal-count buckets. Decile 1 is the lowest funding that day,
  decile 10 the highest. Ties break on the coin name, which does not
  use the future return. If several deciles share the same funding,
  their price bars are not evidence.
- Uncertainty is the day-by-day spread (decile 10 minus decile 1),
  not the coin-day sample, because names move together.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

HL_URL = "https://api.hyperliquid.xyz/info"
ROOT = Path("/home/hlbot/hyperliquid-stat-arb-bot")
CACHE = ROOT / "reports" / ".funding_decile_cache"
PLOT = ROOT / "reports" / "funding_deciles.png"
TABLE = ROOT / "reports" / "funding_deciles.csv"

LOOKBACK_DAYS = 120
MIN_HOURS = 12
MIN_NOTIONAL = 100_000.0
MIN_TRADES = 1
MIN_COINS = 30
N_DECILES = 10
WORKERS = 1
PAGE = 500
# A full page is about 45 weight. 2.6s keeps a single stream near 1,000
# of the 1,200 per-minute budget.
PACE_SEC = 2.6
_PACE_LOCK = threading.Lock()
_PACE_NEXT = 0.0


def _pace() -> None:
    global _PACE_NEXT
    with _PACE_LOCK:
        wait = _PACE_NEXT - time.time()
        if wait > 0:
            time.sleep(wait)
        _PACE_NEXT = time.time() + PACE_SEC


def _post(session: requests.Session, payload: dict):
    for attempt in range(8):
        try:
            _pace()
            response = session.post(HL_URL, json=payload, timeout=40)
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(min(30.0, 2.0 ** attempt))
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            time.sleep(min(30.0, 2.0 ** attempt))
    return None


def universe(session: requests.Session) -> list[str]:
    data = _post(session, {"type": "meta"})
    if not isinstance(data, dict) or "universe" not in data:
        raise RuntimeError("meta request failed")
    return [row["name"] for row in data["universe"] if row.get("name")]


def _frame_from_rows(rows: list[tuple[int, float]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["time", "funding"])
    if frame.empty:
        return frame
    return frame.drop_duplicates("time").sort_values("time")


def fetch_funding(coin: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Page fundingHistory. A finished coin gets a .done marker.

    A mid-history error keeps the pages already stored so the next
    call continues at the last timestamp instead of starting over.
    """
    path = CACHE / f"{coin}_funding.pkl"
    done = CACHE / f"{coin}_funding.done"
    if path.exists() and done.exists() and (time.time() - done.stat().st_mtime) < 12 * 3600:
        cached = pd.read_pickle(path)
        if not cached.empty and int(cached["time"].min()) >= start_ms - 86_400_000:
            return cached

    rows: list[tuple[int, float]] = []
    cursor = start_ms
    if path.exists():
        prior = pd.read_pickle(path)
        if not prior.empty and int(prior["time"].max()) >= start_ms:
            prior = prior[prior["time"] >= start_ms]
            rows = list(zip(prior["time"].astype(int), prior["funding"].astype(float)))
            if rows:
                cursor = int(prior["time"].max()) + 1

    session = requests.Session()
    finished = cursor >= end_ms
    for _ in range(90):
        if cursor >= end_ms:
            finished = True
            break
        page = _post(session, {
            "type": "fundingHistory",
            "coin": coin,
            "startTime": cursor,
            "endTime": end_ms,
        })
        if not isinstance(page, list):
            break
        if not page:
            finished = True
            break
        for item in page:
            rows.append((int(item["time"]), float(item["fundingRate"])))
        last = int(page[-1]["time"])
        if len(page) < PAGE or last + 1 <= cursor:
            finished = True
            break
        cursor = last + 1
        time.sleep(0.05)

    frame = _frame_from_rows(rows)
    frame.to_pickle(path)
    if finished:
        done.write_text("ok\n")
    return frame


def fetch_candles(coin: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    path = CACHE / f"{coin}_1d.pkl"
    if path.exists() and (time.time() - path.stat().st_mtime) < 12 * 3600:
        cached = pd.read_pickle(path)
        if "trades" in getattr(cached, "columns", []):
            return cached

    session = requests.Session()
    page = _post(session, {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": "1d",
            "startTime": start_ms,
            "endTime": end_ms,
        },
    })
    if not isinstance(page, list):
        return pd.DataFrame(columns=["date", "close", "notional", "trades"])
    rows = []
    for item in page or []:
        end_t = int(item["T"])
        if end_t > end_ms:
            continue
        close = float(item["c"])
        volume = float(item["v"])
        rows.append({
            "date": pd.Timestamp(int(item["t"]), unit="ms", tz="UTC").normalize(),
            "close": close,
            "notional": volume * close,
            "trades": int(item.get("n") or 0),
        })
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.drop_duplicates("date").sort_values("date")
    frame.to_pickle(path)
    return frame


def _daily_funding(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(columns=["date", "fund", "n_hours"])
    stamps = pd.to_datetime(raw["time"], unit="ms", utc=True)
    daily = (
        pd.DataFrame({"funding": raw["funding"].to_numpy(), "date": stamps.dt.normalize()})
        .groupby("date", sort=True)["funding"]
        .agg(fund="sum", n_hours="count")
        .reset_index()
    )
    return daily


def build_panel(coins: list[str], start_ms: int, end_ms: int) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    funding: dict[str, pd.DataFrame] = {}
    candles: dict[str, pd.DataFrame] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fund_jobs = {pool.submit(fetch_funding, coin, start_ms, end_ms): coin for coin in coins}
        for job in as_completed(fund_jobs):
            coin = fund_jobs[job]
            funding[coin] = job.result()
            done += 1
            if done % 10 == 0 or done == len(coins):
                print(f"funding {done}/{len(coins)}", flush=True)
        done = 0
        candle_jobs = {pool.submit(fetch_candles, coin, start_ms, end_ms): coin for coin in coins}
        for job in as_completed(candle_jobs):
            coin = candle_jobs[job]
            candles[coin] = job.result()
            done += 1
            if done % 25 == 0 or done == len(coins):
                print(f"candles {done}/{len(coins)}", flush=True)

    for pass_n in range(2, 5):
        missing = [c for c in coins if not (CACHE / f"{c}_funding.done").exists()]
        if not missing:
            break
        print(f"funding incomplete {len(missing)}; pass {pass_n}", flush=True)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            jobs = {pool.submit(fetch_funding, coin, start_ms, end_ms): coin for coin in missing}
            for job in as_completed(jobs):
                funding[jobs[job]] = job.result()
    still = [c for c in coins if not (CACHE / f"{c}_funding.done").exists()]
    print(f"funding still incomplete {len(still)}", flush=True)

    pieces = []
    for coin in coins:
        daily = _daily_funding(funding[coin])
        px = candles[coin]
        if daily.empty or px.empty:
            continue
        merged = daily.merge(px, on="date", how="inner").sort_values("date")
        merged["next_px"] = merged["close"].shift(-1) / merged["close"] - 1.0
        merged["next_fund"] = merged["fund"].shift(-1)
        merged["next_hours"] = merged["n_hours"].shift(-1)
        merged["coin"] = coin
        pieces.append(merged)
    if not pieces:
        return pd.DataFrame()
    panel = pd.concat(pieces, ignore_index=True)
    panel = panel.dropna(subset=["next_px", "next_fund", "next_hours"])
    panel = panel[
        (panel["n_hours"] >= MIN_HOURS)
        & (panel["next_hours"] >= MIN_HOURS)
        & (panel["notional"] >= MIN_NOTIONAL)
        & (panel["trades"] >= MIN_TRADES)
    ].copy()
    panel["short_total"] = panel["next_fund"] - panel["next_px"]
    return panel


def assign_deciles(panel: pd.DataFrame) -> pd.DataFrame:
    kept = []
    for _, day in panel.groupby("date", sort=True):
        if len(day) < MIN_COINS:
            continue
        ordered = day.sort_values(["fund", "coin"], kind="mergesort")
        ordered = ordered.copy()
        ordered["decile"] = pd.qcut(
            np.arange(len(ordered)), N_DECILES, labels=False
        ) + 1
        kept.append(ordered)
    if not kept:
        return pd.DataFrame()
    return pd.concat(kept, ignore_index=True)


def summarize(ranked: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    table = (
        ranked.groupby("decile")
        .agg(
            n=("coin", "size"),
            days=("date", "nunique"),
            fund_bps=("fund", lambda s: float(s.mean() * 1e4)),
            next_px_mean_bps=("next_px", lambda s: float(s.mean() * 1e4)),
            next_px_median_bps=("next_px", lambda s: float(s.median() * 1e4)),
            next_fund_bps=("next_fund", lambda s: float(s.mean() * 1e4)),
            short_mean_bps=("short_total", lambda s: float(s.mean() * 1e4)),
            short_median_bps=("short_total", lambda s: float(s.median() * 1e4)),
        )
        .reset_index()
    )

    def daily_spread(column: str) -> pd.Series:
        means = ranked.groupby(["date", "decile"])[column].mean().unstack()
        return means[N_DECILES] - means[1]

    spreads = {}
    for name, column in (
        ("price", "next_px"),
        ("funding", "next_fund"),
        ("short", "short_total"),
    ):
        spread = daily_spread(column).dropna()
        sigma = float(spread.std(ddof=1))
        n_days = int(spread.shape[0])
        stderr = sigma / np.sqrt(n_days) if n_days else float("nan")
        mean = float(spread.mean())
        spreads[name] = {
            "mean_bps": mean * 1e4,
            "stderr_bps": stderr * 1e4,
            "t": mean / stderr if stderr and np.isfinite(stderr) else float("nan"),
            "days": n_days,
        }
    # Spearman on each day, then the average across days. Ties get average ranks.
    daily_rho = []
    for _, day in ranked.groupby("date", sort=True):
        if day["fund"].nunique() < 3:
            continue
        daily_rho.append(float(day["fund"].corr(day["next_px"], method="spearman")))
    rho = pd.Series(daily_rho)
    spreads["spearman_price"] = {
        "mean": float(rho.mean()) if len(rho) else float("nan"),
        "stderr": float(rho.std(ddof=1) / np.sqrt(len(rho))) if len(rho) > 1 else float("nan"),
        "days": int(len(rho)),
    }
    return table, spreads


def draw(table: pd.DataFrame, meta: dict, spreads: dict) -> None:
    labels = table["decile"].astype(int).astype(str)
    x = np.arange(len(table))
    fig, axes = plt.subplots(1, 3, figsize=(12.2, 4.4), sharex=True)
    series = (
        ("next_px_mean_bps", "Next-day price return", "What the coin did"),
        ("next_fund_bps", "Next-day funding", "Carry a short receives"),
        ("short_mean_bps", "Short total, no fees", "Funding minus the price move"),
    )
    for ax, (column, title, subtitle) in zip(axes, series):
        color = "#4c5c68"
        ax.bar(x, table[column], color=color, width=0.78)
        ax.axhline(0, color="#1c1c1c", linewidth=0.6)
        ax.set_title(title, fontsize=11)
        ax.set_xticks(x)
        ax.set_xticklabels(labels)
        ax.set_xlabel("Funding decile  (1 low, 10 high)")
        ax.set_ylabel("Average, basis points per day")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.text(0.0, 1.02, subtitle, transform=ax.transAxes, fontsize=8, color="#555")
    fig.suptitle(
        "Hyperliquid perps sorted by today's funding: does tomorrow line up?",
        fontsize=13, y=1.04,
    )
    fig.text(
        0.01, -0.08,
        f"{meta['start']} to {meta['end']}   "
        f"{meta['coins']} coins   {meta['days']} days   {meta['rows']} coin-days   "
        f"notional >= ${MIN_NOTIONAL:,.0f}   "
        f"D10-D1 price {spreads['price']['mean_bps']:+.1f} bp "
        f"(t {spreads['price']['t']:+.2f})   "
        f"D10-D1 funding {spreads['funding']['mean_bps']:+.1f} bp "
        f"(t {spreads['funding']['t']:+.2f})",
        fontsize=8, color="#333",
    )
    fig.tight_layout()
    fig.savefig(PLOT, dpi=140, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - LOOKBACK_DAYS * 24 * 3600 * 1000
    session = requests.Session()
    coins = universe(session)
    print(f"coins {len(coins)}", flush=True)
    panel = build_panel(coins, start_ms, end_ms)
    if panel.empty:
        raise SystemExit("no eligible coin-days")
    ranked = assign_deciles(panel)
    if ranked.empty:
        raise SystemExit("no days with enough coins")
    table, spreads = summarize(ranked)
    table.to_csv(TABLE, index=False)
    meta = {
        "start": str(ranked["date"].min().date()),
        "end": str(ranked["date"].max().date()),
        "coins": int(ranked["coin"].nunique()),
        "days": int(ranked["date"].nunique()),
        "rows": int(len(ranked)),
    }
    draw(table, meta, spreads)
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)
    pd.set_option("display.float_format", lambda v: f"{v:,.2f}")
    print(table.to_string(index=False))
    print(
        f"window {meta['start']} -> {meta['end']}  "
        f"coins {meta['coins']}  days {meta['days']}  rows {meta['rows']}"
    )
    for name in ("price", "funding", "short"):
        row = spreads[name]
        print(
            f"D10-D1 {name:8} {row['mean_bps']:+8.2f} bp/day  "
            f"stderr {row['stderr_bps']:6.2f}  t {row['t']:+6.2f}  "
            f"days {row['days']}"
        )
    rho = spreads["spearman_price"]
    print(
        f"mean daily Spearman(funding, next price) {rho['mean']:+.3f}  "
        f"stderr {rho['stderr']:.3f}  days {rho['days']}"
    )
    hourly = ranked["fund"] / ranked["n_hours"]
    clamp = float(((hourly - 1.25e-5).abs() <= 1e-12).mean())
    zeros = float((hourly.abs() <= 1e-12).mean())
    print(f"share of coin-days sitting on the 0.00125% hourly clamp {clamp:.1%}")
    print(f"share of coin-days with funding exactly zero {zeros:.1%}")
    print(f"plot {PLOT}")


if __name__ == "__main__":
    main()
