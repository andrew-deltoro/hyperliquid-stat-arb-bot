#!/usr/bin/env python3
"""Run the locked, public-data-only perp/oracle convergence paper test."""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.hyperliquid_bot.funding.hypothesis import (  # noqa: E402
    fee_for_fill,
    json_dumps,
    quantize_quantity,
    walk_book,
)
from src.hyperliquid_bot.oracle_convergence import (  # noqa: E402
    convergence_bps,
    favorable_edge_bps,
    impact_premium,
    init_db,
    position_pnl,
    rank_tails,
)

DEFAULT_CONFIG = REPO / "config" / "oracle_convergence_hypothesis.yaml"


class PublicInfo:
    def __init__(self, url: str):
        self.url = url
        self.session = requests.Session()

    def post(self, payload: dict):
        response = self.session.post(self.url, json=payload, timeout=20)
        response.raise_for_status()
        return response.json()

    def state(self) -> dict:
        meta, contexts = self.post({"type": "metaAndAssetCtxs"})
        return {
            asset["name"]: {"meta": asset, "ctx": context}
            for asset, context in zip(meta["universe"], contexts)
        }

    def book(self, coin: str) -> dict:
        book = self.post({"type": "l2Book", "coin": coin})
        if not book or len(book.get("levels", [])) != 2:
            raise RuntimeError(f"no usable book for {coin}")
        return book


def eligible_rows(state: dict, cfg: dict) -> list[dict]:
    rows = []
    floor = float(cfg["universe"]["minimum_day_volume_usd"])
    for coin, market in state.items():
        ctx = market["ctx"]
        impact = ctx.get("impactPxs")
        try:
            oracle = float(ctx["oraclePx"])
            bid, ask = float(impact[0]), float(impact[1])
            volume = float(ctx["dayNtlVlm"])
            oi = float(ctx["openInterest"])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if volume < floor or oracle <= 0 or (cfg["universe"]["require_open_interest"] and oi <= 0):
            continue
        rows.append(
            {
                "coin": coin,
                "oracle": oracle,
                "impact_bid": bid,
                "impact_ask": ask,
                "premium": impact_premium(oracle, bid, ask),
                "day_volume": volume,
                "sz_decimals": int(market["meta"]["szDecimals"]),
            }
        )
    return rows


def close_due(conn, api: PublicInfo, state: dict, cfg: dict, now_ms: int) -> list[dict]:
    events = []
    fee_rate = float(cfg["execution_model"]["perp_taker_fee_rate"])
    due = conn.execute(
        "SELECT * FROM positions WHERE status='open' AND due_ts_ms<=?", (now_ms,)
    ).fetchall()
    for pos in due:
        coin, side = pos["coin"], pos["side"]
        market = state.get(coin)
        if not market:
            continue
        book = api.book(coin)
        levels = book["levels"][0] if side == "long" else book["levels"][1]
        fill = walk_book(levels, float(pos["quantity"]))
        if fill is None:
            events.append({"type": "close_deferred", "coin": coin, "reason": "book_depth"})
            continue
        oracle = float(market["ctx"]["oraclePx"])
        gross = position_pnl(
            side, float(pos["quantity"]), float(pos["entry_px"]), fill.average_price
        )
        fees = float(pos["fees_usd"]) + fee_for_fill(fill.notional, fee_rate)
        net = gross - fees
        conv = convergence_bps(
            side,
            float(pos["entry_px"]),
            float(pos["entry_oracle"]),
            fill.average_price,
            oracle,
        )
        conn.execute(
            """UPDATE positions SET status='closed', exit_ts_ms=?, exit_px=?,
               exit_oracle=?, exit_notional=?, fees_usd=?, gross_pnl_usd=?,
               net_pnl_usd=?, exit_basis_bps=?, convergence_bps=?
               WHERE cohort_hour=? AND coin=?""",
            (
                now_ms,
                fill.average_price,
                oracle,
                fill.notional,
                fees,
                gross,
                net,
                (fill.average_price / oracle - 1.0) * 10_000,
                conv,
                pos["cohort_hour"],
                coin,
            ),
        )
        events.append(
            {"type": "paper_close", "cohort": pos["cohort_hour"], "coin": coin, "net_pnl": net}
        )
    for cohort in {row["cohort_hour"] for row in due}:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE cohort_hour=? AND status='open'", (cohort,)
        ).fetchone()[0]
        if remaining == 0:
            conn.execute(
                "UPDATE cohorts SET status='complete', completed_ts_ms=? WHERE cohort_hour=?",
                (now_ms, cohort),
            )
    conn.commit()
    return events


