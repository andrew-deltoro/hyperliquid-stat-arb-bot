"""Load phase3.yaml config."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "phase3.yaml"


@dataclass
class Phase3Config:
    pairs: list
    data: dict
    model: dict
    signal: dict
    position: dict
    risk: dict
    ou_prior: dict
    raw: dict = field(default_factory=dict)


def load_config(path: Path | None = None) -> Phase3Config:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(f"Config not found: {p}")
    with open(p) as f:
        raw = yaml.safe_load(f)
    return Phase3Config(
        pairs=[tuple(x) for x in raw["pairs"]],
        data=raw["data"],
        model=raw["model"],
        signal=raw["signal"],
        position=raw["position"],
        risk=raw["risk"],
        ou_prior=raw["ou_prior"],
        raw=raw,
    )