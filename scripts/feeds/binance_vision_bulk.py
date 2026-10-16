#!/usr/bin/env python
"""Bulk downloader for Binance Vision kline archives.

Why this exists
---------------
`data-api.binance.vision` serves both a REST kline endpoint (1,000 bars per
request) and pre-packed **monthly** and **daily** zip files. The zips are the
fast route: `BTCUSDT` 5m is ~0.45 MB per symbol-month, so a 30-symbol, 8-year
pull is a few hundred files rather than ~50,000 REST pages. The monthly zips
start at each pair's **listing month** (the month before it is HTTP 404), which
is a clean way to establish the listing date without a separate call.

This module is a **feeds** tool, not a study: it downloads and caches, and
makes no trading decision. It is deliberately idempotent so a study can call
it on every run without re-downloading.

Layout on disk
--------------
    reports/.binance_cache/                       (gitignored)
        BTCUSDT-5m-2017-08.zip                    monthly archive, kept as-is
        daily/BTCUSDT-5m-2026-10-15.zip           daily archives, current month
        panels/BTCUSDT_5m.npz                     parsed, sorted, de-duplicated

The `.zip` files are the raw cache; `panels/*.npz` is the parsed array form a
study loads. Re-running skips anything already on disk (`--refresh` overrides).

Schema
------
Binance Vision kline CSVs carry no header and, in most monthly files, the
OpenTime is in **microseconds** while daily files use milliseconds. Both are
normalised here. Columns (0-indexed):

    0 open_time  1 open  2 high  3 low  4 close  5 volume
    6 close_time  7 quote_volume  8 trades  9 taker_buy_base
    10 taker_buy_quote  11 ignore

Usage
-----
    .venv/bin/python scripts/feeds/binance_vision_bulk.py --probe            # listing months only
    .venv/bin/python scripts/feeds/binance_vision_bulk.py --panel BTCUSDT    # one pair
    .venv/bin/python scripts/feeds/binance_vision_bulk.py                    # the frozen Q9 universe
    .venv/bin/python scripts/feeds/binance_vision_bulk.py --list             # what is cached

No credentials are read or written. Nothing here is specific to a live account.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "reports" / ".binance_cache"
PANELS = CACHE / "panels"
DAILY = CACHE / "daily"

BASE_MONTHLY = ("https://data.binance.vision/data/spot/monthly/klines/"
                "{s}/{i}/{s}-{i}-{y}-{m:02d}.zip")
BASE_DAILY = ("https://data.binance.vision/data/spot/daily/klines/"
              "{s}/{i}/{s}-{i}-{y}-{m:02d}-{d:02d}.zip")

# The Q9 frozen universe: 30 USDT pairs, large -> small liquidity, so the
# size/liquidity thesis gets a fair shot. Listing months were probed, not
# guessed; a pair whose archive starts later simply has a shorter panel.
Q9_UNIVERSE = [
    # majors
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    # large-cap alts
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
    "LTCUSDT", "TRXUSDT", "BCHUSDT", "ATOMUSDT", "ETCUSDT",
    # mid-cap alts
    "NEARUSDT", "UNIUSDT", "FILUSDT", "APTUSDT", "ARBUSDT",
    "OPUSDT", "INJUSDT", "SUIUSDT", "RUNEUSDT", "AAVEUSDT",
    # small-cap / newer
    "SEIUSDT", "TIAUSDT", "WLDUSDT", "PEPEUSDT", "JUPUSDT",
]

FIELDS = ["t", "o", "h", "l", "c", "v", "qv", "n", "tb", "tbq"]


@dataclass
class Panel:
    """Parsed 5-minute panel. Arrays are sorted, unique and float64."""
    symbol: str
    interval: str
    t: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    qv: np.ndarray
    n: np.ndarray
    tb: np.ndarray

    def __len__(self) -> int:
        return len(self.t)

    @property
    def span(self) -> tuple[dt.datetime, dt.datetime]:
        return (dt.datetime.fromtimestamp(int(self.t[0]) / 1000, dt.timezone.utc),
                dt.datetime.fromtimestamp(int(self.t[-1]) / 1000, dt.timezone.utc))


# --------------------------------------------------------------- networking
def _head(url: str, timeout: int = 25) -> int:
    """HTTP status for a URL; 404 is a normal answer here."""
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0"})
        return urllib.request.urlopen(req, timeout=timeout).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _get(url: str, timeout: int = 60) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        return urllib.request.urlopen(req, timeout=timeout).read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception:
        return None


def _months(first: tuple[int, int], last: tuple[int, int]) -> list[tuple[int, int]]:
    out = []
    y, m = first
    while (y, m) <= last:
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def month_exists(symbol: str, interval: str, y: int, m: int) -> bool:
    return _head(BASE_MONTHLY.format(s=symbol, i=interval, y=y, m=m)) == 200


def listing_month(symbol: str, interval: str = "5m") -> tuple[int, int] | None:
    """Earliest month with a monthly archive. Scans forward; None if unseen."""
    for y in range(2017, dt.datetime.now(dt.timezone.utc).year + 1):
        for m in range(1, 13):
            if (y, m) > (dt.datetime.now(dt.timezone.utc).year,
                         dt.datetime.now(dt.timezone.utc).month):
                return None
            if month_exists(symbol, interval, y, m):
                return (y, m)
    return None


# ------------------------------------------------------------------- parsing
def _parse_csv(raw: bytes) -> dict[str, np.ndarray]:
    """Parse one kline CSV. Handles the microsecond-vs-millisecond OpenTime
    inconsistency between Binance's monthly and daily archives, and the
    microsecond `open_time` column."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {k: np.empty(0) for k in FIELDS}
    rows = []
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) < 11:
            continue
        try:
            rows.append([float(x) for x in parts[:11]])
        except ValueError:
            continue          # a header row, if any
    if not rows:
        return {k: np.empty(0) for k in FIELDS}
    a = np.asarray(rows, dtype=np.float64)
    ts = a[:, 0]
    # microseconds if the value is implausibly large for milliseconds
    if len(ts) and ts[0] > 1e14:
        ts = ts / 1000.0
    return {
        "t": ts.astype(np.int64),
        "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4], "v": a[:, 5],
        "qv": a[:, 7], "n": a[:, 8], "tb": a[:, 9], "tbq": a[:, 10],
    }


