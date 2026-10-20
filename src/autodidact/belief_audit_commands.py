# 文件职责：提供盲审冻结/独立作答/后置核源/只读状态CLI，候选模型不替换日常模型配置。
import asyncio
from uuid import UUID

import typer
from rich import print

from autodidact.agent import AutonomousLearner
from autodidact.belief_audit import BeliefMigrationAudit, freeze_audit, read_manifest
from autodidact.brain.llm import OpenAICompatibleLLM, build_llm
from autodidact.config import runtime_settings
from autodidact.db import SessionLocal, engine
from autodidact.learning.ground_truth import model_identity
from autodidact.repository import Repository
from autodidact.runtime import controller_lock


# 功能：显式构造候选API工具，不发请求；换端点运行必须配置专用迁移密钥，防默认密钥跨服务发送。
def candidate(model, base_url, *, running=False):
    settings = runtime_settings()
    if not model.strip():
        raise ValueError("候选模型名称不能为空")
    if (
        running
        and base_url
        and base_url.rstrip("/") != settings.llm_base_url.rstrip("/")
        and not settings.migration_new_api_key
    ):
        raise ValueError("更换服务端点请配置MIGRATION_NEW_API_KEY，不向新服务默认发送日常密钥")
    return OpenAICompatibleLLM(
        settings.model_copy(
            update={
                "llm_model": model,
                "llm_base_url": base_url or settings.llm_base_url,
                "llm_api_key": settings.migration_new_api_key or settings.llm_api_key,
            }
        )
    )


# 功能：在全库控制器锁下执行有限审计阶段，不自动迁移库、切换模型或启动后台学习。
async def run_audit(report_id, model, base_url, phase, limit):
    raw = candidate(model, base_url, running=True)
    async with controller_lock(engine), SessionLocal() as session:
        learner = AutonomousLearner(session, raw)
        audit = BeliefMigrationAudit(learner.repo, learner.llm, learner.steps, learner.collector)
        return await audit.run(report_id, phase, limit)


# 功能：注册四个盲审命令，查看/冻结不执行模型，收费阶段必须显式调用。
def register_belief_audit_commands(app):
    # 功能：从人工中性问题文件冻结高置信信念与候选身份，只写报告/不研究。
    @app.command("freeze-belief-audit")
    def freeze(path: str, new_model: str, new_base_url: str = ""):
        manifest, raw = read_manifest(path), candidate(new_model, new_base_url)

        # 功能：控制器互斥内读取当前认知并保存不可变快照，要求已初始化库。
        async def action():
            async with controller_lock(engine), SessionLocal() as session:
                return await freeze_audit(
                    Repository(session), manifest, model_identity(build_llm()), model_identity(raw)
                )

        print(asyncio.run(action()))

    # 功能：仅按人工问题独立盲答，输出持久等级0观察；不读取原文或进入争议。
    @app.command("answer-belief-audit")
    def answer(
        report_id: UUID,
        new_model: str,
        new_base_url: str = "",
        limit: int = typer.Option(1, min=1, max=20),
    ):
        print(asyncio.run(run_audit(report_id, new_model, new_base_url, "answer", limit)))

    # 功能：只核验已有盲答，重读最多3个旧支持URL并独立找新原文；不自动决议争议。
    @app.command("verify-belief-audit")
    def verify(
        report_id: UUID,
        new_model: str,
        new_base_url: str = "",
        limit: int = typer.Option(1, min=1, max=20),
    ):
        print(asyncio.run(run_audit(report_id, new_model, new_base_url, "verify", limit)))

    # 功能：只读显示冻结批次及每项盲答/核源，不构造模型、不抓取网页。
    @app.command("belief-audit-status")
    def status(report_id: UUID):
        # 功能：在独立查询会话拼接已存结果，不迁移或写入状态。
        async def action():
            async with SessionLocal() as session:
                return await BeliefMigrationAudit(Repository(session)).status(report_id)

        print(asyncio.run(action()))
