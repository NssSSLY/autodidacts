# 文件职责：注册概念图查询/关系提议/历史刷新/前置目标与显式收费提取核验CLI，不隐式迁移。
from uuid import UUID

import typer
from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.brain.llm import build_llm
from autodidact.brain.observed import ObservedLLM
from autodidact.db import engine
from autodidact.knowledge.graph import (
    GraphQuery,
    KnowledgeGraph,
    Predicate,
    RelationDraft,
    graph_action,
)
from autodidact.repository import Repository
from autodidact.runtime import controller_lock
from autodidact.service_commands import execute_operation
from autodidact.service_runtime import DeliveryError


# 功能：在控制器锁下显式提取单主张或重核单关系，模型有观察/预算且不改信念；不自动运行研究。
async def model_graph_action(*, claim_id=None, relation_id=None, domain=""):
    async with (
        controller_lock(engine),
        async_sessionmaker(engine, expire_on_commit=False)() as session,
    ):
        graph = KnowledgeGraph(Repository(session))
        llm = ObservedLLM(build_llm(), engine, role="knowledge_relation")
        llm.bind(
            phase="knowledge_relation",
            claim_id=str(claim_id) if claim_id else None,
            relation_id=str(relation_id) if relation_id else None,
        )
        try:
            if relation_id:
                relation = await session.get(models.KnowledgeRelation, relation_id)
                if relation is None:
                    raise ValueError("关系不存在")
                return await graph.review(relation, llm)
            return await graph.extract(claim_id, llm, domain)
        except (ValueError, KeyError, TypeError) as exc:
            raise DeliveryError("关系提议/核验不满足协议，请检查库内主张及模型观察") from exc


# 功能：注册六个有界图谱命令；模型操作需显式确认，其余动作不请求模型或访问网页。
def register_graph_commands(app):
    # 功能：分页只读概念相邻边或单关系/历史，始终重算当前证据状态。
    @app.command("knowledge-graph")
    def show(
        concept_id: UUID | None = None,
        relation_id: UUID | None = None,
        predicate: Predicate | None = None,
        limit: int = typer.Option(20, min=1, max=50),
        offset: int = typer.Option(0, min=0, max=10000),
    ):
        execute_operation(
            lambda: graph_action(
                engine,
                "show",
                query=GraphQuery(
                    concept_id=concept_id,
                    relation_id=relation_id,
                    predicate=predicate,
                    limit=limit,
                    offset=offset,
                ),
            )
        )

    # 功能：从已有主张登记人工候选关系，保存出处和原范围，不将人工输入当事实。
    @app.command("propose-relation")
    def propose(claim_id: UUID, subject: str, predicate: Predicate, object_label: str, domain: str):
        execute_operation(
            lambda: graph_action(
                engine,
                "propose",
                claim_id=claim_id,
                draft=RelationDraft(
                    subject=subject, predicate=predicate, object=object_label, domain=domain
                ),
            )
        )

    # 功能：显式调用模型从单主张提取/原文复核关系；可收费，失败不提升状态。
    @app.command("extract-relation")
    def extract(claim_id: UUID, domain: str, allow_model: bool = False):
        if not allow_model:
            raise typer.BadParameter("可能收费，请确认 --allow-model 后执行")
        execute_operation(lambda: model_graph_action(claim_id=claim_id, domain=domain))

    # 功能：显式模型重核现有关系的方向/原文/范围；原观察留历史，不能借模型覆盖信念。
    @app.command("review-relation")
    def review(relation_id: UUID, allow_model: bool = False):
        if not allow_model:
            raise typer.BadParameter("可能收费，请确认 --allow-model 后执行")
        execute_operation(lambda: model_graph_action(relation_id=relation_id))

    # 功能：无模型地保存当前证据/争议/降级状态快照，不覆写历史报告。
    @app.command("refresh-relation")
    def refresh(relation_id: UUID):
        execute_operation(
            lambda: graph_action(engine, "refresh", query=GraphQuery(relation_id=relation_id))
        )

    # 功能：由supported的requires边排有限前置调查目标，循环/截断拒绝，不启动实际研究。
    @app.command("queue-prerequisites")
    def queue(concept_id: UUID, limit: int = typer.Option(3, min=1, max=20)):
        execute_operation(lambda: graph_action(engine, "queue", concept_id=concept_id, limit=limit))
