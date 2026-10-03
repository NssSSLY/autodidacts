# 文件职责：在实际学习运行时检查可选 Day 0/7/14/21/30 冻结基准里程碑。
from __future__ import annotations

from datetime import UTC, datetime

from autodidact.config import runtime_settings
from autodidact.experiments import canonical_hash, evaluate_suite
from autodidact.learning.reliable_evaluation import load_frozen, training_snapshot, verify_frozen


# 功能：按当前实际日龄选择里程碑，幂等比较无记忆/有记忆并保存快照，不补造错过日期的数据。
async def scheduled_benchmarks(repo, learner, judge):
    """Optional Day 0/7/14/21/30 paired experiments; never invent missing milestones."""
    benchmark_id = runtime_settings().benchmark_snapshot_id
    if not benchmark_id:
        return {"enabled": False}
    suite = await load_frozen(repo, benchmark_id)
    training = await training_snapshot(repo) if verify_frozen(suite.payload) else []
    questions = [q for g in training for q in [g["title"], g["description"]] if q]
    model_key = canonical_hash([benchmark_id, learner.provider_name, learner.model_name])
    start = await repo.get_report("experiment_start", model_key)
    if start is None:
        start = await repo.save_report(
            "experiment_start",
            {
                "started_at": datetime.now(UTC).isoformat(),
                "benchmark_id": benchmark_id,
                "model": learner.model_name,
            },
            model_key,
        )
    elapsed = (datetime.now(UTC) - datetime.fromisoformat(start.payload["started_at"])).days
    milestone = max(day for day in [0, 7, 14, 21, 30] if day <= elapsed)
    key = f"{model_key}:day{milestone}"
    existing = await repo.get_report("scheduled_benchmark", key)
    if existing:
        return {"report_id": str(existing.id), "already_recorded": True}
    memory = await repo.accepted_memory()
    baseline = await evaluate_suite(learner, judge, suite.payload, [], training_questions=questions)
    aided = await evaluate_suite(
        learner, judge, suite.payload, memory, training_questions=questions
    )
    report = await repo.save_report(
        "scheduled_benchmark",
        {
            "milestone": milestone,
            "actual_elapsed_days": elapsed,
            "benchmark_id": benchmark_id,
            "benchmark_sha256": suite.payload["sha256"],
            "memory_snapshot": memory,
            "memory_sha256": canonical_hash(memory),
            "base": baseline,
            "with_memory": aided,
            "protocol": baseline["protocol"],
            "scoring_protocol": baseline["scoring_protocol"],
            "comparison_key": baseline["comparison_key"],
            "training_snapshot": training,
            "training_sha256": canonical_hash(training),
            "memory_gain": aided["score"] - baseline["score"]
            if aided["complete"] and baseline["complete"]
            else None,
            "note": "实际运行日单独保存；未运行的里程碑不会补造历史分数。",
        },
        key,
    )
    return {
        "report_id": str(report.id),
        "milestone": milestone,
        "memory_gain": report.payload["memory_gain"],
    }
