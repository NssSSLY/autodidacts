# 文件职责：注册F12方法定义/案例、重复A/B、只读审计与显式启用/回退CLI，不自动收费或改核心代码。
from typing import Annotated
from uuid import UUID

import typer

from autodidact.db import engine
from autodidact.service_commands import execute_operation
from autodidact.skill_methods import (
    ExperimentSpec,
    MethodCase,
    MethodQuery,
    MethodSpec,
    read_method_file,
    skill_method_action,
)


# 功能：注册七个方法入口，纳管/案例/决定须确认，运行另需allow-model，查看不调用模型。
def register_skill_commands(app):
    # 功能：有界只读查看全部技能、某技能管理历史或冻结对照的可复算结果。
    @app.command("skill-methods")
    def show(
        skill_id: UUID | None = None,
        experiment_id: UUID | None = None,
        limit: int = typer.Option(20, min=1, max=20),
        offset: int = typer.Option(0, min=0, max=1000),
    ):
        execute_operation(
            lambda: skill_method_action(
                engine,
                "show",
                query=MethodQuery(
                    skill_id=skill_id, experiment_id=experiment_id, limit=limit, offset=offset
                ),
            )
        )

    # 功能：显式把现有技能纳入F12，保存适用条件/出处定义并暂停原自动采用，不修改正文。
    @app.command("register-skill-method")
    def register(path: str, confirm: bool = False):
        if not confirm:
            raise typer.BadParameter("注册会暂停此技能旧自动采用，查看清单后显式--confirm")
        execute_operation(
            lambda: skill_method_action(
                engine, "register", spec=read_method_file(path, MethodSpec), confirm=True
            )
        )

    # 功能：保存真实完成会话关联的人工案例，新案例使旧对照过期并暂停方法，保留全部原结果。
    @app.command("record-skill-case")
    def case(path: str, confirm: bool = False):
        if not confirm:
            raise typer.BadParameter("人工案例会暂停旧方法采用，查看后显式--confirm")
        execute_operation(
            lambda: skill_method_action(
                engine, "case", case=read_method_file(path, MethodCase), confirm=True
            )
        )

    # 功能：冻结可靠独立题/负例/重复顺序和模型记忆条件，不答题、不自动启用方法。
    @app.command("freeze-skill-experiment")
    def freeze(path: str):
        execute_operation(
            lambda: skill_method_action(
                engine, "freeze", spec=read_method_file(path, ExperimentSpec)
            )
        )

    # 功能：仅显式运行有界重复A/B答卷，可能收费；成功重用，失败和未知发送另行授权重试。
    @app.command("run-skill-experiment")
    def run(
        experiment_id: UUID,
        max_items: int = typer.Option(10, min=1, max=20),
        allow_model: bool = False,
        retry_failed: bool = False,
        retry_uncertain: bool = False,
    ):
        if not allow_model:
            raise typer.BadParameter("方法对照可能收费，须显式--allow-model；查看不答题")
        execute_operation(
            lambda: skill_method_action(
                engine,
                "run",
                experiment_id=experiment_id,
                max_items=max_items,
                allow_model=True,
                retry_failed=retry_failed,
                retry_uncertain=retry_uncertain,
            )
        )

    # 功能：只在完整重复正迁移/无退化且当前定义仍一致时显式启用，request-id用于原子幂等。
    @app.command("activate-skill-method")
    def activate(
        experiment_id: UUID,
        reason: str,
        request_id: Annotated[UUID, typer.Option()],
        actor: str = "local_user",
        confirm: bool = False,
    ):
        if not confirm:
            raise typer.BadParameter("先查看重复对照再显式--confirm；此操作会允许后续规划选择方法")
        execute_operation(
            lambda: skill_method_action(
                engine,
                "activate",
                experiment_id=experiment_id,
                reason=reason,
                request_id=request_id,
                actor=actor,
                confirm=True,
            )
        )

    # 功能：显式回退为不用此方法的基线，保留定义/答卷/旧采用记录，不自动恢复归档或旧核心代码。
    @app.command("rollback-skill-method")
    def rollback(
        skill_id: UUID,
        reason: str,
        request_id: Annotated[UUID, typer.Option()],
        actor: str = "local_user",
        confirm: bool = False,
    ):
        if not confirm:
            raise typer.BadParameter("确认停用此方法后显式--confirm，使用新的request-id")
        execute_operation(
            lambda: skill_method_action(
                engine,
                "rollback",
                skill_id=skill_id,
                reason=reason,
                request_id=request_id,
                actor=actor,
                confirm=True,
            )
        )
