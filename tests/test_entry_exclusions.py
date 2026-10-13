import csv
import json
from datetime import datetime, timezone

from src.hyperliquid_bot.phase3 import exclusions


NOW = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)


def test_manual_asset_exclusion_is_temporary():
    cfg = {
        "entry_exclusions": {
            "manual_assets": {
                "NEO": {
                    "until_utc": "2026-10-17T20:00:00+00:00",
                    "reason": "review",
                }
            }
        }
    }
    assert "NEO" in exclusions.asset_exclusions(cfg, now=NOW)
    later = datetime(2026, 10, 18, tzinfo=timezone.utc)
    assert "NEO" not in exclusions.asset_exclusions(cfg, now=later)


def test_three_consecutive_losing_legs_trigger_cooldown(tmp_path):
    path = tmp_path / "trades.csv"
    fields = [
        "pair", "direction", "exit_time_iso", "leg_a_usd", "leg_b_usd",
        "entry_price_a", "entry_price_b", "exit_price_a", "exit_price_b", "pnl_usd",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for day in (7, 8, 9):
            writer.writerow(
                {
                    "pair": "AAA/NEO",
                    "direction": "LONG_A_SHORT_B",
                    "exit_time_iso": f"2026-10-0{day}T20:00:00+00:00",
                    "leg_a_usd": 100,
                    "leg_b_usd": 100,
                    "entry_price_a": 1,
                    "entry_price_b": 1,
                    "exit_price_a": 1,
                    "exit_price_b": 1.1,
                    "pnl_usd": -10,
                }
            )
    cfg = {
        "entry_exclusions": {
            "performance": {
                "enabled": True,
                "lookback_days": 30,
                "minimum_closed_legs": 3,
                "consecutive_losing_legs": 3,
                "cooldown_hours": 168,
            }
        }
    }
    blocked = exclusions.asset_exclusions(cfg, now=NOW, trades_file=path)
    assert "NEO" in blocked
    assert "AAA" not in blocked


def test_red_fresh_pair_health_blocks_entry(tmp_path, monkeypatch):
    path = tmp_path / "pair_health.json"
    path.write_text(
        json.dumps(
            {
                "last_run": NOW.isoformat(),
                "pairs": {
                    "AAVE/COMP": {"status": "red"},
                    "ETC/NEO": {"status": "green"},
                },
            }
        )
    )
    monkeypatch.setattr(exclusions, "PAIR_HEALTH_FILE", path)
    cfg = {
        "entry_exclusions": {
            "pair_health": {
                "enabled": True,
                "blocked_statuses": ["red"],
                "maximum_age_hours": 192,
            }
        }
    }
    blocked = exclusions.pair_health_blocks(cfg, now=NOW)
    assert "AAVE/COMP" in blocked
    assert "ETC/NEO" not in blocked
    assert exclusions.entry_block_reason("AAVE/COMP", {}, blocked)
