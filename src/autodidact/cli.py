from __future__ import annotations

import asyncio
import json
import logging

import typer
from rich import print

from autodidact.agent import AutonomousLearner
from autodidact.benchmark import run_benchmark
from autodidact.brain.llm import build_llm
from autodidact.config import agent_config, runtime_settings
from autodidact.db import SessionLocal, init_database
from autodidact.metrics import dashboard

app = typer.Typer(no_args_is_help=True)


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
        async with SessionLocal() as session:
            learner = AutonomousLearner(session, build_llm())
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
            await learner.bootstrap()
            print(await learner.run_cycle())
    asyncio.run(_run())


@app.command("run")
def run_cmd(max_cycles: int = typer.Option(None, help="Override configured cycles for this process.")) -> None:
    configure_logging()
    async def _run():
        await init_database()
        async with SessionLocal() as session:
            learner = AutonomousLearner(session, build_llm())
            await learner.bootstrap()
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
    async def _run():
        result = await run_benchmark(build_llm(), path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    asyncio.run(_run())


@app.command("show-config")
def show_config_cmd() -> None:
    print(agent_config().model_dump_json(indent=2))


if __name__ == "__main__":
    app()
