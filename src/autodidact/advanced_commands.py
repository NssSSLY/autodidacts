"""来源血缘、持久检查点与混合检索的人工可见入口。"""

from __future__ import annotations

import asyncio
from uuid import UUID

import typer
from rich import print
from sqlalchemy import func, select

from autodidact import models
from autodidact.commands import controlled
from autodidact.db import engine
from autodidact.embedding_runtime import RecordedEmbedding
from autodidact.embeddings import build_embedding_provider
from autodidact.knowledge.sources import normalize_url
from autodidact.resume import PROTOCOL
from autodidact.retrieval import HybridRetriever
from autodidact.schemas import SourceDocument


def register_advanced_commands(app):
    @app.command("rebuild-retrieval")
    def rebuild_retrieval(limit: int = typer.Option(200, min=1, max=10000), vectors: bool = False):
        async def action(repo, llm):
            embedding = RecordedEmbedding(build_embedding_provider(), engine) if vectors else None
            return await repo.index.rebuild(embedding, limit=limit)

        print(asyncio.run(controlled(action)))

    @app.command("search-memory")
    def search_memory(
        query: str,
        kind: str = "all",
        limit: int = typer.Option(20, min=1, max=100),
        accepted_only: bool = False,
    ):
        if kind not in {"all", "belief", "claim", "goal"}:
            raise typer.BadParameter("kind应为all / belief / claim / goal")

        async def action(repo, llm):
            hits = await HybridRetriever(
                repo, RecordedEmbedding(build_embedding_provider(), engine)
            ).retrieve(
                query,
                kinds=(kind,) if kind != "all" else ("belief", "claim", "goal"),
                limit=limit,
                accepted_only=accepted_only,
            )
            return {
                "hits": [h.as_dict() for h in hits],
                "note": "排序分数不是可信度；Claim是候选，Goal是意图",
            }

        print(asyncio.run(controlled(action)))

    @app.command("source-lineage")
    def source_lineage(url: str):
        async def action(repo, llm):
            key = normalize_url(url)
            components = await repo.lineage.dependency_keys([key])
            nodes = sorted(components.get(key, ()))
            links = (
                await repo.s.scalars(
                    select(models.SourceLink)
                    .where(models.SourceLink.from_url.in_(nodes))
                    .order_by(models.SourceLink.id)
                    .limit(500)
                )
            ).all()
            return {
                "url": key,
                "dependent_nodes": nodes,
                "links": [
                    {
                        "from": e.from_url,
                        "to": e.to_url,
                        "relation": e.relation,
                        "details": e.details,
                    }
                    for e in links
                ],
                "note": "网页自报血缘仅用于保守去重；普通cites边不合并独立性，图边显示最多500条",
            }

        print(asyncio.run(controlled(action)))

    @app.command("rebuild-lineage")
    def rebuild_lineage(limit: int = typer.Option(200, min=1, max=10000)):
        async def action(repo, llm):
            # Metadata marker also handles sources with zero outgoing edges; batches advance.
            rows = (
                await repo.s.scalars(
                    select(models.Source)
                    .where(
                        models.Source.metadata_json[
                            "lineage_graph_version"
                        ].astext.is_distinct_from("1")
                    )
                    .order_by(models.Source.id)
                    .limit(limit)
                )
            ).all()
            for source in rows:
                metadata = source.metadata_json or {}
                doc = SourceDocument(
                    url=source.url,
                    text=source.extracted_text or "",
                    lineage_key=metadata.get("lineage_key", ""),
                    metadata=metadata,
                )
                await repo.lineage.record(source, doc)
                source.metadata_json = {**metadata, "lineage_graph_version": "1"}
                await repo.s.commit()
            return {
                "rebuilt": len(rows),
                "remaining": await repo.s.scalar(
                    select(func.count())
                    .select_from(models.Source)
                    .where(
                        models.Source.metadata_json[
                            "lineage_graph_version"
                        ].astext.is_distinct_from("1")
                    )
                ),
                "note": "旧数据只回填已有metadata，不重新下载网页、不猜测缺失血缘",
            }

        print(asyncio.run(controlled(action)))

    @app.command("checkpoints")
    def checkpoints(limit: int = typer.Option(20, min=1, max=100)):
        async def action(repo, llm):
            rows = (
                await repo.s.scalars(
                    select(models.LearningSession)
                    .where(models.LearningSession.completed_at.is_(None))
                    .order_by(models.LearningSession.created_at)
                    .limit(limit)
                )
            ).all()
            result = []
            for attempt in rows:
                count = await repo.s.scalar(
                    select(func.count())
                    .select_from(models.LearningStep)
                    .where(models.LearningStep.learning_session_id == attempt.id)
                )
                result.append(
                    {
                        "session_id": str(attempt.id),
                        "goal_id": str(attempt.goal_id),
                        "protocol": attempt.plan.get("resume_protocol"),
                        "stage": (attempt.result or {}).get("checkpoint", "planning"),
                        "saved_work_items": count,
                        "error": (attempt.result or {}).get("resume_error"),
                        "failures": (attempt.result or {}).get("resume_failures", 0),
                    }
                )
            return result

        print(asyncio.run(controlled(action)))

    @app.command("restart-session")
    def restart_session(session_id: str):
        async def action(repo, llm):
            attempt = await repo.s.get(models.LearningSession, UUID(session_id))
            if not attempt or attempt.completed_at:
                raise typer.BadParameter("会话不存在或已经完成")
            goal = await repo.s.get(models.Goal, attempt.goal_id)
            goal.status, goal.completed_at, goal.retry_count = "discovered", None, 0
            metadata = dict(goal.metadata_json or {})
            metadata.pop("blocked_resume_session_id", None)
            goal.metadata_json = metadata
            await repo.finish_learning_session(
                attempt,
                {"recovery": "human_requested_restart"},
                {"reason": "人工结束旧尝试并重新排队；保留全部证据与工作项"},
                False,
            )
            return {
                "archived_attempt_id": session_id,
                "goal_id": str(goal.id),
                "next": "run-once将使用当前配置创建新会话；旧记录与预算账本不会删除",
            }

        print(asyncio.run(controlled(action)))

    @app.command("retry-session")
    def retry_session(session_id: str):
        async def action(repo, llm):
            attempt = await repo.s.get(models.LearningSession, UUID(session_id))
            if (
                not attempt
                or attempt.completed_at
                or attempt.plan.get("resume_protocol") != PROTOCOL
            ):
                raise typer.BadParameter("会话不存在、已完成或不支持工作项续跑")
            goal = await repo.s.get(models.Goal, attempt.goal_id)
            attempt.result = {**(attempt.result or {}), "resume_failures": 0, "resume_error": None}
            if goal.status == "blocked":
                goal.status = "planned"
                metadata = dict(goal.metadata_json or {})
                metadata.pop("blocked_resume_session_id", None)
                goal.metadata_json, goal.completed_at = metadata, None
            await repo.s.commit()
            return {
                "session_id": session_id,
                "status": "ready_to_resume",
                "next": "运行autodidact run-once；仍需保持原模型与学习策略",
            }

        print(asyncio.run(controlled(action)))
