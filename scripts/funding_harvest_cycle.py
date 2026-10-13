#!/usr/bin/env python3
"""Collect and paper-trade the locked funding-harvest hypothesis.

This script only calls Hyperliquid's public /info endpoint. It has no wallet,
private-key, transfer, or order code.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.hyperliquid_bot.funding.hypothesis import (  # noqa: E402
    book_mid,
    closest_oracle,
    fee_for_fill,
    four_leg_pnl,
    funding_cashflow_short,
    init_db,
    json_dumps,
    persistently_positive,
    quantize_quantity,
    walk_book,
)

DEFAULT_CONFIG = REPO / "config" / "funding_harvest_hypothesis.yaml"


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text())
    if cfg.get("mode") != "paper":
        raise RuntimeError("funding hypothesis must remain in paper mode")
    return cfg


class PublicInfo:
    def __init__(self, url: str):
        self.url = url
        self.session = requests.Session()

    def post(self, payload: dict):
        response = self.session.post(self.url, json=payload, timeout=20)
        response.raise_for_status()
        return response.json()

    def market_state(self) -> tuple[dict, dict, dict, dict]:
        perp_meta, perp_ctxs = self.post({"type": "metaAndAssetCtxs"})
        spot_meta, spot_ctxs = self.post({"type": "spotMetaAndAssetCtxs"})
        perps = {
            asset["name"]: (asset, ctx)
            for asset, ctx in zip(perp_meta["universe"], perp_ctxs)
        }
        # Spot context arrays can contain stale/delisted rows. Key by `coin`;
        # pairing them by list position can silently assign the wrong price.
        spots = {ctx["coin"]: ctx for ctx in spot_ctxs}
        return perps, spot_meta, spots, perp_meta

    def book(self, coin: str) -> dict:
        value = self.post({"type": "l2Book", "coin": coin})
        if not value or len(value.get("levels", [])) != 2:
            raise RuntimeError(f"no usable book for {coin}")
        return value

    def funding_history(self, coin: str, start_ms: int) -> list[dict]:
        return self.post({"type": "fundingHistory", "coin": coin, "startTime": start_ms})


def validate_assets(cfg: dict, perps: dict, spot_meta: dict, spots: dict) -> dict:
    tokens = {token["index"]: token for token in spot_meta["tokens"]}
    pairs = {pair["name"]: pair for pair in spot_meta["universe"]}
    valid = {}
    for coin, expected in cfg["assets"].items():
        pair_name = expected["spot_pair"]
        pair = pairs.get(pair_name)
        if coin not in perps or pair is None or pair_name not in spots:
            raise RuntimeError(f"configured market missing: {coin} / {pair_name}")
        base_idx, quote_idx = pair["tokens"][:2]
        base, quote = tokens[base_idx], tokens[quote_idx]
        if (
            base["name"] != expected["spot_token"]
            or base["tokenId"].lower() != expected["token_id"].lower()
            or quote["name"] != "USDC"
        ):
            raise RuntimeError(f"spot identity changed for {coin}: refusing to collect")
        valid[coin] = {
            "pair": pair,
            "spot_ctx": spots[pair_name],
            "perp_meta": perps[coin][0],
            "perp_ctx": perps[coin][1],
        }
    return valid


def store_public_data(
    conn: sqlite3.Connection,
    api: PublicInfo,
    assets: dict,
    now_ms: int,
    history_start_ms: int,
) -> None:
    for coin, market in assets.items():
        ctx = market["perp_ctx"]
        conn.execute(
            """INSERT OR REPLACE INTO oracle_snapshots
               (ts_ms, coin, oracle_px, funding_rate, mark_px, perp_day_volume)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                now_ms,
                coin,
                float(ctx["oraclePx"]),
                float(ctx["funding"]),
                float(ctx["markPx"]),
                float(ctx["dayNtlVlm"]),
            ),
        )
        history = api.funding_history(coin, history_start_ms)
        conn.executemany(
            """INSERT OR REPLACE INTO funding_observations
               (coin, ts_ms, funding_rate) VALUES (?, ?, ?)""",
            [
                (coin, int(row["time"]), float(row["fundingRate"]))
                for row in history
            ],
        )
    conn.commit()


def funding_rows(conn: sqlite3.Connection, coin: str, start_ms: int, end_ms: int):
    return [
        (int(row["ts_ms"]), float(row["funding_rate"]))
        for row in conn.execute(
            """SELECT ts_ms, funding_rate FROM funding_observations
               WHERE coin=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms""",
            (coin, start_ms, end_ms),
        )
    ]


def oracle_rows(conn: sqlite3.Connection, coin: str, start_ms: int, end_ms: int):
    return [
        (int(row["ts_ms"]), float(row["oracle_px"]))
        for row in conn.execute(
            """SELECT ts_ms, oracle_px FROM oracle_snapshots
               WHERE coin=? AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms""",
            (coin, start_ms, end_ms),
        )
    ]


