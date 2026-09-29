"""
Centralized path resolution.

If HLBOT_RUNTIME_DIR is set, all runtime state (state/, reports/, logs/,
KILL) lives under that directory. Otherwise it falls back to the repo root
(backward-compatible with existing dry-run behavior).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Load .env from the repo root regardless of current working directory
load_dotenv(REPO_ROOT / ".env")


def _runtime_dir() -> Path:
    env = os.getenv("HLBOT_RUNTIME_DIR", "").strip()
    if env:
        return Path(env)
    return REPO_ROOT


RUNTIME_DIR = _runtime_dir()
STATE_DIR = RUNTIME_DIR / "state"
REPORTS_DIR = RUNTIME_DIR / "reports"
LOGS_DIR = RUNTIME_DIR / "logs"
KILL_SWITCH_PATH = RUNTIME_DIR / "KILL"

# Ensure runtime dirs exist (defensive: some service managers
# don't create parent dirs before opening log files)
for _d in (RUNTIME_DIR, STATE_DIR, REPORTS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
