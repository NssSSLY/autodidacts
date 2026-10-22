# 文件职责：提供F14四视图只读实验/长期观察CLI，不调用模型或保存报告。
from uuid import UUID

import typer

from autodidact.db import engine
from autodidact.experiment_dashboard import ExperimentQuery, experiment_dashboard
from autodidact.service_commands import execute_operation


# 功能：注册只读看板入口，复用与网页相同的严格视图/窗口/条件分组及有界读取。
def register_experiment_commands(app):
    # 功能：查看总览、报告目录、可靠条件组或单份实验详情，绝不触发评估/收费/历史报告生成。
    @app.command("experiment-dashboard")
    def show(
        view: str = "overview",
        days: int = typer.Option(30, min=1, max=365),
        until: str | None = None,
        kind: str | None = None,
        benchmark_id: UUID | None = None,
        report_id: UUID | None = None,
        group_id: str | None = None,
        limit: int = typer.Option(20, min=1, max=20),
        offset: int = typer.Option(0, min=0, max=10000),
    ):
        execute_operation(
            lambda: experiment_dashboard(
                engine,
                ExperimentQuery(
                    view=view,
                    days=days,
                    until=until,
                    kind=kind,
                    benchmark_id=benchmark_id,
                    report_id=report_id,
                    group_id=group_id,
                    limit=limit,
                    offset=offset,
                ),
            )
        )