def record_paths(conn, api: PublicInfo, state: dict, cfg: dict, now_ms: int) -> int:
    """Record executable early-exit marks without changing the four-hour rule."""
    interval_ms = int(cfg["measurement"].get("path_interval_minutes", 15) * 60_000)
    fee_rate = float(cfg["execution_model"]["perp_taker_fee_rate"])
    open_rows = conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()
    books = {}
    written = 0
    for pos in open_rows:
        last = conn.execute(
            """SELECT MAX(ts_ms) FROM path_observations
               WHERE cohort_hour=? AND coin=?""",
            (pos["cohort_hour"], pos["coin"]),
        ).fetchone()[0]
        if last is not None and now_ms - int(last) < interval_ms:
            continue
        market = state.get(pos["coin"])
        if market is None:
            continue
        if pos["coin"] not in books:
            books[pos["coin"]] = api.book(pos["coin"])
        book = books[pos["coin"]]
        levels = book["levels"][0] if pos["side"] == "long" else book["levels"][1]
        fill = walk_book(levels, float(pos["quantity"]))
        if fill is None:
            continue
        oracle = float(market["ctx"]["oraclePx"])
        gross = position_pnl(
            pos["side"], float(pos["quantity"]), float(pos["entry_px"]), fill.average_price
        )
        total_fees = float(pos["fees_usd"]) + fee_for_fill(fill.notional, fee_rate)
        conv = convergence_bps(
            pos["side"],
            float(pos["entry_px"]),
            float(pos["entry_oracle"]),
            fill.average_price,
            oracle,
        )
        conn.execute(
            """INSERT OR IGNORE INTO path_observations
               (cohort_hour, coin, ts_ms, elapsed_minutes, oracle_px,
                executable_exit_px, exit_basis_bps, convergence_bps,
                gross_pnl_if_exit_usd, net_pnl_if_exit_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pos["cohort_hour"],
                pos["coin"],
                now_ms,
                (now_ms - int(pos["entry_ts_ms"])) / 60_000,
                oracle,
                fill.average_price,
                (fill.average_price / oracle - 1.0) * 10_000,
                conv,
                gross,
                gross - total_fees,
            ),
        )
        written += 1
    conn.commit()
    return written


def evaluate(conn, api: PublicInfo, state: dict, cfg: dict, now: datetime, now_ms: int):
    if now.minute > int(cfg["entry"]["grace_minutes"]):
        return []
    every = int(cfg["entry"]["evaluate_every_hours"])
    if now.hour % every:
        return []
    cohort = now.replace(minute=0, second=0, microsecond=0).isoformat()
    if conn.execute("SELECT 1 FROM cohorts WHERE cohort_hour=?", (cohort,)).fetchone():
        return []

    rows = eligible_rows(state, cfg)
    minimum = int(cfg["universe"]["minimum_assets"])
    if len(rows) < minimum:
        conn.execute(
            "INSERT INTO cohorts VALUES (?, ?, 'skipped', ?, 0, ?)",
            (cohort, now_ms, len(rows), now_ms),
        )
        conn.commit()
        return [{"type": "cohort_skipped", "reason": "small_universe", "assets": len(rows)}]

    ranked = rank_tails(rows, float(cfg["signal"]["richest_fraction"]))
    selected = [row for row in ranked if row["selected_side"]]
    require_sign = bool(cfg["signal"]["require_signal_side"])
    sign_ok = all(
        (row["selected_side"] == "long" and row["premium"] < 0)
        or (row["selected_side"] == "short" and row["premium"] > 0)
        for row in selected
    )
    tail_size = len(selected) // 2
    conn.execute(
        "INSERT INTO cohorts VALUES (?, ?, ?, ?, ?, ?)",
        (
            cohort,
            now_ms,
            "open" if (sign_ok or not require_sign) else "skipped",
            len(rows),
            tail_size,
            None if (sign_ok or not require_sign) else now_ms,
        ),
    )
    conn.executemany(
        """INSERT INTO rankings
           (cohort_hour, coin, rank, oracle_px, impact_bid, impact_ask,
            premium, day_volume, selected_side)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                cohort,
                row["coin"],
                row["rank"],
                row["oracle"],
                row["impact_bid"],
                row["impact_ask"],
                row["premium"],
                row["day_volume"],
                row["selected_side"],
            )
            for row in ranked
        ],
    )
    if require_sign and not sign_ok:
        conn.commit()
        return [{"type": "cohort_skipped", "reason": "tails_not_across_oracle"}]

    notional = float(cfg["entry"]["notional_usd_per_position"])
    fee_rate = float(cfg["execution_model"]["perp_taker_fee_rate"])
    fills = []
    minimum_edge_bps = float(cfg["signal"].get("minimum_executable_edge_bps", 0.0))
    for row in selected:
        book = api.book(row["coin"])
        levels = book["levels"][1] if row["selected_side"] == "long" else book["levels"][0]
        reference = float(levels[0]["px"])
        quantity = quantize_quantity(notional / reference, row["sz_decimals"])
        fill = walk_book(levels, quantity) if quantity > 0 else None
        if fill:
            edge_bps = favorable_edge_bps(
                row["selected_side"], fill.average_price, row["oracle"]
            )
            if edge_bps >= minimum_edge_bps:
                fills.append((row, quantity, fill))

    longs = [value for value in fills if value[0]["selected_side"] == "long"]
    shorts = [value for value in fills if value[0]["selected_side"] == "short"]
    pair_count = min(len(longs), len(shorts))
    if pair_count == 0:
        conn.execute(
            "UPDATE cohorts SET status='skipped', completed_ts_ms=? WHERE cohort_hour=?",
            (now_ms, cohort),
        )
        conn.commit()
        return [{"type": "cohort_skipped", "reason": "cost_gate_or_book_depth"}]

    # Keep the most extreme successfully executable rows on each side.
    chosen = longs[:pair_count] + shorts[-pair_count:]
    due_ms = now_ms + int(cfg["exit"]["hold_hours"] * 3_600_000)
    for row, quantity, fill in chosen:
        conn.execute(
            """INSERT INTO positions
               (cohort_hour, coin, side, status, entry_ts_ms, due_ts_ms,
                quantity, entry_px, entry_oracle, entry_notional, fees_usd,
                entry_basis_bps)
               VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cohort,
                row["coin"],
                row["selected_side"],
                now_ms,
                due_ms,
                quantity,
                fill.average_price,
                row["oracle"],
                fill.notional,
                fee_for_fill(fill.notional, fee_rate),
                (fill.average_price / row["oracle"] - 1.0) * 10_000,
            ),
        )
    conn.commit()
    return [
        {
            "type": "paper_cohort_open",
            "cohort": cohort,
            "longs": pair_count,
            "shorts": pair_count,
            "gross_notional": sum(value[2].notional for value in chosen),
        }
    ]


def cycle(cfg: dict, conn) -> list[dict]:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    api = PublicInfo(cfg["api_url"])
    state = api.state()
    events = close_due(conn, api, state, cfg, now_ms)
    paths = record_paths(conn, api, state, cfg, now_ms)
    if paths:
        events.append({"type": "path_observations", "count": paths})
    events += evaluate(conn, api, state, cfg, now, now_ms)
    return events


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    cfg = yaml.safe_load(args.config.read_text())
    if cfg.get("mode") != "paper":
        raise RuntimeError("oracle-convergence test must remain in paper mode")
    db_path = Path(cfg["outputs"]["database"])
    if not db_path.is_absolute():
        db_path = REPO / db_path
    conn = init_db(db_path)
    while True:
        try:
            print(
                json_dumps(
                    {"ts": datetime.now(timezone.utc).isoformat(), "events": cycle(cfg, conn)}
                ),
                flush=True,
            )
        except Exception as exc:
            print(json_dumps({"error": str(exc)}), file=sys.stderr, flush=True)
            if not args.loop:
                raise
        if not args.loop:
            return
        time.sleep(int(cfg["measurement"]["collector_interval_seconds"]))


if __name__ == "__main__":
    main()
