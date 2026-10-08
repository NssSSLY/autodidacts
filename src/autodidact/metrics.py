# 文件职责：提供数据库状态计数与学习/信念比例，不将数量当成智能提升证明。
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from autodidact import models
from autodidact.learning.ground_truth import evaluation_groups
from autodidact.provider_runtime import provider_health


# 功能：统计目标/信念/争议与按版本模型分组的评估；多组时总均值未知，不混算历史评分。
async def dashboard(session: AsyncSession) -> dict:
    # 功能：对指定业务表执行行数统计，供状态面板复用。
    async def count(model):
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()

    total_goals = await count(models.Goal)
    passed = (
        await session.execute(
            select(func.count()).select_from(models.Goal).where(models.Goal.status == "passed")
        )
    ).scalar_one()
    self_generated = (
        await session.execute(
            select(func.count()).select_from(models.Goal).where(models.Goal.source != "human")
        )
    ).scalar_one()
    verified = (
        await session.execute(
            select(func.count())
            .select_from(models.Belief)
            .where(models.Belief.status == "verified")
        )
    ).scalar_one()
    total_beliefs = await count(models.Belief)
    score_groups = evaluation_groups((await session.scalars(select(models.Evaluation))).all())
    avg_eval = next(iter(score_groups.values()))["mean_score"] if len(score_groups) == 1 else None
    return {
        "total_goals": total_goals,
        "goal_success_rate": passed / total_goals if total_goals else 0.0,
        "self_generated_goals_rate": self_generated / total_goals if total_goals else 0.0,
        "beliefs": total_beliefs,
        "verified_beliefs_rate": verified / total_beliefs if total_beliefs else 0.0,
        "average_evaluation_score": avg_eval,
        "evaluation_score_groups": score_groups,
        "provider_health": await provider_health(session, 20),
        "open_disputes": (
            await session.execute(
                select(func.count())
                .select_from(models.Dispute)
                .where(models.Dispute.status.in_(["open", "investigating", "unresolved"]))
            )
        ).scalar_one(),
    }
