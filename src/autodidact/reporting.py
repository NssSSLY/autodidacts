# 文件职责：汇总指定时间窗的闭卷趋势、资源用量、错误及冻结实验报告。
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from autodidact import models


# 功能：读取时间窗内评估/操作/报告，生成成长与费用摘要；日常评分不等于冻结基准增益。
async def longitudinal_report(repo, days=30):
    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    evaluations = list(
        (
            await repo.s.scalars(
                select(models.Evaluation)
                .where(models.Evaluation.created_at >= start)
                .order_by(models.Evaluation.created_at)
            )
        ).all()
    )
    operations = list(
        (
            await repo.s.scalars(
                select(models.OperationEvent).where(models.OperationEvent.created_at >= start)
            )
        ).all()
    )
    reports = list(
        (
            await repo.s.scalars(
                select(models.ResearchReport)
                .where(
                    models.ResearchReport.created_at >= start,
                    models.ResearchReport.kind.in_(
                        [
                            "memory_daily",
                            "model_migration",
                            "benchmark_run",
                            "skill_validation",
                            "scheduled_benchmark",
                        ]
                    ),
                )
                .order_by(models.ResearchReport.created_at)
            )
        ).all()
    )
    timeline = {}
    for evaluation in evaluations:
        if (evaluation.components or {}).get("audit", {}).get("protocol") != "closed_book_v1":
            continue
        day = evaluation.created_at.astimezone(UTC).date().isoformat()
        point = timeline.setdefault(
            day, {"scores": [], "calibration": [], "transfer": [], "passed": 0}
        )
        point["scores"].append(evaluation.score)
        point["calibration"].append(evaluation.components.get("calibration", 0))
        point["transfer"].append(evaluation.components.get("transfer", 0))
        point["passed"] += int(evaluation.passed)
    daily = []
    for day, point in timeline.items():
        daily.append(
            {
                "day": day,
                "evaluations": len(point["scores"]),
                "mean_score": sum(point["scores"]) / len(point["scores"]),
                "mean_calibration": sum(point["calibration"]) / len(point["calibration"]),
                "mean_transfer": sum(point["transfer"]) / len(point["transfer"]),
                "passed": point["passed"],
            }
        )
    usage = Counter()
    errors = Counter()
    for op in operations:
        usage[op.resource] += op.actual if op.actual is not None else op.reserved
        if op.details.get("error"):
            errors[op.details["error"]] += 1
    open_disputes = await repo.open_disputes(1000)
    return {
        "window_start_utc": start.isoformat(),
        "window_end_utc": now.isoformat(),
        "daily_learning": daily,
        "resource_usage": dict(usage),
        "operation_errors": dict(errors),
        "open_disputes": len(open_disputes),
        "milestones": [
            {**r.payload, "report_id": str(r.id)}
            for r in reports
            if r.kind == "scheduled_benchmark"
        ],
        "experiment_reports": [
            {"id": str(r.id), "kind": r.kind, "created_at": r.created_at.isoformat()}
            for r in reports
        ],
        "note": "日常学习评分不是冻结基准增益；跨天成长需对照相同模型、题集和预算。",
    }