def close_positions(
    conn: sqlite3.Connection,
    api: PublicInfo,
    cfg: dict,
    assets: dict,
    now_ms: int,
) -> list[dict]:
    events = []
    tolerance_ms = int(cfg["measurement"]["nearest_oracle_tolerance_minutes"] * 60_000)
    spot_fee = float(cfg["execution_model"]["spot_taker_fee_rate"])
    perp_fee = float(cfg["execution_model"]["perp_taker_fee_rate"])
    for pos in conn.execute("SELECT * FROM positions WHERE status='open'").fetchall():
        rates = funding_rows(conn, pos["coin"], pos["entry_ts_ms"] + 1, now_ms)
        negatives = [ts for ts, rate in rates if rate < 0]
        due = now_ms >= int(pos["planned_exit_ts_ms"])
        if not negatives and not due:
            continue
        reason = "negative_funding" if negatives else "seven_days"
        coin = pos["coin"]
        quantity = float(pos["quantity"])
        spot_book = api.book(cfg["assets"][coin]["spot_pair"])
        perp_book = api.book(coin)
        spot_fill = walk_book(spot_book["levels"][0], quantity)
        perp_fill = walk_book(perp_book["levels"][1], quantity)
        if spot_fill is None or perp_fill is None:
            events.append({"type": "close_deferred", "coin": coin, "reason": "insufficient_book"})
            continue

        oracles = oracle_rows(
            conn, coin, int(pos["entry_ts_ms"]) - tolerance_ms, now_ms + tolerance_ms
        )
        funding = 0.0
        missing = []
        for ts, rate in rates:
            oracle = closest_oracle(oracles, ts, tolerance_ms)
            if oracle is None:
                missing.append(ts)
            else:
                funding += funding_cashflow_short(quantity, oracle, rate)
        exit_fees = fee_for_fill(spot_fill.notional, spot_fee) + fee_for_fill(
            perp_fill.notional, perp_fee
        )
        all_fees = float(pos["fees_usd"]) + exit_fees
        pnl = four_leg_pnl(
            quantity,
            float(pos["entry_spot_px"]),
            float(pos["entry_perp_px"]),
            spot_fill.average_price,
            perp_fill.average_price,
            funding,
            all_fees,
        )
        valid = not missing
        invalid_reason = None if valid else f"missing oracle near {len(missing)} funding print(s)"
        conn.execute(
            """UPDATE positions SET status='closed', exit_ts_ms=?, exit_reason=?,
               exit_spot_px=?, exit_perp_px=?, exit_spot_notional=?,
               exit_perp_notional=?, fees_usd=?, funding_usd=?, net_pnl_usd=?,
               valid=?, invalid_reason=? WHERE id=?""",
            (
                now_ms,
                reason,
                spot_fill.average_price,
                perp_fill.average_price,
                spot_fill.notional,
                perp_fill.notional,
                all_fees,
                funding,
                pnl,
                int(valid),
                invalid_reason,
                pos["id"],
            ),
        )
        events.append(
            {
                "type": "paper_close",
                "coin": coin,
                "reason": reason,
                "net_pnl_usd": pnl,
                "valid": valid,
            }
        )
    conn.commit()
    return events


def in_entry_window(now: datetime, cfg: dict) -> bool:
    entry = cfg["entry"]
    target = now.replace(
        hour=int(entry["evaluation_hour_utc"]),
        minute=int(entry["evaluation_minute_utc"]),
        second=0,
        microsecond=0,
    )
    return target <= now <= target + timedelta(minutes=int(entry["grace_minutes"]))


