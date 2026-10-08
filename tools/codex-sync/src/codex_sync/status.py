"""Read-only project and Git status."""

from __future__ import annotations

import os
from pathlib import Path

from . import git, project


def render(root: Path) -> str:
    data = project.load_project(root)
    checkpoints = sorted((project.sync_dir(root) / "checkpoints").glob("*.md"))
    lines = [
        f"项目：{data['project_name']} ({data['project_id']})",
        f"仓库：{root}",
        f"CODEX_HOME（只查看，不同步）：{os.environ.get('CODEX_HOME') or Path.home() / '.codex'}",
        f"分支：{git.branch(root)}",
        f"HEAD: {git.head(root)}",
        f"未提交改动：{'有' if git.run(root, 'status', '--porcelain=v1', '--untracked-files=all') else '无'}",
        f"最新检查点：{checkpoints[-1].name if checkpoints else '无'}",
    ]
    return "\n".join(lines)
