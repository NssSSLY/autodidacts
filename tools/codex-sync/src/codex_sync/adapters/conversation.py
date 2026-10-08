"""Explicit context supplied by a user or agent plus portable project files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ConversationContext:
    goal: str
    summary: str
    decisions: list[str]
    todos: list[str]
    blockers: list[str]


class ConversationAdapter(Protocol):
    def export_current_context(self) -> ConversationContext: ...


class ProvidedContextAdapter:
    def __init__(
        self,
        sync_directory: Path,
        goal: str = "",
        summary: str = "",
        decisions: list[str] | None = None,
        todos: list[str] | None = None,
        blockers: list[str] | None = None,
    ) -> None:
        self.directory = sync_directory
        self.goal = goal
        self.summary = summary
        self.decisions = decisions or []
        self.todos = todos or []
        self.blockers = blockers or []

    def export_current_context(self) -> ConversationContext:
        def first_body(name: str) -> str:
            path = self.directory / name
            lines = path.read_text(encoding="utf-8").splitlines()
            if name == "CURRENT_STATE.md":
                for line in lines:
                    if line.lstrip(" -").lower().startswith("summary:"):
                        return line.split(":", 1)[1].strip()[:500]
            for line in lines:
                if not line.strip() or line.lstrip().startswith(("#", "<!--")):
                    continue
                cleaned = line.strip(" -#\t")
                if cleaned.lower().startswith(("updated:", "project id:")):
                    continue
                if cleaned:
                    return cleaned[:500]
            return ""

        return ConversationContext(
            goal=self.goal or first_body("PROJECT_CONTEXT.md"),
            summary=self.summary or first_body("CURRENT_STATE.md"),
            decisions=self.decisions or ([first_body("DECISIONS.md")] if first_body("DECISIONS.md") else []),
            todos=self.todos or ([first_body("TODO.md")] if first_body("TODO.md") else []),
            blockers=self.blockers,
        )
