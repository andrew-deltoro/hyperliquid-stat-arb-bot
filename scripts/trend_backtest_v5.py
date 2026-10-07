"""
Trend-following v5 — walk-forward coin selection on the FULL HL universe.

The v4 result (portfolio Sharpe 1.35 across 29 coins) is honest but suffers
from two biases:
  1. Survivorship — coins that delisted before 2026 aren't in the sample
  2. Selection — I hand-picked 29 coins from memory

v5 fixes both:
  - Fetches the entire HL perp universe (~200 symbols)
  - Keeps every coin with >= 400 days of data (includes delisted-but-had-data)
  - Uses QUARTERLY walk-forward with trailing 180d Sharpe-based coin selection
  - Reports OOS portfolio Sharpe across all quarters, per-cohort

Structure:
  For each quarter Q_t in the test window:
    1. Rank all coins by their trailing 180d Sharpe (as of end of Q_{t-1})
    2. Select coins with Sharpe > MIN_COIN_SHARPE and >= MIN_TRADES trades
    3. Trade those coins EQUAL-WEIGHT through Q_t using Dual-MA signals
    4. Record portfolio return for the quarter
  Aggregate: portfolio of quarter-portfolios = the deployable strategy

Costs: 3 bps fee + 3 bps slippage per side = 12 bps round trip
Sizing: fixed-fraction, 1x initial capital per position
"""
from __future__ import annotations
import sys
import time
import json
import pickle
from pathlib import Path
from datetime import datetime

sys.path.insert(0, "/home/hlbot/hyperliquid-stat-arb-bot")
import numpy as np
import pandas as pd
import requests

from src.hyperliquid_bot.data.ohlcv import fetch_candles


HL_URL = "https://api.hyperliquid.xyz/info"
CACHE_DIR = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports/.trend_cache")
CACHE_FILE = CACHE_DIR / "daily_candles_1500d.pkl"

FEE_BPS = 3.0
SLIP_BPS = 3.0
RT_COST = (FEE_BPS + SLIP_BPS) * 2 / 10000

# Walk-forward params
MIN_DATA_DAYS = 400            # minimum bars for a coin to be considered
TRAILING_WINDOW_DAYS = 180     # Sharpe measurement window
MIN_COIN_SHARPE = 0.3          # minimum trailing Sharpe to include a coin
MIN_COIN_TRADES = 3            # minimum trades in trailing window
MAX_COINS_PER_QUARTER = 15     # cap to limit portfolio turnover
QUARTER_DAYS = 91              # ~3 months


# ---------------------------------------------------------------------------
# Data fetching with cache
# ---------------------------------------------------------------------------
def fetch_universe() -> list[str]:
    """Return all HL perp symbols (main dex)."""
    try:
        r = requests.post(HL_URL, json={"type": "meta"}, timeout=20)
        r.raise_for_status()
        data = r.json()
        return [a["name"] for a in data.get("universe", [])]
    except Exception as e:
        print(f"  universe fetch failed: {e}")
        return []


def fetch_one(coin: str, retries: int = 3) -> pd.DataFrame | None:
    """Fetch daily candles, retry on 429."""
    for attempt in range(retries):
        try:
            candles = fetch_candles(coin, interval="1d", lookback_hours=24 * 1500)
            if not candles or len(candles) < 200:
                return None
            df = pd.DataFrame([{
                "t": c.t, "open": c.o, "high": c.h,
                "low": c.l, "close": c.c, "volume": c.v,
            } for c in candles])
            df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_localize(None)
            return df.set_index("date").sort_index()
        except Exception as e:
            if "429" in str(e) or "Too Many" in str(e):
                wait = 2 ** attempt
                time.sleep(wait)
                continue
            return None
    return None


