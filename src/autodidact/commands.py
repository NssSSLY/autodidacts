# 文件职责：注册目标、记忆、技能、模型比较、索引、导入、网页模型与工作台 CLI。
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
    freeze_suite,
    prepare_suite_file,
    validate_skill,
)
from autodidact.goals.scorer import goal_score
from autodidact.learning.reliable_evaluation import (
    load_frozen,
    recompute_run,
    run_benchmark,
    verify_frozen,
)
from autodidact.memory import MemoryConsolidator
from autodidact.reporting import longitudinal_report
from autodidact.repository import Repository
from autodidact.runtime import controller_lock
from autodidact.schemas import CandidateGoal


# 功能：先升级数据库，再在控制器互斥和会话内执行 CLI 操作，提供仓库与被审计模型。
async def controlled(action):
    await init_database()
    async with controller_lock(engine), SessionLocal() as session:
        return await action(Repository(session), ObservedLLM(build_llm(), engine))


# 功能：把增量操作函数注册到 Typer；注册过程不执行实际学习任务。
def register_commands(app):
    # 功能：仅校验本地题集/真值与摘要，无数据库/模型操作，供冻结前准备。
    @app.command("check-benchmark")
    def check_benchmark(path: str):
        payload = prepare_suite_file(path)
        print(
            {
                "protocol": payload.get("protocol", "legacy_benchmark_v1"),
                "sha256": payload["sha256"],
                "items": len(payload["items"]),
                "note": "格式/快照校验不认证专家身份或语义独立性",
            }
        )

    # 功能：将人工标题和描述转为高优先级目标，去重保存并返回 ID。
    @app.command("add-goal")
    def add_goal(title: str, description: str = ""):
        # 功能：在受控数据库操作中将人工标题和描述转为高优先级目标，去重保存并返回 ID。
        async def action(repo, llm):
            goal = CandidateGoal(
                title=title, description=description, source="human", importance=0.9
            )
            stored = await repo.add_goal(goal, goal_score(goal))
            return {"goal_id": str(stored.id), "title": stored.title}

        print(asyncio.run(controlled(action)))

    # 功能：触发 UTC 当日记忆整合，输出报告 ID 和候选方法统计。
    @app.command("consolidate")
    def consolidate():
        # 功能：在受控数据库操作中触发 UTC 当日记忆整合，输出报告 ID 和候选方法统计。
        async def action(repo, llm):
            return await MemoryConsolidator(repo, llm).consolidate()

        print(asyncio.run(controlled(action)))

    # 功能：刷新模型观察/评估经验 Profile，返回更新数量。
    @app.command("refresh-profiles")
    def refresh_profiles():
        # 功能：在受控数据库操作中刷新模型观察/评估经验 Profile，返回更新数量。
        async def action(repo, llm):
            return {"profiles": await repo.refresh_model_profiles()}

        print(asyncio.run(controlled(action)))

    # 功能：校验文件题集并冻结内容，返回基准 ID、摘要与题数。
    @app.command("freeze-benchmark")
    def freeze_benchmark(path: str = "data/benchmark/sample.json"):
        # 功能：在受控数据库操作中校验文件题集并冻结内容，返回基准 ID、摘要与题数。
        async def action(repo, llm):
            report = await freeze_suite(repo, path)
            return {
                "benchmark_id": str(report.id),
                "sha256": report.payload["sha256"],
                "items": len(report.payload["items"]),
            }

        print(asyncio.run(controlled(action)))

    # 功能：校验冻结版本后评估；可指定基线做同模型/同记忆的真实延迟保持复测。
    @app.command("evaluate-benchmark")
    def evaluate_benchmark(
        benchmark_id: str, use_memory: bool = False, retention_baseline: str = ""
    ):
        # 功能：在控制器锁内执行版本化评估并保存新报告，不改既有分数/信念。
        async def action(repo, llm):
            return await run_benchmark(
                repo,
                llm,
                build_judge(engine),
                benchmark_id,
                use_memory=use_memory,
                baseline_id=retention_baseline or None,
            )

        print(asyncio.run(controlled(action)))

    # 功能：离线于模型重算可靠报告的逐题分数，保存独立审计报告，不覆盖原报告。
    @app.command("regrade-benchmark")
    def regrade_benchmark(report_id: str):
        # 功能：读取原运行/保持报告及冻结真值，校验后仅用固定规则复算，不调用模型。
        async def action(repo, llm):
            original = await repo.s.get(models.ResearchReport, UUID(report_id))
            if original is None or original.kind not in {"benchmark_run", "benchmark_retention"}:
                raise ValueError("需要基准运行或保持报告ID")
            frozen = await load_frozen(repo, original.payload["benchmark_id"])
            suite = verify_frozen(frozen.payload)
            if suite is None:
                raise ValueError("旧模型裁判报告不能冒充确定性真值重算")
            result = recompute_run(original.payload, suite)
            report = await repo.save_report(
                "benchmark_regrade",
                {
                    **result,
                    "original_report_id": report_id,
                    "benchmark_id": str(frozen.id),
                    "original_score": original.payload.get("score"),
                    "note": "重算只新增审计，不覆盖旧报告；不会执行保存的答案/模型建议。",
                },
            )
            return {
                "report_id": str(report.id),
                "score": result["score"],
                "complete": result["complete"],
            }

        print(asyncio.run(controlled(action)))

    # 功能：构造旧/新审计模型，在相同冻结题集和记忆上执行四组比较，不自动切换。
    @app.command("compare-models")
    def compare_models(benchmark_id: str, old_model: str, new_model: str, new_base_url: str = ""):
        # 功能：在受控数据库操作中构造旧/新审计模型，在相同冻结题集和记忆上执行四组比较，不自动切换。
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

    # 功能：在独立冻结题上比较指定研究方法并更新候选/验证状态。
    @app.command("validate-skill")
    def validate_skill_command(skill_id: str, benchmark_id: str):
        # 功能：在受控数据库操作中在独立冻结题上比较指定研究方法并更新候选/验证状态。
        async def action(repo, llm):
            return await validate_skill(repo, llm, build_judge(engine), skill_id, benchmark_id)

        print(asyncio.run(controlled(action)))

    # 功能：列出已保存技能的名称、置信度、状态和验证 metadata。
    @app.command("skills")
    def skills():
        # 功能：在受控数据库操作中列出已保存技能的名称、置信度、状态和验证 metadata。
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

    # 功能：汇总指定天数的成长/资源数据并保存 progress 报告。
    @app.command("report")
    def report(days: int = typer.Option(30, min=1, max=365)):
        # 功能：在受控数据库操作中汇总指定天数的成长/资源数据并保存 progress 报告。
        async def action(repo, llm):
            payload = await longitudinal_report(repo, days)
            saved = await repo.save_report("progress", payload)
            return {"report_id": str(saved.id), **payload}

        print(asyncio.run(controlled(action)))

    # 功能：读取指定报告 ID 的类型与完整载荷，报告不存在时提示错误。
    @app.command("show-report")
    def show_report(report_id: str):
        # 功能：在受控数据库操作中读取指定报告 ID 的类型与完整载荷，报告不存在时提示错误。
        async def action(repo, llm):
            stored = await repo.s.get(models.ResearchReport, UUID(report_id))
            if stored is None:
                raise typer.BadParameter("报告不存在")
            return {"id": str(stored.id), "kind": stored.kind, "payload": stored.payload}

        print(asyncio.run(controlled(action)))

    # 功能：为已有开放争议补排关联调查目标，不直接裁定争议。
    @app.command("queue-disputes")
    def queue_disputes():
        # 功能：在受控数据库操作中为已有开放争议补排关联调查目标，不直接裁定争议。
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

    # 功能：分批重建非撤回信念的缺失/旧指纹向量，--force 可覆盖当前向量。
    @app.command("rebuild-embeddings")
    def rebuild_embeddings(limit: int = typer.Option(100, min=1, max=10000), force: bool = False):
        # 功能：在受控数据库操作中分批重建非撤回信念的缺失/旧指纹向量，--force 可覆盖当前向量。
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

    # 功能：在指定本地端口启动研究工作台，不开启公网监听。
    @app.command("serve")
    def serve_command(port: int = typer.Option(8765, min=1024, max=65535)):
        from autodidact.workbench import serve

        asyncio.run(serve(port))

    # 功能：读取本地材料，保存等级 1 来源并创建关联学习目标，不直接生成 verified 信念。
    @app.command("import-document")
    def import_document(path: str):
        # 功能：在受控数据库操作中读取本地材料，保存等级 1 来源并创建关联学习目标，不直接生成 verified 信念。
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

    # 功能：向指定合法配置网页模型提问，保存观察并明确输出等级 0。
    @app.command("web-ask")
    def web_ask(provider: str, prompt: str):
        # 功能：在受控数据库操作中向指定合法配置网页模型提问，保存观察并明确输出等级 0。
        async def action(repo, llm):
            from autodidact.web_models.service import WebModelService

            answer = await WebModelService(engine).ask(provider, prompt)
            return {"evidence_level": 0, **answer.model_dump()}

        print(asyncio.run(controlled(action)))
