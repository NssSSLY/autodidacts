"""Portable project metadata and non-destructive V1 initialization."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import git

SCHEMA_VERSION = 2
DOCUMENTS = ("PROJECT_CONTEXT.md", "DECISIONS.md", "CURRENT_STATE.md", "TODO.md")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sync_dir(root: Path) -> Path:
    return root / ".codex-sync"


def _template(name: str) -> str:
    module = Path(__file__).resolve()
    for base in (module.parents[1], module.parents[2]):
        path = base / "templates" / name
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise RuntimeError(f"Template is missing: {name}")


def save_project(root: Path, data: dict) -> None:
    path = sync_dir(root) / "project.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_project(root: Path) -> dict:
    path = sync_dir(root) / "project.json"
    if not path.is_file():
        raise git.SyncError("Project is not initialized. Run codex-sync init.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise git.SyncError("Unsupported project schema; review MIGRATION_V1_V2.md.")
    return data


def initialize(root: Path) -> dict:
    directory = sync_dir(root)
    directory.mkdir(exist_ok=True)
    (directory / "checkpoints").mkdir(exist_ok=True)
    (directory / "history").mkdir(exist_ok=True)
    legacy = {"DECISIONS.md": "decisions.md", "CURRENT_STATE.md": "current-state.md"}
    legacy_directory = directory / "legacy-v1"
    for old_name in legacy.values():
        old = next((path for path in directory.iterdir() if path.name == old_name and path.is_file()), None)
        if old is None:
            continue
        legacy_directory.mkdir(exist_ok=True)
        archived = legacy_directory / old_name
        if archived.exists():
            raise git.SyncError(f"Both V1 files exist: {old} and {archived}. Reconcile them manually.")
        old.replace(archived)
    for name in DOCUMENTS:
        target = directory / name
        if any(path.name == name and path.is_file() for path in directory.iterdir()):
            continue
        old = legacy_directory / legacy[name] if name in legacy else None
        content = old.read_text(encoding="utf-8") if old and old.is_file() else _template(name)
        target.write_text(content, encoding="utf-8")
    metadata = directory / "project.json"
    if metadata.exists():
        return load_project(root)
    now = utc_now()
    data = {
        "schema_version": SCHEMA_VERSION,
        "project_id": str(uuid.uuid4()),
        "project_name": root.name,
        "repository": git.repository(root),
        "repository_root": ".",
        "default_branch": git.default_branch(root),
        "created_at": now,
        "updated_at": now,
    }
    save_project(root, data)
    return data
