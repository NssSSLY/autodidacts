# 文件职责：注册单用户运行计划、只读健康/进度、受控备份恢复及发布校验CLI，不隐含模型探测/迁移。
import asyncio
import os

import typer
from rich import print

from autodidact.backups import backup_database, restore_database, verify_backup
from autodidact.db import engine
from autodidact.delivery import DeliveryError, verify_release
from autodidact.service_runtime import (
    PlanRunner,
    RunStore,
    learning_cycle,
    read_run_plan,
    service_health,
    service_logger,
)


# 功能：统一运行交付操作，仅展示自定义安全拒绝或异常类别，不打印数据库连接/子进程错误正文。
def execute_operation(action):
    try:
        result = action()
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        print(result)
        return result
    except Exception as exc:  # noqa: BLE001 - 交付CLI错误不能展开配置/外部异常正文。
        print(
            {
                "ok": False,
                "reason": str(exc) if isinstance(exc, DeliveryError) else type(exc).__name__,
                "hint": "请核对使用指南、版本、路径和权限；外部错误正文不展示。",
            }
        )
        raise typer.Exit(1) from None


# 功能：注册显式运维操作；真正恢复必须确认新空库，查看不触发学习或自动迁移。
def register_service_commands(app):
    # 功能：离线检查/显示计划，无数据库或模型操作。
    @app.command("check-run-plan")
    def check_plan(path: str):
        execute_operation(lambda: read_run_plan(path).model_dump())

    # 功能：按明确计划运行有限学习轮次；可能付费，重启不会自动继续旧计划。
    @app.command("run-plan")
    def run_plan(path: str):
        # 功能：先检查本地参数再组装安全日志/计划运行器，不自动迁移。
        def action():
            plan = read_run_plan(path)
            return PlanRunner(engine, learning_cycle, logger=service_logger()).run(plan)

        execute_operation(action)

    # 功能：只读诊断数据库/迁移版本；无外部付费探测，未就绪退出码1。
    @app.command("service-health")
    def health():
        result = execute_operation(lambda: service_health(engine))
        if not result["ready"]:
            raise typer.Exit(1)

    # 功能：读取最近20次运行的持久快照，不据running推断进程存活。
    @app.command("service-runs")
    def runs():
        execute_operation(lambda: RunStore(engine).status())

    # 功能：只在用户显式调用时创建新备份文件/摘要清单，不覆盖旧文件或自动打包密钥。
    @app.command("backup-db")
    def backup(output: str):
        execute_operation(lambda: backup_database(engine, output))

    # 功能：离线验证归档摘要与清单，不连接数据库，不执行归档内SQL。
    @app.command("verify-backup")
    def check_backup(path: str):
        # 功能：只输出摘要核验结果/版本，不在终端展开全部私有表数据。
        def action():
            payload = verify_backup(path)
            return {
                "verified": True,
                "revision": payload["revision"],
                "tables": len(payload["tables"]),
            }

        execute_operation(action)

    # 功能：仅恢复可信备份到显式环境变量指定的新空*_restore/*_test库，不切换当前配置。
    @app.command("restore-db")
    def restore(path: str, confirm_empty_target: bool = False):
        # 功能：固定读取专用恢复变量，不接受命令行密码或默认覆盖当前库。
        def action():
            target = os.environ.get("AUTODIDACT_RESTORE_DATABASE_URL")
            if not target:
                raise DeliveryError("请先设置AUTODIDACT_RESTORE_DATABASE_URL指向新空_restore库")
            return restore_database(engine.url, target, path, confirmed=confirm_empty_target)

        execute_operation(action)

    # 功能：离线校验完整发布文件夹，不运行EXE、不加载用户.env或探测模型。
    @app.command("verify-release")
    def release(path: str):
        execute_operation(lambda: verify_release(path))
