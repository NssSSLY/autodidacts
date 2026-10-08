"""Checkpoint project context without reading Codex sessions or raw patches."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import git, project
from .adapters.conversation import ProvidedContextAdapter


def _safe_text(value: str) -> str:
    if re.search(r"(?i)(password|secret|token|api[_-]?key|private[_-]?key)\s*[:=]\s*\S+", value):
        raise git.SyncError("Checkpoint input appears to contain a credential.")
    if "-----BEGIN " in value and "PRIVATE KEY-----" in value:
        raise git.SyncError("Checkpoint input appears to contain a private key.")
    return value.strip()


def _append_unique(path: Path, values: list[str]) -> None:
    if not values:
        return
    existing = path.read_text(encoding="utf-8")
    additions = [f"- {value}" for value in values if f"- {value}" not in existing]
    if additions:
        path.write_text(existing.rstrip() + "\n\n" + "\n".join(additions) + "\n", encoding="utf-8")


def _list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- 无"


def create(
    root: Path,
    *,
    goal: str = "",
    summary: str = "",
    decisions: list[str] | None = None,
    todos: list[str] | None = None,
    blockers: list[str] | None = None,
) -> Path:
    metadata = project.load_project(root)
    values = [goal, summary, *(decisions or []), *(todos or []), *(blockers or [])]
    for value in values:
        _safe_text(value)
    directory = project.sync_dir(root)
    adapter = ProvidedContextAdapter(directory, goal, summary, decisions, todos, blockers)
    context = adapter.export_current_context()
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    snapshot = {
        "schema_version": 2,
        "checkpoint_id": stamp,
        "project_id": metadata["project_id"],
        "created_at": now.isoformat(timespec="seconds"),
        "goal": context.goal,
        "summary": context.summary,
        "decisions": context.decisions,
        "modified_files": git.changed_files(root),
        "git_branch": git.branch(root),
        "git_head": git.head(root),
        "git_status": git.status_lines(root),
        "git_diff_summary": git.diff_summary(root),
        "recent_commits": git.recent_commits(root),
        "todos": context.todos,
        "blockers": context.blockers,
    }
    markdown = (
        f"# 检查点 {stamp}\n\n"
        f"项目 ID：`{snapshot['project_id']}`\n\n"
        f"创建时间：{snapshot['created_at']}\n\n"
        f"Git: `{snapshot['git_branch']}` @ `{snapshot['git_head']}`\n\n"
        f"## 当前目标\n\n{context.goal or '未提供'}\n\n"
        f"## 阶段摘要\n\n{context.summary or '未提供'}\n\n"
        f"## 关键决策\n\n{_list(context.decisions)}\n\n"
        f"## 修改文件\n\n{_list(snapshot['modified_files'])}\n\n"
        f"## Git 工作区状态\n\n{_list(snapshot['git_status'])}\n\n"
        f"## Git 差异摘要\n\n{_list(snapshot['git_diff_summary'])}\n\n"
        f"## 最近提交\n\n{_list(snapshot['recent_commits'])}\n\n"
        f"## 待办事项\n\n{_list(context.todos)}\n\n"
        f"## 阻塞\n\n{_list(context.blockers)}\n"
    )
    checkpoint_dir = directory / "checkpoints"
    checkpoint_dir.mkdir(exist_ok=True)
    history_dir = directory / "history"
    history_dir.mkdir(exist_ok=True)
    md_path = checkpoint_dir / f"{stamp}.md"
    md_path.write_text(markdown, encoding="utf-8")
    (checkpoint_dir / f"{stamp}.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (history_dir / f"{stamp}.jsonl").write_text(
        json.dumps(snapshot, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _append_unique(directory / "DECISIONS.md", context.decisions if decisions else [])
    _append_unique(directory / "TODO.md", context.todos if todos else [])
    metadata["updated_at"] = project.utc_now()
    project.save_project(root, metadata)
    return md_path
