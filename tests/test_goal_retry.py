from types import SimpleNamespace

import pytest

from autodidact.enums import GoalStatus
from autodidact.repository import Repository


class CommitOnlySession:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_failed_goal_becomes_blocked_at_retry_limit():
    session = CommitOnlySession()
    repo = Repository(session)  # type: ignore[arg-type]
    goal = SimpleNamespace(
        status=GoalStatus.RESEARCHING,
        started_at=None,
        completed_at=None,
        retry_count=1,
        confidence_after=None,
    )

    await repo.update_goal_status(goal, GoalStatus.FAILED, max_retry=2)

    assert goal.retry_count == 2
    assert goal.status == GoalStatus.BLOCKED
    assert goal.completed_at is not None
    assert session.commits == 1