def load_or_fetch_universe() -> dict[str, pd.DataFrame]:
    """Return {coin: dataframe}. Uses disk cache if fresh."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if CACHE_FILE.exists():
        age_hours = (time.time() - CACHE_FILE.stat().st_mtime) / 3600
        if age_hours < 12:
            print(f"Loading cache ({age_hours:.1f}h old)...")
            with open(CACHE_FILE, "rb") as f:
                data = pickle.load(f)
            print(f"  {len(data)} coins from cache")
            return data

    universe = fetch_universe()
    print(f"Fetching {len(universe)} coins from HL...")
    print(f"  (rate-limited ~0.5s/coin, expect ~2 min)")

    data: dict[str, pd.DataFrame] = {}
    t0 = time.time()
    for i, coin in enumerate(universe):
        if i % 20 == 0 and i > 0:
            elapsed = time.time() - t0
            print(f"  [{i}/{len(universe)}]  {len(data)} valid  "
                  f"({elapsed:.0f}s elapsed)")
        df = fetch_one(coin)
        if df is not None and len(df) >= MIN_DATA_DAYS:
            data[coin] = df
        time.sleep(0.4)  # rate limit

    print(f"  fetched {len(data)} coins with >= {MIN_DATA_DAYS} days")

    with open(CACHE_FILE, "wb") as f:
        pickle.dump(data, f)
    return data


# ---------------------------------------------------------------------------
# Signal + sim
# ---------------------------------------------------------------------------
def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def dual_ma_signal(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    score = pd.Series(0.0, index=df.index)
    for w in (50, 100, 200):
        ma = df["close"].rolling(w).mean()
        score += np.sign(df["close"] - ma).fillna(0.0)
    return (score > 0).fillna(False), (score < 0).fillna(False)


def simulate(df: pd.DataFrame) -> dict:
    """Full simulation of Dual-MA on df. Returns equity + trades."""
    df = df.copy()
    df["atr"] = atr(df, 14)
    L, S = dual_ma_signal(df)

    position = 0
    entry_price = 0.0
    stop_price = 0.0
    size_usd = 0.0
    initial = 1.0
    cash = initial
    equity = [initial]
    trades = []

    for i in range(1, len(df)):
        close = df["close"].iloc[i]
        a = df["atr"].iloc[i]
        want_long = bool(L.iloc[i]) if not pd.isna(L.iloc[i]) else False
        want_short = bool(S.iloc[i]) if not pd.isna(S.iloc[i]) else False

        if position != 0:
            exit_now, reason = False, ""
            if position == 1 and df["low"].iloc[i] <= stop_price:
                exit_now, reason = True, "stop"
            elif position == -1 and df["high"].iloc[i] >= stop_price:
                exit_now, reason = True, "stop"
            if not exit_now and position == 1 and not want_long:
                exit_now, reason = True, "flip"
            if not exit_now and position == -1 and not want_short:
                exit_now, reason = True, "flip"
            if exit_now:
                realized = size_usd * ((close - entry_price) / entry_price) * position
                cost = size_usd * RT_COST
                cash = cash + size_usd + realized - cost
                trades.append({
                    "exit_date": df.index[i],
                    "return": realized / size_usd - RT_COST,
                    "reason": reason,
                })
                position = 0
                size_usd = 0.0

        if position == 0 and not pd.isna(a) and cash > 0.01:
            base_size = min(cash, initial)
            if want_long and base_size > 0:
                position = 1
                entry_price = close
                stop_price = close - 2 * a
                size_usd = base_size
                cash -= size_usd
            elif want_short and base_size > 0:
                position = -1
                entry_price = close
                stop_price = close + 2 * a
                size_usd = base_size
                cash -= size_usd

        if position == 0:
            equity.append(cash)
        else:
            unrealized = size_usd * ((close - entry_price) / entry_price) * position
            equity.append(cash + size_usd + unrealized)

    eq = pd.Series(equity, index=df.index)
    return {"equity": eq, "n_trades": len(trades), "trades": trades}


def sharpe_of(eq: pd.Series) -> float:
    """Annualized Sharpe from an equity series."""
    if len(eq) < 20:
        return float("nan")
    r = eq.pct_change().dropna()
    if len(r) < 5 or r.std() == 0:
        return float("nan")
    ann_ret = (eq.iloc[-1] / eq.iloc[0]) ** (365 / len(eq)) - 1 if eq.iloc[0] > 0 else -1
    ann_vol = r.std() * np.sqrt(365)
    return ann_ret / ann_vol if ann_vol > 0 else float("nan")


# ---------------------------------------------------------------------------
# Walk-forward engine
# ---------------------------------------------------------------------------
def build_timeline(data: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    """Common daily index spanning the union of all coin date ranges."""
    all_dates = set()
    for df in data.values():
        all_dates.update(df.index.tolist())
    return pd.DatetimeIndex(sorted(all_dates))


def score_coins_at(data: dict[str, pd.DataFrame],
                    as_of: pd.Timestamp,
                    trailing_days: int = TRAILING_WINDOW_DAYS
                    ) -> list[tuple[str, float, int]]:
    """
    For each coin, compute trailing Sharpe and trade count as of `as_of`.
    Returns [(coin, trailing_sharpe, n_trades), ...] sorted by Sharpe desc.
    """
    results = []
    start = as_of - pd.Timedelta(days=trailing_days)
    for coin, df in data.items():
        sub = df.loc[(df.index > start) & (df.index <= as_of)]
        if len(sub) < 60:
            continue
        try:
            r = simulate(sub)
        except Exception:
            continue
        s = sharpe_of(r["equity"])
        if np.isnan(s):
            continue
        results.append((coin, float(s), int(r["n_trades"])))
    results.sort(key=lambda x: -x[1])
    return results


def walk_forward(data: dict[str, pd.DataFrame],
                  min_sharpe: float = MIN_COIN_SHARPE,
                  min_trades: int = MIN_COIN_TRADES,
                  max_coins: int = MAX_COINS_PER_QUARTER
                  ) -> dict:
    """Quarterly walk-forward with trailing-Sharpe coin selection."""
    timeline = build_timeline(data)
    start = timeline[0] + pd.Timedelta(days=TRAILING_WINDOW_DAYS + 30)
    end = timeline[-1] - pd.Timedelta(days=QUARTER_DAYS)

    quarters = []
    cursor = start
    while cursor < end:
        q_end = min(cursor + pd.Timedelta(days=QUARTER_DAYS), timeline[-1])

        # Select coins based on data BEFORE this quarter
        scores = score_coins_at(data, cursor - pd.Timedelta(days=1))
        selected = [
            (c, s, n) for (c, s, n) in scores
            if s >= min_sharpe and n >= min_trades
        ][:max_coins]

        if not selected:
            quarters.append({
                "start": cursor, "end": q_end,
                "n_coins": 0, "portfolio_ret": 0.0,
                "selected": [], "per_coin_ret": {},
            })
            cursor = q_end
            continue

        # Trade each selected coin in this quarter
        per_coin_rets = {}
        for coin, _sharpe, _n in selected:
            df = data[coin]
            sub = df.loc[(df.index >= cursor) & (df.index < q_end)]
            if len(sub) < 10:
                continue
            r = simulate(sub)
            eq = r["equity"]
            ret = float(eq.iloc[-1] / eq.iloc[0] - 1)
            per_coin_rets[coin] = ret

        # Portfolio return = equal-weight mean
        if per_coin_rets:
            port_ret = float(np.mean(list(per_coin_rets.values())))
        else:
            port_ret = 0.0

        quarters.append({
            "start": cursor, "end": q_end,
            "n_coins": len(per_coin_rets),
            "portfolio_ret": port_ret,
            "selected": [c for c, _, _ in selected],
            "per_coin_ret": per_coin_rets,
        })
        cursor = q_end

    return {"quarters": quarters, "timeline": timeline}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def report(wf: dict):
    quarters = wf["quarters"]
    if not quarters:
        print("No quarters generated.")
        return

    print(f"\n{'='*110}")
    print("QUARTERLY WALK-FORWARD (trailing-180d Sharpe coin selection)")
    print(f"{'='*110}")
    print(f"  {'Q':>3}  {'Start':>11}  {'End':>11}  {'N':>3}  "
          f"{'PortRet':>9}  {'Sharpe_sel':>11}  {'Top 3 coins'}")
    print(f"  {'-'*100}")

    port_rets = []
    for i, q in enumerate(quarters, 1):
        pr = q["portfolio_ret"]
        port_rets.append(pr)
        top3 = ",".join(q["selected"][:3]) if q["selected"] else "-"
        start_s = q["start"].date()
        end_s = q["end"].date()
        # Trailing Sharpe of top selected coin (approximation)
        sel_sharpe = "-"
        print(f"  {i:>3}  {str(start_s):>11}  {str(end_s):>11}  "
              f"{q['n_coins']:>3}  {pr*100:>8.2f}%  {sel_sharpe:>11}  {top3}")

    # Aggregate the quarter-portfolio
    port_rets = np.array(port_rets)
    if len(port_rets) < 4:
        print(f"\n  Not enough quarters for meaningful aggregation.")
        return

    # Build compounded portfolio curve
    eq = np.cumprod(1 + port_rets)
    eq_series = pd.Series(eq)
    daily_r = pd.Series(port_rets)
    ann_ret = (eq[-1] ** (365 / (len(quarters) * QUARTER_DAYS))) - 1
    ann_vol = daily_r.std() * np.sqrt(4)  # 4 quarters per year
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    peak = eq_series.cummax()
    max_dd = float(((eq_series - peak) / peak).min())

    downside = daily_r[daily_r < 0]
    down_vol = downside.std() * np.sqrt(4) if len(downside) > 0 else ann_vol
    sortino = ann_ret / down_vol if down_vol > 0 else 0.0

    print(f"\n{'='*110}")
    print("AGGREGATE OOS PORTFOLIO METRICS")
    print(f"{'='*110}")
    print(f"  Quarters:              {len(quarters)}")
    print(f"  Avg coins per quarter: {np.mean([q['n_coins'] for q in quarters]):.1f}")
    print(f"  Total quarters > 0:    {(port_rets > 0).sum()}/{len(port_rets)}")
    print(f"  Total return:          {(eq[-1] - 1)*100:+.1f}%")
    print(f"  Annualized return:     {ann_ret*100:+.1f}%")
    print(f"  Quarterly volatility:  {daily_r.std()*100:.2f}%")
    print(f"  Annualized volatility: {ann_vol*100:.1f}%")
    print(f"  Sharpe ratio:          {sharpe:.2f}")
    print(f"  Sortino ratio:         {sortino:.2f}")
    print(f"  Max drawdown:          {max_dd*100:.1f}%")
    print(f"  Best quarter:          {port_rets.max()*100:+.1f}%")
    print(f"  Worst quarter:         {port_rets.min()*100:+.1f}%")

    # Coin inclusion frequency
    all_coins = []
    for q in quarters:
        all_coins.extend(q["selected"])
    from collections import Counter
    freq = Counter(all_coins)
    print(f"\n  Most frequent coins across quarters:")
    for coin, n in freq.most_common(15):
        print(f"    {coin:>8}: {n}/{len(quarters)} quarters")

    # Verdict
    print(f"\n{'='*110}")
    print("VERDICT")
    print(f"{'='*110}")
    if sharpe > 1.0:
        print(f"  Sharpe {sharpe:.2f} — this is a REAL, tradeable strategy.")
        print(f"  → Deploy as portfolio, equal-weight, 0.5x sizing per coin.")
    elif sharpe > 0.6:
        print(f"  Sharpe {sharpe:.2f} — solid but not spectacular.")
        print(f"  → Deploy at reduced size. Add coins if live confirms.")
    elif sharpe > 0.3:
        print(f"  Sharpe {sharpe:.2f} — marginal, likely eaten by live costs.")
        print(f"  → Do not deploy. Edge too thin.")
    else:
        print(f"  Sharpe {sharpe:.2f} — no OOS edge. Reject.")


def main():
    print("=" * 110)
    print("TREND-FOLLOWING v5 — FULL UNIVERSE WALK-FORWARD")
    print("=" * 110)
    print(f"  Trailing Sharpe window: {TRAILING_WINDOW_DAYS}d")
    print(f"  Min coin Sharpe: {MIN_COIN_SHARPE}")
    print(f"  Min coin trades: {MIN_COIN_TRADES}")
    print(f"  Max coins/quarter: {MAX_COINS_PER_QUARTER}")
    print(f"  Quarter length: {QUARTER_DAYS}d")
    print(f"  Costs: {FEE_BPS} bps fee + {SLIP_BPS} bps slip per side")

    data = load_or_fetch_universe()
    if len(data) < 20:
        print(f"Not enough coins ({len(data)}). Aborting.")
        return

    print(f"\nLoaded {len(data)} coins")
    print(f"Date range: {min(df.index[0] for df in data.values()).date()} to "
          f"{max(df.index[-1] for df in data.values()).date()}")

    wf = walk_forward(data)
    report(wf)

    # Save the walk-forward detail
    out = Path("/home/hlbot/hyperliquid-stat-arb-bot/reports")
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    detail_path = out / f"trend_v5_walkforward_{stamp}.json"
    serializable = {
        "quarters": [
            {
                "start": str(q["start"].date()),
                "end": str(q["end"].date()),
                "n_coins": q["n_coins"],
                "portfolio_ret": q["portfolio_ret"],
                "selected": q["selected"],
                "per_coin_ret": q["per_coin_ret"],
            } for q in wf["quarters"]
        ]
    }
    detail_path.write_text(json.dumps(serializable, indent=2))
    print(f"\n  Saved: {detail_path}")


if __name__ == "__main__":
    main()
