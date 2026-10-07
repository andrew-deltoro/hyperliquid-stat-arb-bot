"""Build a timestamped, shareable snapshot of hyperliquid-stat-arb-bot.

Outputs (in this script's directory):
  hlbot_snapshot_<UTC>_<sha>.zip       full tracked source + selected untracked scripts
  hlbot_core_bundle_<UTC>_<sha>.md     single-file bundle of the live code path, for pasting into prompts

Never includes: .env, .git, .venv, state/, logs/, reports/ data, *.bak*.
Strips the install-on-open dropper files found in the initial template commit.
"""
from __future__ import annotations

import io
import re
import subprocess
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/home/hlbot/hyperliquid-stat-arb-bot")
OUT = Path(__file__).resolve().parent

UNTRACKED_INCLUDE = [
    "scripts/leverage_study.py",
    "scripts/directional_perp_v2.py",
    "scripts/directional_perp_v3.py",
    "scripts/directional_perp_v4.py",
]
DROPPER = {".vscode/tasks.json", ".vscode/settings.json", ".npmrc", "package.json"}
EXCLUDE_RE = re.compile(r"(\.bak|^state/|^logs/|^reports/.*\.(csv|json|log|png)$|^\.env$)")

CORE_GLOBS = [
    "pyproject.toml",
    "requirements.txt",
    "config/phase3-live.yaml",
    "config/directional-live.yaml",
    "src/hyperliquid_bot/phase3/*.py",
    "src/hyperliquid_bot/stats/cointegration.py",
    "src/hyperliquid_bot/stats/mean_reversion.py",
    "src/hyperliquid_bot/data/ohlcv.py",
    "src/hyperliquid_bot/pairs/kalman.py",
    "src/hyperliquid_bot/directional/*.py",
    "src/hyperliquid_bot/funding/executor.py",
    "scripts/run_cycle.py",
    "scripts/reconcile_orphans.py",
    "scripts/walk_forward_ensemble.py",
    "scripts/leverage_study.py",
]

SECRET_PATTERNS = {
    "64-hex (private-key shaped)": re.compile(r"\b(0x)?[0-9a-fA-F]{64}\b"),
    "PRIVATE_KEY with value": re.compile(r"PRIVATE_KEY[A-Z_]*\s*=\s*['\"]?[0-9a-fA-Fx]{20,}"),
}


def git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(REPO), *args], text=True).strip()


def sanitize(path: str, data: bytes) -> bytes:
    if path == ".env.example":
        lines = data.decode("utf-8", "replace").replace("\r\n", "\n").splitlines()
        lines = [l for l in lines if not l.startswith("REGISTRY_STR=")]
        return ("\n".join(lines) + "\n").encode()
    return data


def collect_files() -> dict[str, bytes]:
    archive = subprocess.check_output(["git", "-C", str(REPO), "archive", "--format=tar", "HEAD"])
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
        for m in tf.getmembers():
            if not m.isfile() or m.name in DROPPER or EXCLUDE_RE.search(m.name):
                continue
            files[m.name] = sanitize(m.name, tf.extractfile(m).read())
    for rel in UNTRACKED_INCLUDE:
        p = REPO / rel
        if p.exists():
            files[rel] = p.read_bytes()
    return files


def scan(files: dict[str, bytes]) -> list[str]:
    hits = []
    for name, data in files.items():
        text = data.decode("utf-8", "ignore")
        for label, rx in SECRET_PATTERNS.items():
            if rx.search(text):
                hits.append(f"{name}: {label}")
    return hits


def header(stamp: str, sha: str, subject: str, commit_date: str, kind: str) -> str:
    dirty = git("status", "--porcelain", "--untracked-files=no")
    return f"""# hyperliquid-stat-arb-bot — {kind}

| | |
|---|---|
| **Snapshot taken (UTC)** | {stamp} |
| **Commit** | `{sha}` — "{subject}" ({commit_date}) |
| **Tracked changes vs commit** | {"none" if not dirty else "YES - working tree differs"} |
| **Extra untracked files included** | {", ".join(UNTRACKED_INCLUDE)} |
| **Excluded** | `.env` (secrets), `.git`, `.venv`, `state/`, `logs/`, report data, `*.bak*` backups |
| **Removed for safety** | `.vscode/tasks.json`, `.vscode/settings.json`, `.npmrc`, `package.json`, `REGISTRY_STR` line in `.env.example` (install-on-open dropper from template commit `3295ae8`) |
| **Companion audit** | `AUDIT_REPORT_2026-10-07.md` |

> This is a point-in-time copy. The live code may have changed since the timestamp above;
> verify against the current repository before acting on anything here.
"""


def main() -> None:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    tag = now.strftime("%Y%m%dT%H%MZ")
    sha = git("rev-parse", "HEAD")
    short = sha[:7]
    subject = git("log", "-1", "--format=%s")
    commit_date = git("log", "-1", "--format=%ad", "--date=iso")

    files = collect_files()
    hits = scan(files)
    if hits:
        raise SystemExit("Refusing to write snapshot; possible secrets:\n  " + "\n  ".join(hits))

    info = header(stamp, sha, subject, commit_date, "Full Source Snapshot")
    info += "\n## Files\n\n" + "\n".join(f"- `{n}`" for n in sorted(files)) + "\n"

    zip_path = OUT / f"hlbot_snapshot_{tag}_{short}.zip"
    root = f"hyperliquid-stat-arb-bot_{tag}_{short}"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{root}/SNAPSHOT_INFO.md", info)
        for name in sorted(files):
            zf.writestr(f"{root}/{name}", files[name])

    core = []
    for g in CORE_GLOBS:
        matches = sorted(n for n in files if Path(n).match(g))
        core.extend(m for m in matches if m not in core)
    lang = {".py": "python", ".yaml": "yaml", ".toml": "toml", ".txt": "text"}
    parts = [header(stamp, sha, subject, commit_date, "Core Live-Path Bundle")]
    parts.append("\n## Contents\n\n" + "\n".join(f"- `{n}`" for n in core) + "\n")
    parts.append("\n## Full repository tree (for reference)\n\n```\n" + "\n".join(sorted(files)) + "\n```\n")
    for n in core:
        body = files[n].decode("utf-8", "replace")
        parts.append(f"\n---\n\n## `{n}`\n\n```{lang.get(Path(n).suffix, '')}\n{body.rstrip()}\n```\n")
    md_path = OUT / f"hlbot_core_bundle_{tag}_{short}.md"
    md_path.write_text("".join(parts))

    print(f"zip:    {zip_path} ({zip_path.stat().st_size/1024:.0f} KB, {len(files)} files)")
    print(f"bundle: {md_path} ({md_path.stat().st_size/1024:.0f} KB, {len(core)} files)")


if __name__ == "__main__":
    main()
