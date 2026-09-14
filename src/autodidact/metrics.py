from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from autodidact import models


async def dashboard(session: AsyncSession) -> dict:
    async def count(model):
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()

    total_goals = await count(models.Goal)
    passed = (await session.execute(select(func.count()).select_from(models.Goal).where(models.Goal.status == "passed"))).scalar_one()
    self_generated = (await session.execute(
        select(func.count()).select_from(models.Goal).where(models.Goal.source != "human")
    )).scalar_one()
    verified = (await session.execute(
        select(func.count()).select_from(models.Belief).where(models.Belief.status == "verified")
    )).scalar_one()
    total_beliefs = await count(models.Belief)
    avg_eval = (await session.execute(select(func.avg(models.Evaluation.score)))).scalar_one() or 0.0
    return {
        "total_goals": total_goals,
        "goal_success_rate": passed / total_goals if total_goals else 0.0,
        "self_generated_goals_rate": self_generated / total_goals if total_goals else 0.0,
        "beliefs": total_beliefs,
        "verified_beliefs_rate": verified / total_beliefs if total_beliefs else 0.0,
        "average_evaluation_score": float(avg_eval),
        "open_disputes": (await session.execute(
            select(func.count()).select_from(models.Dispute).where(models.Dispute.status.in_(["open", "investigating", "unresolved"]))
        )).scalar_one(),
    }
