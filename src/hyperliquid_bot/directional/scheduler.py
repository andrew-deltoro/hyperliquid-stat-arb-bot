"""
Directional perp cycle.

Runs once daily (02:00 UTC). Universe is small (BTC, SOL by default).
Total account equity is split between pairs bot and directional bot
by config-level caps, not by shared balance.
"""
from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path

from ..phase3.paths import LOGS_DIR, STATE_DIR
from .config import load_dir_config
from .signals import generate_signal
from .positions import DirPositionManager
from .executor import DirectionalExecutor


def _log(msg: str, fh=None):
    print(msg)
    if fh:
        fh.write(msg + "\n")
        fh.flush()


def run_cycle(config_path=None, log_path=None):
    cfg = load_dir_config(config_path)

    if log_path is None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = LOGS_DIR / f"directional_{stamp}.log"

    with open(log_path, "w") as fh:
        _log(f"=== Directional cycle: {log_path.name} ===", fh)

        executor = DirectionalExecutor(cfg, log_fn=lambda m: _log(m, fh))
        pm = DirPositionManager(cfg, executor, log_fn=lambda m: _log(m, fh))

        # Fetch main-dex equity (this strategy trades the main dex only)
        try:
            state = executor.info.user_state(executor.wallet)
            margin = state.get("marginSummary", {})
            equity = float(margin.get("accountValue", 0))
            margin_used = float(margin.get("totalMarginUsed", 0))
            if equity < 1:
                # fallback spot
                spot = executor.info.spot_user_state(executor.wallet)
                for b in spot.get("balances", []):
                    if b.get("coin") == "USDC":
                        equity = float(b.get("total", 0))
                        break
        except Exception as e:
            _log(f"[DIR] equity fetch failed: {e}", fh)
            return log_path

        # Leave the margin other positions on this account are using, times
        # a buffer, plus a fixed floor, untouched.
        dcfg = cfg.raw.get("directional", {})
        reserve = max(
            float(dcfg.get("pairs_maintenance_reserve_usd", 300.0)),
            margin_used * float(dcfg.get("margin_used_buffer", 2.0)),
        )
        pairs_maint = reserve
        dir_budget = dcfg.get("directional_budget_usd", 500.0)
        dir_equity = max(0.0, min(dir_budget, equity - pairs_maint))
        if dir_equity < cfg.directional.get("min_equity", 100):
            _log(f"[DIR] insufficient headroom (equity={equity:.2f}, "
                 f"pairs_maint={pairs_maint:.2f}, budget={dir_budget:.2f}, "
                 f"usable={dir_equity:.2f}) — skip", fh)
            return log_path

        _log(f"[DIR] account equity: ${equity:.2f}  "
             f"pairs maintenance: ${pairs_maint:.2f}  "
             f"directional budget: ${dir_budget:.2f}  "
             f"usable: ${dir_equity:.2f}", fh)

        _log(f"[DIR] open positions: {pm.open_count()}  "
             f"notional: ${pm.total_open_notional():.2f}", fh)
        _log("", fh)

        # ---------- 1. Evaluate existing ----------
        _log("--- Evaluating open positions ---", fh)
        for pos in list(pm.positions):
            if pos["status"] != "open":
                continue
            coin = pos["coin"]
            px = executor.get_mid(coin)
            if px <= 0:
                _log(f"[{coin}] price fetch failed", fh)
                continue
            # We need a signal for flip detection
            sig = generate_signal(coin, equity=dir_equity,
                                  leverage=cfg.directional["leverage"])
            result = pm.evaluate(coin, px, sig)
            if result["action"] == "closed":
                _log(f"[{coin}] ✅ CLOSED {result['reason']} "
                     f"pnl=${result['pnl_usd']:+.2f}", fh)
            elif result["action"] == "hold":
                _log(f"[{coin}] holding  unreal=${result['unrealized_usd']:+.2f}",
                     fh)
            elif result["action"] == "close_failed":
                _log(f"[{coin}] close failed: {result.get('error')}", fh)

        _log("", fh)

        # ---------- 2. Open new ----------
        _log("--- Generating signals ---", fh)
        max_notional = cfg.directional["max_notional_per_position"]
        max_conc = cfg.directional["max_concurrent_positions"]
        leverage = cfg.directional["leverage"]

        for coin in cfg.directional["universe"]:
            sig = generate_signal(coin, equity=dir_equity, leverage=leverage)
            if sig is None:
                _log(f"[{coin}] no signal", fh)
                continue
            _log(f"[{coin}] close={sig.close:.4f} ma={sig.ma:.4f} "
                 f"mom={sig.momentum*100:+.2f}% volr={sig.vol_ratio:.2f} "
                 f"atr={sig.atr:.4f} trend={sig.trend} → {sig.action}", fh)

            if sig.action in ("OPEN_LONG", "OPEN_SHORT"):
                res = pm.try_open(sig, equity=dir_equity, leverage=leverage,
                                  max_concurrent=max_conc,
                                  max_notional_per_pos=max_notional)
                if res["opened"]:
                    _log(f"[{coin}] ✅ OPENED {sig.direction} "
                         f"notional=${res['position']['notional']:.2f}", fh)
                else:
                    _log(f"[{coin}] ✗ not opened: {res['reason']}", fh)

        _log("", fh)
        _log(f"Cycle end. Open positions: {pm.open_count()}  "
             f"notional: ${pm.total_open_notional():.2f}", fh)

    return log_path


if __name__ == "__main__":
    run_cycle()
