"""
Skylark FLP regime classifier.

Regimes:
  FAVORABLE      FLP < 50
  TRANSITIONAL   50 <= FLP < 65
  DEFENSIVE      FLP >= 65

Reads the current regime from macro_regime.json (written by skylark.py) OR
accepts an explicit FLP value. Pure function -- no I/O side effects beyond
the optional file read.
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Regime(str, Enum):
    FAVORABLE = "favorable"
    TRANSITIONAL = "transitional"
    DEFENSIVE = "defensive"


@dataclass
class RegimeReading:
    flp: float
    regime: Regime
    bias: str          # "long", "short", "flat"
    confidence: float  # 0..1, how far from nearest threshold
    ts: str            # iso from source file, or "" if explicit


def classify(flp: float) -> RegimeReading:
    if flp < 50:
        regime = Regime.FAVORABLE
        bias = "long"
        # Distance to transitional boundary
        confidence = min(1.0, (50.0 - flp) / 15.0)
    elif flp < 65:
        regime = Regime.TRANSITIONAL
        bias = "flat"
        # Middle of transitional band is the least confident
        midpoint = 57.5
        dist = abs(flp - midpoint)
        confidence = dist / 7.5
    else:
        regime = Regime.DEFENSIVE
        bias = "short"
        confidence = min(1.0, (flp - 65.0) / 15.0)
    return RegimeReading(flp=flp, regime=regime, bias=bias,
                         confidence=confidence, ts="")


def classify_from_state(state_dir: Path | None = None) -> RegimeReading | None:
    """Read macro_regime.json and classify. Returns None if file absent."""
    if state_dir is None:
        state_dir = Path("/home/hlbot/hlbot-runtime/live/state")
    f = state_dir / "macro_regime.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
    except Exception:
        return None
    flp = data.get("flp_monthly")
    if flp is None:
        return None
    r = classify(float(flp))
    r.ts = data.get("last_fetch", "")
    return r


def position_multiplier(r: RegimeReading) -> float:
    """Size multiplier from regime + confidence. Caps at 2x."""
    if r.regime == Regime.TRANSITIONAL:
        return 0.0
    base = 1.0
    if r.regime == Regime.FAVORABLE:
        base = 1.0 + 1.0 * r.confidence     # up to 2x
    elif r.regime == Regime.DEFENSIVE:
        base = 1.0 + 1.0 * r.confidence
    return round(base, 3)


if __name__ == "__main__":
    r = classify_from_state()
    if r:
        print(f"FLP: {r.flp:.2f}")
        print(f"Regime: {r.regime.value}")
        print(f"Bias: {r.bias}")
        print(f"Confidence: {r.confidence:.2f}")
        print(f"Size multiplier: {position_multiplier(r):.2f}x")
    else:
        print("No macro_regime.json found")
