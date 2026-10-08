from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
SOURCE = TOOL_ROOT / "src"
sys.path.insert(0, str(SOURCE))


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def make_repo(base: Path, name: str = "project") -> Path:
    root = base / name
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Codex Sync Test")
    git(root, "config", "user.email", "codex-sync-test@example.invalid")
    (root / "README.md").write_text("test project\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "--no-gpg-sign", "-m", "initial")
    return root


def cli(path: Path, *args: str, success: bool = True, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, "-m", "codex_sync", *args], cwd=path,
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if success and result.returncode:
        raise AssertionError(result.stderr)
    return result
