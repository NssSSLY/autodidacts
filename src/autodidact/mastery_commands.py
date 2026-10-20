# 文件职责：注册无模型调用的F09掌握查询、不可变快照和显式学习/复习目标排队命令。
from uuid import UUID

import typer

from autodidact.db import engine
from autodidact.mastery import mastery_action
from autodidact.service_commands import execute_operation


# 功能：注册三条掌握命令，查看不写状态、排队不运行研究，不隐含初始化数据库或评估。
def register_mastery_commands(app):
    # 功能：只读汇总固定基准的主题掌握、未知/缺口及条件组，不发模型请求。
    @app.command("mastery")
    def show(frozen_id: UUID, group_id: str | None = None):
        execute_operation(lambda: mastery_action(engine, "show", frozen_id, group_id))

    # 功能：显式保存新掌握快照到报告库，相同输入/同日复用，不覆盖旧认知。
    @app.command("snapshot-mastery")
    def snapshot(frozen_id: UUID, group_id: str | None = None):
        execute_operation(lambda: mastery_action(engine, "snapshot", frozen_id, group_id))

    # 功能：仅按明确弱项排有限学习/复习目标，missing/unknown不当判错；实际研究仍需用户启动学习。
    @app.command("queue-mastery-goals")
    def queue(
        frozen_id: UUID, group_id: str | None = None, limit: int = typer.Option(3, min=1, max=20)
    ):
        execute_operation(lambda: mastery_action(engine, "queue", frozen_id, group_id, limit))
