"""Load directional config."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT = REPO_ROOT / "config" / "directional-live.yaml"


@dataclass
class DirConfig:
    directional: dict
    executor: dict
    raw: dict = field(default_factory=dict)


def load_dir_config(path: Path | None = None) -> DirConfig:
    p = Path(path) if path else DEFAULT
    if not p.exists():
        raise FileNotFoundError(f"Directional config not found: {p}")
    with open(p) as f:
        raw = yaml.safe_load(f)
    return DirConfig(
        directional=raw["directional"],
        executor=raw.get("executor", {}),
        raw=raw,
    )
