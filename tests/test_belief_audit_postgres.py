# 文件职责：仅显式*_test随机schema验证F04冻结/盲答报告幂等、会话复用和旧认知保留，不调用真实模型。
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_belief_audit import Candidate
from test_postgres_integration import postgres_engine as postgres_engine  # noqa: PLC0414

from autodidact import models
from autodidact.belief_audit import PROTOCOL, AuditManifest, BeliefMigrationAudit, freeze_audit
from autodidact.learning.ground_truth import model_identity
from autodidact.migrations import upgrade_database
from autodidact.repository import Repository
from autodidact.runtime import controller_lock


# 功能：现有0009无需加表即能保存冻结/盲答及阻断占位会话，跨实例复用且不改变旧信念。
async def test_audit_reports_preserve_old_state_and_replay(postgres_engine):
    await upgrade_database(postgres_engine)
    maker = async_sessionmaker(postgres_engine, expire_on_commit=False)
    provider = Candidate()
    async with controller_lock(postgres_engine), maker() as session:
        belief = models.Belief(
            statement="旧结论",
            topic="测试",
            status="verified",
            confidence=0.9,
            explanation="旧解释",
            metadata_json={"legacy": True},
        )
        session.add(belief)
        await session.commit()
        belief_id = belief.id
        manifest = AuditManifest(
            protocol=PROTOCOL,
            label="test",
            reviewer="测试审核者",
            neutral_questions_confirmed=True,
            items=[{"belief_id": belief_id, "question": "如何分析相关原理及适用条件？"}],
        )
        repo = Repository(session)
        first = await freeze_audit(repo, manifest, {"model": "old"}, model_identity(provider))
        second = await freeze_audit(repo, manifest, {"model": "old"}, model_identity(provider))
        assert first["report_id"] == second["report_id"]
        key = UUID(first["report_id"])
        await BeliefMigrationAudit(repo, provider).run(key, "answer")
    async with controller_lock(postgres_engine), maker() as session:
        result = await BeliefMigrationAudit(Repository(session), provider).run(key, "answer")
        assert result["processed"] == 0 and len(provider.calls) == 1
        old = await session.get(models.Belief, belief_id)
        assert old.statement == "旧结论" and old.confidence == 0.9 and old.status == "verified"
        assert old.metadata_json == {"legacy": True}
        assert await session.scalar(select(func.count()).select_from(models.LearningSession)) == 1
        goal = await session.scalar(select(models.Goal))
        assert (
            goal.status == "blocked"
            and (await session.scalar(select(func.count()).select_from(models.Dispute))) == 0
        )