def record_decision(
    conn: sqlite3.Connection,
    date: str,
    coin: str,
    now_ms: int,
    eligible: bool,
    reason: str,
    details: dict,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO decisions
           (entry_date, coin, ts_ms, eligible, reason, details_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (date, coin, now_ms, int(eligible), reason, json_dumps(details)),
    )


def open_positions(
    conn: sqlite3.Connection,
    api: PublicInfo,
    cfg: dict,
    assets: dict,
    now: datetime,
    now_ms: int,
    force_evaluation: bool = False,
) -> list[dict]:
    if not force_evaluation and not in_entry_window(now, cfg):
        return []
    entry = cfg["entry"]
    execution = cfg["execution_model"]
    date = now.date().isoformat()
    events = []
    lookback_ms = int(entry["lookback_hours"] * 3_600_000)
    for coin, market in assets.items():
        if conn.execute(
            "SELECT 1 FROM decisions WHERE entry_date=? AND coin=?", (date, coin)
        ).fetchone():
            continue
        rates = funding_rows(conn, coin, now_ms - lookback_ms, now_ms)
        positive, reason = persistently_positive(
            rates,
            now_ms - lookback_ms,
            now_ms,
            int(entry["minimum_hourly_prints"]),
        )
        details = {"funding_prints": len(rates)}
        if not positive:
            record_decision(conn, date, coin, now_ms, False, reason, details)
            continue
        if (
            conn.execute(
                "SELECT 1 FROM positions WHERE coin=? AND status='open'", (coin,)
            ).fetchone()
            and entry["one_open_position_per_coin"]
        ):
            record_decision(conn, date, coin, now_ms, False, "position already open", details)
            continue
        day_volume = float(market["perp_ctx"]["dayNtlVlm"])
        if day_volume < float(entry["minimum_perp_day_volume_usd"]):
            record_decision(conn, date, coin, now_ms, False, "perp volume below floor", details)
            continue

        spot_book = api.book(cfg["assets"][coin]["spot_pair"])
        perp_book = api.book(coin)
        spot_mid, perp_mid = book_mid(spot_book), book_mid(perp_book)
        divergence = abs(spot_mid / perp_mid - 1.0)
        details.update(
            {"spot_mid": spot_mid, "perp_mid": perp_mid, "mid_divergence": divergence}
        )
        if divergence > float(entry["maximum_spot_perp_mid_divergence"]):
            record_decision(conn, date, coin, now_ms, False, "spot/perp identity guard", details)
            continue

        decimals = min(
            int(market["perp_meta"]["szDecimals"]),
            int(
                next(
                    token["szDecimals"]
                    for token in market["tokens"]
                    if token["tokenId"].lower()
                    == cfg["assets"][coin]["token_id"].lower()
                )
            ),
        )
        reference_px = max(
            float(spot_book["levels"][1][0]["px"]),
            float(perp_book["levels"][0][0]["px"]),
        )
        quantity = quantize_quantity(
            float(entry["intended_notional_usd"]) / reference_px, decimals
        )
        spot_fill = walk_book(spot_book["levels"][1], quantity)
        perp_fill = walk_book(perp_book["levels"][0], quantity)
        if quantity <= 0 or spot_fill is None or perp_fill is None:
            record_decision(conn, date, coin, now_ms, False, "insufficient visible book", details)
            continue

        fees = fee_for_fill(
            spot_fill.notional, float(execution["spot_taker_fee_rate"])
        ) + fee_for_fill(perp_fill.notional, float(execution["perp_taker_fee_rate"]))
        planned_exit = now_ms + int(cfg["exit"]["hold_hours"] * 3_600_000)
        details.update(
            {
                "quantity": quantity,
                "entry_spot_average": spot_fill.average_price,
                "entry_perp_average": perp_fill.average_price,
                "entry_fees_usd": fees,
            }
        )
        record_decision(conn, date, coin, now_ms, True, reason, details)
        conn.execute(
            """INSERT INTO positions
               (entry_date, coin, status, entry_ts_ms, planned_exit_ts_ms,
                quantity, entry_spot_px, entry_perp_px, entry_spot_notional,
                entry_perp_notional, fees_usd)
               VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                date,
                coin,
                now_ms,
                planned_exit,
                quantity,
                spot_fill.average_price,
                perp_fill.average_price,
                spot_fill.notional,
                perp_fill.notional,
                fees,
            ),
        )
        events.append({"type": "paper_open", "coin": coin, **details})
    conn.commit()
    return events


def cycle(cfg: dict, conn: sqlite3.Connection, force_evaluation: bool = False) -> list[dict]:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    api = PublicInfo(cfg["api_url"])
    perps, spot_meta, spots, _ = api.market_state()
    assets = validate_assets(cfg, perps, spot_meta, spots)
    tokens = spot_meta["tokens"]
    for market in assets.values():
        market["tokens"] = tokens
    history_start = now_ms - int((cfg["exit"]["hold_hours"] + 24) * 3_600_000)
    store_public_data(conn, api, assets, now_ms, history_start)
    events = close_positions(conn, api, cfg, assets, now_ms)
    events += open_positions(conn, api, cfg, assets, now, now_ms, force_evaluation)
    return events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument(
        "--evaluate-now",
        action="store_true",
        help="Testing only: run today's locked entry rules outside the daily window.",
    )
    args = parser.parse_args()
    cfg = load_config(args.config)
    db_path = Path(cfg["outputs"]["database"])
    if not db_path.is_absolute():
        db_path = REPO / db_path
    conn = init_db(db_path)
    interval = int(cfg["measurement"]["collector_interval_seconds"])
    while True:
        try:
            events = cycle(cfg, conn, force_evaluation=args.evaluate_now)
            stamp = datetime.now(timezone.utc).isoformat()
            print(json_dumps({"ts": stamp, "events": events}), flush=True)
        except Exception as exc:
            print(json_dumps({"error": str(exc)}), file=sys.stderr, flush=True)
            if not args.loop:
                raise
        if not args.loop:
            return
        time.sleep(interval)


if __name__ == "__main__":
    main()
