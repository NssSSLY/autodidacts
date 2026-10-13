# 文件职责：注册持久索引任务的创建/只读状态/有界运行/暂停恢复/重试/验证/显式版本切换CLI。
import asyncio
from uuid import UUID

import typer
from rich import print

from autodidact.db import engine
from autodidact.retrieval_jobs import job_action


# 功能：注册索引运营命令，不初始化认知模型、不自动迁移或生成向量。
def register_retrieval_commands(app):
    # 功能：创建候选版本并记入持久队列；需要已初始化0009库和启用嵌入配置，但不实际调用服务。
    @app.command("create-retrieval-job")
    def create(max_attempts: int = typer.Option(3, min=1, max=5)):
        print(asyncio.run(job_action(engine, "create", max_attempts=max_attempts)))

    # 功能：只读列出任务/活动版本/失败摘要，指定UUID只查看该任务，不发起研究。
    @app.command("retrieval-job-status")
    def status(job_id: UUID | None = None, limit: int = typer.Option(20, min=1, max=50)):
        print(asyncio.run(job_action(engine, "status", job_id, limit=limit)))

    # 功能：执行最多max_items个工作项，完成项复用；此命令可能实际调用嵌入服务并计费。
    @app.command("run-retrieval-job")
    def run(job_id: UUID, max_items: int = typer.Option(200, min=1, max=10000)):
        print(asyncio.run(job_action(engine, "run", job_id, max_items=max_items)))

    # 功能：明确请求pause/resume/retry；重排本身不启动服务请求，重试不清零次数。
    @app.command("control-retrieval-job")
    def control(job_id: UUID, action: str):
        if action not in {"pause", "resume", "retry"}:
            raise typer.BadParameter("action需为pause / resume / retry")
        print(asyncio.run(job_action(engine, action, job_id)))

    # 功能：检查当前内容/覆盖/向量/指纹/实际索引结构并存审计，不调用模型。
    @app.command("verify-retrieval-job")
    def verify(job_id: UUID):
        print(asyncio.run(job_action(engine, "verify", job_id)))

    # 功能：独占控制器并重新核验后原子启用版本，不自动更改模型配置或任何信念。
    @app.command("activate-retrieval-job")
    def activate(job_id: UUID):
        print(asyncio.run(job_action(engine, "activate", job_id)))

    # 功能：用户显式回到原检索索引，不删除候选版本；旧向量仍受当前配置指纹过滤。
    @app.command("use-legacy-retrieval")
    def legacy():
        print(asyncio.run(job_action(engine, "legacy")))