def _read_zip(path: Path) -> dict[str, np.ndarray]:
    with zipfile.ZipFile(path) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            return _parse_csv(f.read())


# ----------------------------------------------------------------- download
def _fetch_month(symbol: str, interval: str, y: int, m: int,
                 refresh: bool) -> tuple[str, bool, int]:
    """Download one monthly zip if absent. Returns (name, fetched, bytes)."""
    dest = CACHE / f"{symbol}-{interval}-{y}-{m:02d}.zip"
    if dest.exists() and not refresh:
        return (dest.name, False, dest.stat().st_size)
    blob = _get(BASE_MONTHLY.format(s=symbol, i=interval, y=y, m=m))
    if blob is None:
        return (dest.name, False, 0)          # 404: not listed yet
    dest.write_bytes(blob)
    return (dest.name, True, len(blob))


def _fetch_day(symbol: str, interval: str, d: dt.date,
               refresh: bool) -> tuple[str, bool, int]:
    DAILY.mkdir(parents=True, exist_ok=True)
    dest = DAILY / f"{symbol}-{interval}-{d:%Y-%m-%d}.zip"
    if dest.exists() and not refresh:
        return (dest.name, False, dest.stat().st_size)
    blob = _get(BASE_DAILY.format(s=symbol, i=interval, y=d.year,
                                  m=d.month, d=d.day))
    if blob is None:
        return (dest.name, False, 0)
    dest.write_bytes(blob)
    return (dest.name, True, len(blob))


