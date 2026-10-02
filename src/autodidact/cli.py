from __future__ import annotations

import asyncio
import json
import logging

import typer
from rich import print

from autodidact.agent import AutonomousLearner
from autodidact.brain.factory import build_judge
from autodidact.brain.llm import build_llm
from autodidact.commands import controlled, register_commands
from autodidact.config import agent_config, runtime_settings
from autodidact.db import SessionLocal, engine, init_database
from autodidact.experiments import evaluate_suite, freeze_suite
from autodidact.metrics import dashboard
from autodidact.runtime import controller_lock

app = typer.Typer(no_args_is_help=True)
register_commands(app)


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, runtime_settings().log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command("init-db")
def init_db_cmd() -> None:
    asyncio.run(init_database())
    print("[green]Database initialized.[/green]")


@app.command("bootstrap")
def bootstrap_cmd() -> None:
    async def _run():
        await init_database()
        async with controller_lock(engine), SessionLocal() as session:
            learner = AutonomousLearner(session, build_llm())
            await learner.repo.recover_interrupted()
            await learner.bootstrap()

    asyncio.run(_run())
    print("[green]Agent bootstrapped.[/green]")


@app.command("run-once")
def run_once_cmd() -> None:
    configure_logging()

    async def _run():
        await init_database()
        async with SessionLocal() as session:
            learner = AutonomousLearner(session, build_llm())
            print(await learner.run_cycle())

    asyncio.run(_run())


@app.command("run")
def run_cmd(
    max_cycles: int = typer.Option(None, help="Override configured cycles for this process."),
) -> None:
    configure_logging()

    async def _run():
        await init_database()
        async with SessionLocal() as session:
            learner = AutonomousLearner(session, build_llm())
            await learner.run_daemon(max_cycles)

    asyncio.run(_run())


@app.command("status")
def status_cmd() -> None:
    async def _run():
        async with SessionLocal() as session:
            print(json.dumps(await dashboard(session), ensure_ascii=False, indent=2))

    asyncio.run(_run())


@app.command("benchmark")
def benchmark_cmd(path: str = "data/benchmark/sample.json") -> None:
    async def action(repo, llm):
        suite = await freeze_suite(repo, path)
        result = await evaluate_suite(llm, build_judge(engine), suite.payload["items"], [])
        report = await repo.save_report(
            "benchmark_run", {**result, "benchmark_id": str(suite.id), "memory_snapshot": []}
        )
        return {"report_id": str(report.id), **result}

    print(asyncio.run(controlled(action)))


@app.command("show-config")
def show_config_cmd() -> None:
    print(agent_config().model_dump_json(indent=2))


if __name__ == "__main__":
    app()
