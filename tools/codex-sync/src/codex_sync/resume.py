"""Render bounded, structured context for a fresh Codex conversation."""

from __future__ import annotations

from pathlib import Path

from . import git, project


def _read(path: Path, limit: int = 12000) -> str:
    content = path.read_text(encoding="utf-8").strip()
    if len(content) > limit:
        return content[:limit].rstrip() + f"\n\n[内容过长已截断；其余内容见 {path.name}]"
    return content


def render(root: Path) -> str:
    data = project.load_project(root)
    directory = project.sync_dir(root)
    sections = [
        "# Codex 项目恢复上下文",
        f"项目：{data['project_name']}  \n项目 ID：`{data['project_id']}`  \n"
        f"Git: `{git.branch(root)}` @ `{git.head(root)}`",
    ]
    for name in project.DOCUMENTS:
        sections.append(f"## {name}\n\n{_read(directory / name)}")
    checkpoints = sorted((directory / "checkpoints").glob("*.md"))
    if checkpoints:
        sections.append(f"## 最新检查点（{checkpoints[-1].name}）\n\n{_read(checkpoints[-1], 8000)}")
    recent_history = sorted((directory / "history").glob("*.jsonl"))[-3:]
    if recent_history:
        sections.append("## 近期历史\n\n" + "\n".join(
            f"- {path.name}: {_read(path, 1000)}" for path in recent_history
        ))
    sections.append("## 当前工作区\n\n" + ("\n".join(git.status_lines(root)) or "干净"))
    return "\n\n".join(sections) + "\n"
