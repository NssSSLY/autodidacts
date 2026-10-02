"""Incremental CLI entry points for persistent learning and experiments."""

from __future__ import annotations

import asyncio
from uuid import UUID

import typer
from rich import print
from sqlalchemy import select

from autodidact import models
from autodidact.brain.factory import build_judge
from autodidact.brain.llm import OpenAICompatibleLLM, build_llm
from autodidact.brain.observed import ObservedLLM
from autodidact.config import runtime_settings
from autodidact.db import SessionLocal, engine, init_database
from autodidact.experiments import (
    ModelMigrationProtocol,
    evaluate_suite,
    freeze_suite,
    validate_skill,
)
from autodidact.goals.scorer import goal_score
from autodidact.memory import MemoryConsolidator
from autodidact.reporting import longitudinal_report
from autodidact.repository import Repository
from autodidact.runtime import controller_lock
from autodidact.schemas import CandidateGoal


async def controlled(action):
    await init_database()
    async with controller_lock(engine), SessionLocal() as session:
        return await action(Repository(session), ObservedLLM(build_llm(), engine))


def register_commands(app):
    @app.command("add-goal")
    def add_goal(title: str, description: str = ""):
        async def action(repo, llm):
            goal = CandidateGoal(
                title=title, description=description, source="human", importance=0.9
            )
            stored = await repo.add_goal(goal, goal_score(goal))
            return {"goal_id": str(stored.id), "title": stored.title}

        print(asyncio.run(controlled(action)))

    @app.command("consolidate")
    def consolidate():
        async def action(repo, llm):
            return await MemoryConsolidator(repo, llm).consolidate()

        print(asyncio.run(controlled(action)))

    @app.command("refresh-profiles")
    def refresh_profiles():
        async def action(repo, llm):
            return {"profiles": await repo.refresh_model_profiles()}

        print(asyncio.run(controlled(action)))

    @app.command("freeze-benchmark")
    def freeze_benchmark(path: str = "data/benchmark/sample.json"):
        async def action(repo, llm):
            report = await freeze_suite(repo, path)
            return {
                "benchmark_id": str(report.id),
                "sha256": report.payload["sha256"],
                "items": len(report.payload["items"]),
            }

        print(asyncio.run(controlled(action)))

    @app.command("evaluate-benchmark")
    def evaluate_benchmark(benchmark_id: str, use_memory: bool = False):
        async def action(repo, llm):
            suite = await repo.s.get(models.ResearchReport, UUID(benchmark_id))
            if suite is None or suite.kind != "frozen_benchmark":
                raise typer.BadParameter("冻结基准不存在")
            memory = await repo.accepted_memory() if use_memory else []
            result = await evaluate_suite(llm, build_judge(engine), suite.payload["items"], memory)
            report = await repo.save_report(
                "benchmark_run", {**result, "benchmark_id": benchmark_id, "memory_snapshot": memory}
            )
            return {"report_id": str(report.id), "score": result["score"], "n": result["n"]}

        print(asyncio.run(controlled(action)))

    @app.command("compare-models")
    def compare_models(benchmark_id: str, old_model: str, new_model: str, new_base_url: str = ""):
        async def action(repo, llm):
            settings = runtime_settings()
            old = ObservedLLM(
                OpenAICompatibleLLM(settings.model_copy(update={"llm_model": old_model})),
                engine,
                role="migration_old",
            )
            new = ObservedLLM(
                OpenAICompatibleLLM(
                    settings.model_copy(
                        update={
                            "llm_model": new_model,
                            "llm_base_url": new_base_url or settings.llm_base_url,
                            "llm_api_key": settings.migration_new_api_key or settings.llm_api_key,
                        }
                    )
                ),
                engine,
                role="migration_new",
            )
            return await ModelMigrationProtocol(repo, build_judge(engine)).compare(
                old, new, benchmark_id
            )

        print(asyncio.run(controlled(action)))

    @app.command("validate-skill")
    def validate_skill_command(skill_id: str, benchmark_id: str):
        async def action(repo, llm):
            return await validate_skill(repo, llm, build_judge(engine), skill_id, benchmark_id)

        print(asyncio.run(controlled(action)))

    @app.command("skills")
    def skills():
        async def action(repo, llm):
            rows = (
                await repo.s.scalars(select(models.Skill).order_by(models.Skill.created_at.desc()))
            ).all()
            return [
                {
                    "id": str(s.id),
                    "name": s.name,
                    "confidence": s.confidence,
                    "metadata": s.metadata_json,
                }
                for s in rows
            ]

        print(asyncio.run(controlled(action)))

    @app.command("report")
    def report(days: int = typer.Option(30, min=1, max=365)):
        async def action(repo, llm):
            payload = await longitudinal_report(repo, days)
            saved = await repo.save_report("progress", payload)
            return {"report_id": str(saved.id), **payload}

        print(asyncio.run(controlled(action)))

    @app.command("show-report")
    def show_report(report_id: str):
        async def action(repo, llm):
            stored = await repo.s.get(models.ResearchReport, UUID(report_id))
            if stored is None:
                raise typer.BadParameter("报告不存在")
            return {"id": str(stored.id), "kind": stored.kind, "payload": stored.payload}

        print(asyncio.run(controlled(action)))

    @app.command("queue-disputes")
    def queue_disputes():
        async def action(repo, llm):
            ids = []
            for dispute in await repo.open_disputes():
                goal = CandidateGoal(
                    title=f"独立调查争议 {dispute.id}",
                    source="conflict",
                    importance=0.95,
                    uncertainty=1.0,
                )
                saved = await repo.add_goal(goal, goal_score(goal))
                saved.metadata_json = {**(saved.metadata_json or {}), "dispute_id": str(dispute.id)}
                ids.append(str(saved.id))
            await repo.s.commit()
            return {"investigation_goals": ids}

        print(asyncio.run(controlled(action)))

    @app.command("rebuild-embeddings")
    def rebuild_embeddings(limit: int = typer.Option(100, min=1, max=10000), force: bool = False):
        async def action(repo, llm):
            from autodidact.embedding_runtime import RecordedEmbedding
            from autodidact.embeddings import build_embedding_provider

            embedding = RecordedEmbedding(build_embedding_provider(), engine)
            query = (
                select(models.Belief)
                .where(models.Belief.status != "retracted")
                .order_by(models.Belief.created_at)
            )
            if not force:
                query = query.where(
                    models.Belief.embedding.is_(None)
                    | (models.Belief.metadata_json["embedding_fingerprint"].astext.is_(None))
                    | (
                        models.Belief.metadata_json["embedding_fingerprint"].astext
                        != embedding.fingerprint
                    )
                )
            rows = (await repo.s.scalars(query.limit(limit))).all()
            for belief in rows:
                vector = await embedding.embed(belief.statement)
                await repo.set_belief_embedding(belief, vector, fingerprint=embedding.fingerprint)
            return {"rebuilt": len(rows), "fingerprint": embedding.fingerprint}

        print(asyncio.run(controlled(action)))

    @app.command("serve")
    def serve_command(port: int = typer.Option(8765, min=1024, max=65535)):
        from autodidact.workbench import serve

        asyncio.run(serve(port))

    @app.command("import-document")
    def import_document(path: str):
        async def action(repo, llm):
            from autodidact.tools.documents import read_local_document
            from autodidact.tools.reader import WebReader

            document = read_local_document(path)
            source = await repo.upsert_source(document, WebReader.hash_text(document.text))
            draft = CandidateGoal(
                title=f"学习本地文档：{document.title}", source="human", importance=0.9
            )
            goal = await repo.add_goal(draft, goal_score(draft))
            goal.metadata_json = {
                **(goal.metadata_json or {}),
                "document_source_id": str(source.id),
            }
            await repo.s.commit()
            return {"source_id": str(source.id), "goal_id": str(goal.id), "evidence_level": 1}

        print(asyncio.run(controlled(action)))

    @app.command("web-ask")
    def web_ask(provider: str, prompt: str):
        async def action(repo, llm):
            from autodidact.web_models.service import WebModelService

            answer = await WebModelService(engine).ask(provider, prompt)
            return {"evidence_level": 0, **answer.model_dump()}

        print(asyncio.run(controlled(action)))