def download(symbol: str, interval: str = "5m",
             first: tuple[int, int] | None = None,
             last: tuple[int, int] | None = None,
             refresh: bool = False, workers: int = 8,
             include_partial_month: bool = True, quiet: bool = False,
             ) -> list[Path]:
    """Idempotently cache monthly zips for one pair, plus daily zips for the
    current (partial) month. Returns the list of monthly zip paths, sorted.

    `first` defaults to the pair's listing month. `last` defaults to the last
    completed month (the partial current month is covered by daily zips).
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    if first is None:
        first = listing_month(symbol, interval)
        if first is None:
            raise SystemExit(f"{symbol}: no monthly archive found (unlisted?)")
    # last completed month
    prev = now.replace(day=1) - dt.timedelta(days=1)
    last_complete = (prev.year, prev.month)
    if last is None:
        last = last_complete
    months = _months(first, last)
    if not quiet:
        print(f"  {symbol}: {len(months)} monthly zips "
              f"{first[0]}-{first[1]:02d} .. {last[0]}-{last[1]:02d}")
    got: list[Path] = []
    total_new = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_fetch_month, symbol, interval, y, m, refresh)
                for (y, m) in months]
        for fut in cf.as_completed(futs):
            name, fetched, size = fut.result()
            if fetched:
                total_new += size
            got.append(CACHE / name)

    # daily zips for the current partial month, up to yesterday (today's bar
    # is still forming and its daily zip does not exist yet)
    if include_partial_month:
        d = now.replace(day=1).date()
        while d < now.date():
            _, fetched, size = _fetch_day(symbol, interval, d, refresh)
            if fetched:
                total_new += size
            d += dt.timedelta(days=1)

    if not quiet:
        print(f"    new bytes this call: {total_new/1e6:.2f} MB "
              f"({len(got)} monthly files cached)")
    return sorted(got)


def build_panel(symbol: str, interval: str = "5m",
                refresh: bool = False) -> Panel:
    """Fetch if needed, then assemble a sorted, de-duplicated Panel and write
    it to `panels/<SYM>_<interval>.npz`. Returns the Panel.

    Sorting and de-duplication matter: Binance restates a bar occasionally and
    a restart boundary can duplicate the last bar of one file at the head of
    the next. A study must not see a duplicated or out-of-order bar.
    """
    download(symbol, interval, refresh=refresh, quiet=True)
    zips = sorted(CACHE.glob(f"{symbol}-{interval}-*.zip"))
    zips += sorted(DAILY.glob(f"{symbol}-{interval}-*.zip"))
    if not zips:
        raise SystemExit(f"{symbol}: nothing cached after download")

    acc: dict[str, list[np.ndarray]] = {k: [] for k in FIELDS}
    for z in zips:
        try:
            part = _read_zip(z)
        except zipfile.BadZipFile:
            print(f"    WARNING: corrupt zip skipped: {z.name}", file=sys.stderr)
            continue
        for k in FIELDS:
            acc[k].append(part[k])
    cat = {k: np.concatenate(v) if v else np.empty(0) for k, v in acc.items()}

    order = np.argsort(cat["t"], kind="stable")
    for k in FIELDS:
        cat[k] = cat[k][order]
    uniq = np.concatenate([[True], np.diff(cat["t"]) != 0])
    for k in FIELDS:
        cat[k] = cat[k][uniq]

    panel = Panel(symbol=symbol, interval=interval,
                  t=cat["t"], o=cat["o"], h=cat["h"], l=cat["l"], c=cat["c"],
                  v=cat["v"], qv=cat["qv"], n=cat["n"], tb=cat["tb"])
    PANELS.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(PANELS / f"{symbol}_{interval}.npz", **{
        k: getattr(panel, k) for k in
        ("t", "o", "h", "l", "c", "v", "qv", "n", "tb")})
    return panel


def load_panel(symbol: str, interval: str = "5m") -> Panel:
    """Load a cached panel from `panels/`; builds it if absent."""
    f = PANELS / f"{symbol}_{interval}.npz"
    if not f.exists():
        return build_panel(symbol, interval)
    z = np.load(f)
    return Panel(symbol=symbol, interval=interval, **{k: z[k] for k in
                 ("t", "o", "h", "l", "c", "v", "qv", "n", "tb")})


# ------------------------------------------------------------------- CLI
def _cmd_list() -> None:
    if not PANELS.exists():
        print("no panels cached")
        return
    for f in sorted(PANELS.glob("*.npz")):
        z = np.load(f)
        t = z["t"]
        t0 = dt.datetime.fromtimestamp(int(t[0]) / 1000, dt.timezone.utc)
        t1 = dt.datetime.fromtimestamp(int(t[-1]) / 1000, dt.timezone.utc)
        print(f"  {f.stem:16s} {len(t):>9,} bars  {t0:%Y-%m-%d} -> {t1:%Y-%m-%d}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="*", default=None,
                    help="symbols to fetch (default: the frozen Q9 universe)")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--panel", nargs="*", default=None,
                    help="build panels for these symbols and print a structure "
                         "report (counts and span only)")
    ap.add_argument("--probe", action="store_true",
                    help="report listing months only; download nothing")
    ap.add_argument("--list", action="store_true", help="list cached panels")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download files already on disk")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if args.list:
        _cmd_list()
        return

    syms = args.symbols or Q9_UNIVERSE
    if args.probe:
        print("listing months (monthly archive present):")
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            for s, fm in zip(syms, ex.map(lambda x: listing_month(x, args.interval), syms)):
                print(f"  {s:14s} {fm}")
        return

    targets = args.panel if args.panel is not None else syms
    print("=" * 78)
    print(f"Binance Vision bulk 5m  |  {len(targets)} pairs  |  cache {CACHE}")
    print("=" * 78)
    grand_bars = 0
    for s in targets:
        p = build_panel(s, args.interval, refresh=args.refresh)
        grand_bars += len(p)
        t0, t1 = p.span
        print(f"  {s:12s} {len(p):>9,} bars  {t0:%Y-%m-%d %H:%M} -> {t1:%Y-%m-%d %H:%M}")
    print(f"\ntotal bars cached this run: {grand_bars:,}")
    print("Nothing here is a live order or a claim about returns.")


if __name__ == "__main__":
    main()
