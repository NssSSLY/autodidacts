"""Small, explicit Git operations used by the continuity commands."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class SyncError(RuntimeError):
    pass


def run(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and result.returncode:
        raise SyncError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.rstrip("\r\n")


def repo_root(start: Path) -> Path:
    root = run(start, "rev-parse", "--show-toplevel")
    return Path(root).resolve()


def branch(root: Path) -> str:
    return run(root, "branch", "--show-current") or "(detached)"


def head(root: Path) -> str:
    return run(root, "rev-parse", "HEAD")


def repository(root: Path) -> str:
    raw = run(root, "remote", "get-url", "origin", check=False)
    return re.sub(r"(://)[^/@]+@", r"\1", raw)


def default_branch(root: Path) -> str:
    remote_head = run(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
    return remote_head.rsplit("/", 1)[-1] if remote_head else branch(root)


def _safe_path(path: str) -> bool:
    lowered = path.lower().replace("\\", "/")
    pieces = lowered.split("/")
    return not (
        any(piece in {".env", "auth.json", ".sandbox-secrets"} for piece in pieces)
        or lowered.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def status_lines(root: Path) -> list[str]:
    raw = run(root, "status", "--porcelain=v1", "--untracked-files=all")
    return [line for line in raw.splitlines() if _safe_path(line[3:])]


def changed_files(root: Path) -> list[str]:
    tracked = run(root, "diff", "--name-only", "HEAD").splitlines()
    untracked = run(root, "ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({path for path in tracked + untracked if path and _safe_path(path)})


def diff_summary(root: Path) -> list[str]:
    lines = []
    for line in run(root, "diff", "--numstat", "HEAD").splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and _safe_path(parts[2]):
            lines.append(f"{parts[2]}: +{parts[0]}/-{parts[1]}")
    return lines


def recent_commits(root: Path, count: int = 5) -> list[str]:
    return run(root, "log", f"-{count}", "--format=%h %s").splitlines()


def sync(root: Path, pull: bool = False) -> str:
    upstream = run(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    if not upstream or "/" not in upstream:
        raise SyncError("No upstream branch. Configure one before syncing.")
    if pull and run(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise SyncError("Working tree is not clean; preserve local changes before --pull.")
    remote = upstream.split("/", 1)[0]
    run(root, "fetch", remote)
    counts = run(root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}").split()
    ahead, behind = map(int, counts)
    if ahead and behind:
        raise SyncError(f"Branches diverged: ahead {ahead}, behind {behind}. Resolve manually.")
    if pull and behind:
        run(root, "pull", "--ff-only")
        return f"Fast-forwarded {behind} commit(s) from {upstream}."
    return f"{upstream}: ahead {ahead}, behind {behind}. " + (
        "Use --pull for a clean fast-forward." if behind else "No pull needed."
    )
