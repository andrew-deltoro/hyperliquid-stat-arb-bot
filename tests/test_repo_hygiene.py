"""Supply-chain hygiene: no install-on-open tasks or registry redirection."""
from __future__ import annotations

import base64
import binascii
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
B64 = re.compile(rb"[A-Za-z0-9+/]{24,}={0,2}")


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    return [ROOT / p for p in out.stdout.splitlines() if (ROOT / p).is_file()]


def test_no_folder_open_tasks():
    tasks = ROOT / ".vscode" / "tasks.json"
    assert not tasks.exists() or "folderOpen" not in tasks.read_text()


def test_no_custom_npm_registry():
    npmrc = ROOT / ".npmrc"
    assert not npmrc.exists() or "registry" not in npmrc.read_text()


def test_no_base64_encoded_urls():
    hits = []
    for path in tracked_files():
        if path.suffix in {".png", ".jpg", ".pdf", ".ipynb"}:
            continue
        for m in B64.finditer(path.read_bytes()):
            try:
                decoded = base64.b64decode(m.group(0), validate=True)
            except (binascii.Error, ValueError):
                continue
            if re.search(rb"https?://", decoded):
                hits.append(path.relative_to(ROOT))
    assert hits == []
