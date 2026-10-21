# 文件职责：注册只读维护审计、规则建议、变更提议及显式确认/拒绝CLI，不请求模型或隐式迁移。
from uuid import UUID

import typer

from autodidact.db import engine
from autodidact.lifecycle import (
    Action,
    ChangeRequest,
    DecisionRequest,
    Kind,
    MaintenanceQuery,
    lifecycle_action,
)
from autodidact.service_commands import execute_operation


# 功能：注册五条维护命令，提议不改记忆，决定必须确认，所有写者沿用控制器互斥。
def register_lifecycle_commands(app):
    # 功能：查看active/archived记忆、单项历史或提议/决策，独立只读且不收费。
    @app.command("memory-maintenance")
    def show(
        kind: Kind = "belief",
        state: str = "active",
        entity_id: UUID | None = None,
        proposal_id: UUID | None = None,
        limit: int = typer.Option(20, min=1, max=50),
        offset: int = typer.Option(0, min=0, max=10000),
    ):
        execute_operation(
            lambda: lifecycle_action(
                engine,
                "show",
                query=MaintenanceQuery(
                    kind=kind,
                    state=state,
                    entity_id=entity_id,
                    proposal_id=proposal_id,
                    limit=limit,
                    offset=offset,
                ),
            )
        )

    # 功能：有限规则扫描并保存重复/相似/过旧维护建议，不执行归档、归并、恢复或模型请求。
    @app.command("suggest-memory")
    def suggest(kind: Kind = "belief", limit: int = typer.Option(5, min=1, max=20)):
        execute_operation(lambda: lifecycle_action(engine, "suggest", kind=kind, limit=limit))

    # 功能：人工提出归档/恢复/重复归并或复核，仅冻结当前依据，供后续逐项明确确认。
    @app.command("propose-memory-change")
    def propose(
        kind: Kind, entity_id: UUID, action: Action, reason: str, target_id: UUID | None = None
    ):
        execute_operation(
            lambda: lifecycle_action(
                engine,
                "propose",
                change=ChangeRequest(
                    kind=kind,
                    entity_id=entity_id,
                    action=action,
                    reason=reason,
                    target_id=target_id,
                ),
            )
        )

    # 功能：核对提议原快照/当前版本后显式执行单项变更，写不可变操作历史；无确认不访问数据库。
    @app.command("apply-memory-change")
    def apply(proposal_id: UUID, reason: str, actor: str = "local_user", confirm: bool = False):
        if not confirm:
            raise typer.BadParameter(
                "请先查看提议，再显式 --confirm；归档/恢复会改变默认记忆可见性"
            )
        execute_operation(
            lambda: lifecycle_action(
                engine,
                "decide",
                decision=DecisionRequest(
                    proposal_id=proposal_id,
                    decision="apply",
                    actor=actor,
                    reason=reason,
                    confirm=True,
                ),
            )
        )

    # 功能：显式拒绝维护提议并保留理由，不修改原实体/生命周期，也不能覆盖已执行决策。
    @app.command("reject-memory-change")
    def reject(proposal_id: UUID, reason: str, actor: str = "local_user", confirm: bool = False):
        if not confirm:
            raise typer.BadParameter("请显式 --confirm 记录拒绝决定")
        execute_operation(
            lambda: lifecycle_action(
                engine,
                "decide",
                decision=DecisionRequest(
                    proposal_id=proposal_id,
                    decision="reject",
                    actor=actor,
                    reason=reason,
                    confirm=True,
                ),
            )
        )
