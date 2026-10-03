# 文件职责：注册命令行主入口、日志、初始化、学习循环、状态和冻结基准入口。
from __future__ import annotations

import asyncio
import json
import logging

import typer
from rich import print

from autodidact.advanced_commands import register_advanced_commands
from autodidact.agent import AutonomousLearner
from autodidact.brain.factory import build_judge
from autodidact.brain.llm import build_llm
from autodidact.commands import controlled, register_commands
from autodidact.config import agent_config, runtime_settings
from autodidact.db import SessionLocal, engine, init_database
from autodidact.experiments import freeze_suite
from autodidact.learning.reliable_evaluation import run_benchmark
from autodidact.metrics import dashboard
from autodidact.runtime import controller_lock

app = typer.Typer(no_args_is_help=True)
register_commands(app)
register_advanced_commands(app)


# 功能：按 LOG_LEVEL 设置标准日志格式，供学习进程输出阶段与错误。
def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, runtime_settings().log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# 功能：执行迁移初始化命令，把数据库升级到当前 HEAD。
@app.command("init-db")
def init_db_cmd() -> None:
    asyncio.run(init_database())
    print("[green]Database initialized.[/green]")


# 功能：初始化后在控制器互斥下恢复状态并创建必要的身份/种子目标。
@app.command("bootstrap")
def bootstrap_cmd() -> None:
    # 功能：异步初始化数据库、获取控制器锁并执行恢复与 bootstrap。
    async def _run():
        await init_database()
        async with controller_lock(engine), SessionLocal() as session:
            learner = AutonomousLearner(session, build_llm())
            await learner.repo.recover_interrupted()
            await learner.bootstrap()

    asyncio.run(_run())
    print("[green]Agent bootstrapped.[/green]")


# 功能：配置日志并运行一轮持久学习，向终端显示结果。
@app.command("run-once")
def run_once_cmd() -> None:
    configure_logging()

    # 功能：异步初始化会话，构造学习器并执行一次 run_cycle。
    async def _run():
        await init_database()
        async with SessionLocal() as session:
            learner = AutonomousLearner(session, build_llm())
            print(await learner.run_cycle())

    asyncio.run(_run())


# 功能：配置日志并启动有限轮次学习循环，可由 --max-cycles 覆盖默认上限。
@app.command("run")
def run_cmd(
    max_cycles: int = typer.Option(None, help="Override configured cycles for this process."),
) -> None:
    configure_logging()

    # 功能：异步初始化数据库和学习器，执行指定轮次数的 run_daemon。
    async def _run():
        await init_database()
        async with SessionLocal() as session:
            learner = AutonomousLearner(session, build_llm())
            await learner.run_daemon(max_cycles)

    asyncio.run(_run())


# 功能：读取并显示数据库状态面板，不在该命令中开展学习。
@app.command("status")
def status_cmd() -> None:
    # 功能：在只读查询会话中汇总目标、信念与评估统计并打印 JSON。
    async def _run():
        async with SessionLocal() as session:
            print(json.dumps(await dashboard(session), ensure_ascii=False, indent=2))

    asyncio.run(_run())


# 功能：冻结输入题集并执行无记忆基准，保存独立报告而不使用旧 benchmark.py 评分接口。
@app.command("benchmark")
def benchmark_cmd(path: str = "data/benchmark/sample.json") -> None:
    # 功能：冻结题集、隔离评估、保存 benchmark_run 和空记忆快照，返回报告及分数。
    async def action(repo, llm):
        suite = await freeze_suite(repo, path)
        return await run_benchmark(repo, llm, build_judge(engine), suite.id)

    print(asyncio.run(controlled(action)))


# 功能：输出已校验学习策略，不输出模型 API 密钥等运行秘密。
@app.command("show-config")
def show_config_cmd() -> None:
    print(agent_config().model_dump_json(indent=2))


if __name__ == "__main__":
    app()
