# 文件职责：检查目标失败计数达到上限后进入 blocked，防止无限重试。
from types import SimpleNamespace

import pytest

from autodidact.enums import GoalStatus
from autodidact.repository import Repository


class CommitOnlySession:
    # 功能：初始化最小提交计数会话替身。
    def __init__(self):
        self.commits = 0

    # 功能：记录状态更新是否提交，不写数据库。
    async def commit(self):
        self.commits += 1


# 功能：验证失败次数达到 max_retry 时阻断目标并保存完成时间。
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
