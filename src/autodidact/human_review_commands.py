# 文件职责：注册人工复核列表/冻结请求/逐项决定命令，不自动启动调查、收费或修改认知真值。
from typing import Annotated
from uuid import UUID

import typer

from autodidact.db import engine
from autodidact.human_review import (
    Action,
    ReviewDecision,
    ReviewQuery,
    human_review_action,
    read_review_request,
)
from autodidact.service_commands import execute_operation


# 功能：注册三个受控人工入口，保留现有只读认知审计和专属F12/F13操作，不开放通用ORM写入。
def register_human_review_commands(app):
    # 功能：有界查看人工请求或提议详情/依据有效性/决定，查看不写数据库或调用模型。
    @app.command("human-reviews")
    def show(
        proposal_id: UUID | None = None,
        entity_id: UUID | None = None,
        action: Action | None = None,
        state: str = "all",
        limit: int = typer.Option(20, min=1, max=20),
        offset: int = typer.Option(0, min=0, max=10000),
    ):
        execute_operation(
            lambda: human_review_action(
                engine,
                "show",
                query=ReviewQuery(
                    proposal_id=proposal_id,
                    entity_id=entity_id,
                    action=action,
                    state=state,
                    limit=limit,
                    offset=offset,
                ),
            )
        )

    # 功能：读取人工JSON并冻结现有实体/原文依据，保存提议但不执行排队或更正。
    @app.command("propose-human-review")
    def propose(path: str):
        execute_operation(
            lambda: human_review_action(engine, "propose", request=read_review_request(path))
        )

    # 功能：先查看提议后显式批准或拒绝；无确认在访问数据库前拒绝，批准不运行实际研究。
    @app.command("decide-human-review")
    def decide(
        proposal_id: UUID,
        choice: str,
        reason: str,
        proposal_sha256: Annotated[str, typer.Option()],
        actor: str = "local_user",
        confirm: bool = False,
    ):
        if not confirm:
            raise typer.BadParameter(
                "先查看提议/当前依据，再显式--confirm记录决定；批准只排队或批注"
            )
        execute_operation(
            lambda: human_review_action(
                engine,
                "decide",
                decision=ReviewDecision(
                    proposal_id=proposal_id,
                    proposal_sha256=proposal_sha256,
                    decision=choice,
                    actor=actor,
                    reason=reason,
                    confirm=True,
                ),
            )
        )
