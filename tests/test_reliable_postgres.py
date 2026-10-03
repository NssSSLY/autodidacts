# 文件职责：仅在显式*_test临时schema验证F03 JSONB新协议往返与旧冻结报告/信念保留；不访问日常库。
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_postgres_integration import (
    postgres_engine as postgres_engine,  # noqa: PLC0414 - pytest共享隔离fixture
)

from autodidact import models
from autodidact.experiments import freeze_suite
from autodidact.learning.ground_truth import digest
from autodidact.learning.reliable_evaluation import training_snapshot, verify_frozen
from autodidact.migrations import upgrade_database
from autodidact.repository import Repository


# 功能：现有0008结构即可读写版本/真值报告，旧JSONB快照及原信念不改写，不需要新增schema。
async def test_reliable_protocol_preserves_legacy_reports(postgres_engine):
    await upgrade_database(postgres_engine)
    maker = async_sessionmaker(postgres_engine, expire_on_commit=False)
    old_items = [
        {
            "question": "legacy question",
            "rubric": "",
            "category": "legacy",
            "accepted_answers": ["old"],
        }
    ]
    old_payload = {"items": old_items, "sha256": digest(old_items)}
    async with maker() as session:
        repo = Repository(session)
        old = await repo.save_report("frozen_benchmark", old_payload, old_payload["sha256"])
        old_id = old.id
        belief = models.Belief(
            topic="legacy", statement="旧结论", status="verified", confidence=0.9
        )
        goal = models.Goal(
            title="真实训练目标", description="训练内容", status="passed", source="human"
        )
        session.add_all([belief, goal])
        await session.commit()
        belief_id = belief.id
        session.add(models.LearningSession(goal_id=goal.id))
        await session.commit()
        current = await freeze_suite(repo, "data/benchmark/reliable_sample.json")
        assert verify_frozen(current.payload).suite_version == 1
        assert (await freeze_suite(repo, "data/benchmark/reliable_sample.json")).id == current.id
        assert (await training_snapshot(repo))[0]["title"] == "真实训练目标"
    async with maker() as session:
        assert (await session.get(models.ResearchReport, old_id)).payload == old_payload
        assert verify_frozen((await session.get(models.ResearchReport, old_id)).payload) is None
        old_belief = await session.get(models.Belief, belief_id)
        assert old_belief.statement == "旧结论" and old_belief.confidence == 0.9
