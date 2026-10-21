# 文件职责：注册F11来源/路由/溯源只读、冻结清单及显式分阶段模型审查CLI，不自动切换模型或修改真值。
from typing import Literal
from uuid import UUID

import typer

from autodidact.cognitive_review import cognition_action, read_review_manifest
from autodidact.cognitive_sources import Role
from autodidact.db import engine
from autodidact.service_commands import execute_operation


# 功能：注册六个F11入口；模型阶段需独立授权，网页及不确定重发另需人工确认。
def register_cognition_commands(app):
    # 功能：仅读取认知来源配置/身份声明，无数据库、模型或浏览器动作。
    @app.command("cognitive-sources")
    def sources():
        execute_operation(lambda: cognition_action(engine, "sources"))

    # 功能：从精确来源样本与Profile参考输出可回退路由建议，不自动应用或联网探测。
    @app.command("route-cognitive-sources")
    def route(fallback: str, role: Role = "blind", topic: str = "general"):
        if not 1 <= len(topic.strip()) <= 200:
            raise typer.BadParameter("topic长度须1–200")
        execute_operation(
            lambda: cognition_action(engine, "route", role=role, topic=topic, fallback=fallback)
        )

    # 功能：有界只读查看Claim/Belief明确引用与同会话观察，或单条模型观察的来源上下文。
    @app.command("cognitive-provenance")
    def provenance(kind: Literal["claim", "belief", "observation"], entity_id: UUID):
        execute_operation(
            lambda: cognition_action(engine, "provenance", kind=kind, entity_id=entity_id)
        )

    # 功能：冻结人工清单/中性声明/角色来源/关联认知，不调用模型或自动开始学习。
    @app.command("freeze-cognitive-review")
    def freeze(path: str):
        execute_operation(
            lambda: cognition_action(engine, "freeze", manifest=read_review_manifest(path))
        )

    # 功能：只读审查冻结内容、各阶段原观察及0级论证图，不恢复未完成调用。
    @app.command("show-cognitive-review")
    def show(review_id: UUID):
        execute_operation(lambda: cognition_action(engine, "show", review_id=review_id))

    # 功能：显式运行一次有界盲答/批判/综合，成功重用；失败或不确定重发需额外选项且次数有上限。
    @app.command("run-cognitive-review")
    def run(
        review_id: UUID,
        phase: Literal["blind", "critique", "synthesis"] = "blind",
        allow_model: bool = False,
        allow_web: bool = False,
        retry_failed: bool = False,
        retry_uncertain: bool = False,
    ):
        if not allow_model:
            raise typer.BadParameter("该阶段可能收费；须显式--allow-model，网页另需--allow-web")
        execute_operation(
            lambda: cognition_action(
                engine,
                "run",
                review_id=review_id,
                phase=phase,
                allow_model=True,
                allow_web=allow_web,
                retry_failed=retry_failed,
                retry_uncertain=retry_uncertain,
            )
        )
