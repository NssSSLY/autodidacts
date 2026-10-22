# 文件职责：有界只读汇总真实实验、UTC费用/错误和观察时间，复用可靠重算/保持与F11/F12审计，不调用模型或保存报告。
from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy import String, case, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.cognitive_review import CognitiveReview
from autodidact.delivery import DeliveryError
from autodidact.learning.evaluation_contracts import ExamAnswer
from autodidact.learning.ground_truth import (
    HELD_OUT,
    digest,
    evaluation_groups,
    grade_answer,
    summarize,
)
from autodidact.learning.reliable_evaluation import recompute_run, verify_frozen
from autodidact.mastery import condition_key, historical_runs, observed_time
from autodidact.repository import Repository
from autodidact.skill_methods import MethodQuery, SkillMethods

ReportKind = Literal[
    "frozen_benchmark",
    "benchmark_run",
    "benchmark_retention",
    "benchmark_regrade",
    "model_migration",
    "skill_validation",
    "skill_method_experiment",
    "cognitive_review",
    "mastery_profile",
    "progress",
    "memory_daily",
    "scheduled_benchmark",
]
KINDS = list(ReportKind.__args__)
SAFE_FAILURES = [
    "connection",
    "timeout",
    "rate_limit",
    "upstream",
    "authentication",
    "forbidden",
    "access_challenge",
    "not_found",
    "request_rejected",
    "invalid_response",
    "unsafe_source",
    "unsupported_content",
    "unexpected",
    "budget",
    "cancelled",
    "circuit_open",
]


class ExperimentQuery(BaseModel):
    model_config = {"extra": "forbid"}
    view: Literal["overview", "reports", "benchmark", "report"] = "overview"
    days: int = Field(default=30, ge=1, le=365)
    until: AwareDatetime | None = None
    kind: ReportKind | None = None
    benchmark_id: UUID | None = None
    report_id: UUID | None = None
    group_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    limit: int = Field(default=20, ge=1, le=20)
    offset: int = Field(default=0, ge=0, le=10000)

    # 功能：按视图限制字段，禁止混选实体、用无时区/未来时间伪造观察窗口。
    @model_validator(mode="after")
    def relevant(self):
        if self.until and self.until > datetime.now(UTC):
            raise ValueError("观察窗口结束时间不能在未来")
        if (self.view == "report") != bool(self.report_id) or (self.view == "benchmark") != bool(
            self.benchmark_id
        ):
            raise ValueError("report/benchmark视图须且仅可提供各自UUID")
        if (self.kind and self.view != "reports") or (self.group_id and self.view != "benchmark"):
            raise ValueError("kind只用于报告列表，group_id只用于可靠基准视图")
        return self


# 功能：只输出有限数值，非法历史数字不当零或可比较得分。
def finite(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


# 功能：从可靠重算结果选出成绩/区间/完整度，不暴露冻结记忆、真值或将unknown算错。
def score_view(value):
    return {
        k: value.get(k)
        for k in [
            "n",
            "scored_n",
            "unknown_n",
            "complete",
            "score",
            "wilson_95",
            "calibration",
            "split_metrics",
        ]
    }


class ExperimentDashboard:
    # 功能：绑定同一只读会话和原报告仓库，不构造提供方/预算或写动作。
    def __init__(self, repo, engine, query):
        self.repo, self.s, self.engine, self.query = repo, repo.s, engine, query
        self.now = datetime.now(UTC)
        self.end = query.until.astimezone(UTC) if query.until else self.now
        self.start = self.end - timedelta(days=query.days)

    # 功能：给按创建时间筛选的窗口附真实读时刻；结算/子阶段在当前快照读，不伪装历史回放。
    def window(self):
        return {
            "window_start_utc": self.start.isoformat(),
            "window_end_utc": self.end.isoformat(),
            "read_at_utc": self.now.isoformat(),
            "days": self.query.days,
            "note": "窗口筛创建时间；结算值和实验子阶段为本次数据库快照，跨请求不是冻结历史。无记录不证明未运行或零成本。",
        }

    # 功能：对时间列构造统一闭开窗口，UTC日分组，不把客户端日期标签当真实实验时间。
    def during(self, column):
        return (column >= self.start) & (column < self.end)

    # 功能：先投影元数据/JSON大小再加载报告，超过4MiB拒绝，避免无界旧报告进入网页。
    async def report(self, report_id, kind=None):
        row = (
            await self.s.execute(
                select(
                    models.ResearchReport.kind,
                    models.ResearchReport.created_at,
                    func.octet_length(models.ResearchReport.payload.cast(String)).label("bytes"),
                ).where(models.ResearchReport.id == report_id)
            )
        ).one_or_none()
        if row is None or (kind and row.kind != kind) or row.kind not in KINDS:
            raise ValueError("实验报告不存在或类型不在查看白名单")
        observed_time(row.created_at, self.now)
        if row.bytes > 4 * 1024 * 1024:
            raise ValueError("报告超过4MiB，只能另行分域审计，不截断后计算成绩")
        return await self.s.get(models.ResearchReport, report_id)

    # 功能：限制复用F09/F11/F12读者的依赖总载荷16MiB，不为看板加载无界历史正文。
    async def dependency_budget(self, statement):
        subquery = statement.subquery()
        size = await self.s.scalar(select(func.sum(subquery.c.bytes)))
        if size and size > 16 * 1024 * 1024:
            raise ValueError("依赖报告超过16MiB只读处理上限，请按单项原审计查看")

    # 功能：按UTC日/资源汇总实际值与未结算预留，负数/非有限值分开计数，不将估算称真实账单。
    async def usage(self):
        op = models.OperationEvent
        day = func.date_trunc("day", func.timezone("UTC", op.created_at))
        valid_actual = (op.actual >= 0) & (op.actual < float("inf"))
        valid_reserved = (op.reserved >= 0) & (op.reserved < float("inf"))
        rows = (
            (
                await self.s.execute(
                    select(
                        day.label("day"),
                        op.resource,
                        func.count().label("events"),
                        func.sum(case((valid_actual, 1), else_=0)).label("known_actual_n"),
                        func.sum(case((valid_actual, op.actual), else_=0)).label("settled"),
                        func.sum(
                            case((op.actual.is_(None) & valid_reserved, op.reserved), else_=0)
                        ).label("reserved"),
                        func.sum(case((op.actual.is_(None), 1), else_=0)).label("unsettled_n"),
                        func.sum(
                            case(
                                (
                                    (op.actual.is_not(None) & ~valid_actual)
                                    | (op.actual.is_(None) & ~valid_reserved),
                                    1,
                                ),
                                else_=0,
                            )
                        ).label("invalid_n"),
                    )
                    .where(self.during(op.created_at))
                    .group_by(day, op.resource)
                    .order_by(day, op.resource)
                    .offset(self.query.offset)
                    .limit(self.query.limit + 1)
                )
            )
            .mappings()
            .all()
        )
        items = [
            {
                **dict(r),
                "day": r["day"].date().isoformat(),
                "settled": finite(r["settled"]) if r["known_actual_n"] else None,
                "reserved": finite(r["reserved"]),
            }
            for r in rows[: self.query.limit]
        ]
        return {
            "daily_resources": items,
            "next_offset": self.query.offset + self.query.limit
            if len(rows) > self.query.limit and self.query.offset + self.query.limit <= 10000
            else None,
            "scope": "按UTC日/资源分页聚合窗口全部运营记录，不强行归属某实验。estimated_usd是配置价格估算，0不证明免费；无有效actual显示null，invalid_n>0为不完整，reserved是未结算预留。",
        }

    # 功能：只汇总安全失败类别计数，不读取/展示原prompt、response或异常正文，运营成功率不当认知正确率。
    async def errors(self):
        obs = models.ModelObservation
        category = obs.metadata_json["failure_category"].astext
        safe_category = case((category.in_(SAFE_FAILURES), category), else_="other_or_unknown")
        failed = obs.metadata_json["error"].astext.is_not(None)
        rows = (
            (
                await self.s.execute(
                    select(
                        safe_category.label("category"),
                        func.count().label("calls"),
                        func.sum(case((failed, 1), else_=0)).label("failed_calls"),
                    )
                    .where(self.during(obs.created_at))
                    .group_by(safe_category)
                    .order_by(safe_category)
                )
            )
            .mappings()
            .all()
        )
        return {
            "model_calls": [dict(r) for r in rows],
            "operation_errors": [
                dict(r)
                for r in (
                    await self.s.execute(
                        select(
                            case(
                                (
                                    models.OperationEvent.details["failure_category"].astext.in_(
                                        SAFE_FAILURES
                                    ),
                                    models.OperationEvent.details["failure_category"].astext,
                                ),
                                else_="other_or_unknown",
                            ).label("category"),
                            func.count().label("events"),
                        )
                        .where(
                            self.during(models.OperationEvent.created_at),
                            models.OperationEvent.details["error"].astext.is_not(None),
                        )
                        .group_by("category")
                        .order_by("category")
                    )
                )
                .mappings()
                .all()
            ],
            "note": "观察调用与运营错误为重叠审计层，不能相加；无失败类别成功调用也在other_or_unknown。只计已保存记录，不认证平台后台或外部账单。",
        }

    # 功能：有界复用既有评估分组，日常代理分数与可靠基准隔离，缺失校准/未知版本不补成零。
    async def daily_learning(self):
        evaluation = models.Evaluation
        await self.dependency_budget(
            select(func.octet_length(evaluation.components.cast(String)).label("bytes"))
            .where(self.during(evaluation.created_at))
            .order_by(evaluation.created_at.desc(), evaluation.id.desc())
            .limit(501)
        )
        rows = (
            await self.s.scalars(
                select(models.Evaluation)
                .where(self.during(models.Evaluation.created_at))
                .order_by(models.Evaluation.created_at.desc(), models.Evaluation.id.desc())
                .limit(501)
            )
        ).all()
        days = defaultdict(list)
        invalid = 0
        for row in rows[:500]:
            if finite(row.score) is None or not 0 <= row.score <= 1:
                invalid += 1
                continue
            days[observed_time(row.created_at, self.now).date().isoformat()].append(row)
        points = []
        for day, entries in sorted(days.items()):
            for key, value in evaluation_groups(entries).items():
                points.append({"day": day, "evaluation_key": key, **value})
        return {
            "points": points[self.query.offset : self.query.offset + self.query.limit],
            "next_offset": self.query.offset + self.query.limit
            if self.query.offset + self.query.limit < len(points)
            else None,
            "truncated": len(rows) > 500,
            "invalid_scores": invalid,
            "scope": "最多最近500评估；日常代理分数不是可靠真值或成长因果证据，不与冻结基准合算。",
        }

    # 功能：显示真实学习会话时间跨度与记录日数，不填空白日或把日期跨度当30天有效实验。
    async def coverage(self):
        session = models.LearningSession
        result = (
            (
                await self.s.execute(
                    select(
                        func.min(session.created_at).label("first"),
                        func.max(session.created_at).label("last"),
                        func.count().label("sessions"),
                        func.count(
                            func.distinct(func.date(func.timezone("UTC", session.created_at)))
                        ).label("recorded_days"),
                    ).where(self.during(session.created_at))
                )
            )
            .mappings()
            .one()
        )
        span = (
            (result["last"] - result["first"]).total_seconds() / 86400 if result["first"] else None
        )
        return {
            **dict(result),
            "span_days": span,
            "thirty_day_effect_proven": False,
            "note": "记录含失败/占位等会话，非有效学习天数。无历史不模拟；跨度/记录日数不证明30天持续运行、可靠保持或能力提升。",
        }

    # 功能：投影实验目录和小字段元信息，筛选/分页有界，不把保存过的score当已重新核验成绩。
    async def listing(self):
        report = models.ResearchReport
        stmt = select(
            report.id,
            report.kind,
            report.created_at,
            report.payload["protocol"].astext.label("protocol"),
            report.payload["benchmark_id"].astext.label("benchmark_id"),
        )
        stmt = stmt.where(
            report.kind.in_([self.query.kind] if self.query.kind else KINDS),
            self.during(report.created_at),
        )
        rows = (
            (
                await self.s.execute(
                    stmt.order_by(report.created_at.desc(), report.id.desc())
                    .offset(self.query.offset)
                    .limit(self.query.limit + 1)
                )
            )
            .mappings()
            .all()
        )
        return {
            "items": [dict(r) for r in rows[: self.query.limit]],
            "next_offset": self.query.offset + self.query.limit
            if len(rows) > self.query.limit and self.query.offset + self.query.limit <= 10000
            else None,
            "note": "目录不授予成绩有效性；打开报告后重算支持的可靠协议，旧协议保持未核验。",
        }

    # 功能：校验单份可靠报告/冻结身份，真实保持另追完整基线与数据库时间，不信旧保存的保持率。
    async def reliable(self, row):
        payload = row.payload
        frozen = await self.report(UUID(payload["benchmark_id"]), "frozen_benchmark")
        suite = verify_frozen(frozen.payload)
        if suite is None:
            raise ValueError("旧模型裁判协议不能当可靠评分")
        at = observed_time(row.created_at, self.now)
        if at < observed_time(frozen.created_at, self.now):
            raise ValueError("实验早于冻结题集")
        group, conditions = condition_key(payload)
        value = recompute_run(payload, suite)
        retention = None
        if row.kind == "benchmark_run" and set(value["selected_splits"]) != set(HELD_OUT):
            raise ValueError("基线分集不完整")
        if row.kind == "benchmark_retention":
            if value["selected_splits"] != ["retention"]:
                raise ValueError("保持报告分集非法")
            baseline_id = UUID(payload["retention_comparison"]["baseline_report_id"])
            base = await self.report(baseline_id, "benchmark_run")
            if (
                base.payload["benchmark_id"] != payload["benchmark_id"]
                or condition_key(base.payload)[0] != group
            ):
                raise ValueError("保持基线题集/模型/记忆/方法不匹配")
            original = recompute_run(base.payload, suite)
            elapsed = (at - observed_time(base.created_at, self.now)).total_seconds() / 3600
            if (
                not original["complete"]
                or set(original["selected_splits"]) != set(HELD_OUT)
                or base.created_at < frozen.created_at
                or elapsed < suite.min_retention_hours
            ):
                raise ValueError("保持基线不完整或真实间隔不足")
            prior = {
                d["item_id"]
                for d in original["details"]
                if d["split"] == "retention" and d["correct"]
            }
            retested = [
                d for d in value["details"] if d["item_id"] in prior and d["status"] == "scored"
            ]
            retention = {
                "baseline_report_id": str(base.id),
                "elapsed_hours": elapsed,
                "previously_correct_n": len(prior),
                "retested_n": len(retested),
                "retention_rate": sum(d["correct"] for d in retested) / len(prior)
                if prior and len(retested) == len(prior)
                else None,
                "delta": value["score"]
                - summarize([d for d in original["details"] if d["split"] == "retention"])["score"]
                if value["complete"]
                else None,
            }
        details = value["details"][self.query.offset : self.query.offset + self.query.limit]
        return {
            "status": "recomputed_reliable",
            "benchmark_id": str(frozen.id),
            "group_id": group,
            "conditions": conditions,
            "metrics": score_view(value),
            "retention": retention,
            "items": details,
            "total_items": len(value["details"]),
            "next_offset": self.query.offset + self.query.limit
            if self.query.offset + self.query.limit < len(value["details"])
            else None,
            "note": "准确率/校准/区间来自固定规则复算，unknown不当错误；人工题库真实性/语义独立/提供方版本仍未认证。含题面与答案，仅本机授权审计。",
        }

    # 功能：重算四组迁移原答卷并复查固定记忆/训练摘要与组内模型身份，不把保存的增益声明直接当成绩。
    async def migration(self, row):
        payload = row.payload
        frozen = await self.report(UUID(payload["benchmark_id"]), "frozen_benchmark")
        suite = verify_frozen(frozen.payload)
        if (
            suite is None
            or row.created_at < frozen.created_at
            or payload["benchmark_sha256"] != frozen.payload["sha256"]
        ):
            raise ValueError("迁移报告不是有效可靠冻结条件")
        if (
            digest(payload["memory_snapshot"]) != payload["memory_sha256"]
            or digest(payload["training_snapshot"]) != payload["training_sha256"]
            or set(payload["groups"]) != {"A1", "A2", "B1", "B2"}
        ):
            raise ValueError("迁移记忆/训练或四组不完整")
        groups, details = {}, []
        for label in ("A1", "A2", "B1", "B2"):
            value = {
                **payload["groups"][label],
                "memory_snapshot": payload["memory_snapshot"] if label.endswith("2") else [],
                "skill_snapshot": None,
                "use_memory": label.endswith("2"),
                "training_snapshot": payload["training_snapshot"],
                "training_sha256": payload["training_sha256"],
            }
            group, conditions = condition_key(value)
            result = recompute_run(value, suite)
            if set(result["selected_splits"]) != set(HELD_OUT):
                raise ValueError("迁移分集不完整")
            groups[label] = {
                "label": label,
                "group_id": group,
                "conditions": conditions,
                **score_view(result),
            }
            details.extend({"group": label, **d} for d in result["details"])
        if any(
            groups[a]["conditions"]["model_identity"] != groups[b]["conditions"]["model_identity"]
            for a, b in [("A1", "A2"), ("B1", "B2")]
        ):
            raise ValueError("同模型的无记忆/有记忆组身份不一致")
        return {
            "status": "recomputed_reliable_migration",
            "groups": list(groups.values()),
            "gains": {
                name: groups[a]["score"] - groups[b]["score"]
                if groups[a]["complete"] and groups[b]["complete"]
                else None
                for name, a, b in [
                    ("old_memory_gain", "A2", "A1"),
                    ("new_memory_gain", "B2", "B1"),
                    ("base_model_gain", "B1", "A1"),
                ]
            },
            "items": details[self.query.offset : self.query.offset + self.query.limit],
            "next_offset": self.query.offset + self.query.limit
            if self.query.offset + self.query.limit < len(details)
            else None,
            "note": "A1/A2旧模型无/有记忆，B1/B2新模型无/有记忆；仅固定对照差值，不自动换模型或作因果成长证明。",
        }

    # 功能：按同冻结版本和精确条件组展示所有有界历史，不取最好分，不跨记忆/模型计算总增长。
    async def benchmark(self):
        frozen = await self.report(self.query.benchmark_id, "frozen_benchmark")
        suite = verify_frozen(frozen.payload)
        if suite is None:
            raise ValueError("此视图仅支持可靠冻结基准，旧协议请从报告审计查看")
        report = models.ResearchReport
        await self.dependency_budget(
            select(func.octet_length(report.payload.cast(String)).label("bytes"))
            .where(
                report.kind.in_(["benchmark_run", "benchmark_retention"]),
                report.payload["benchmark_id"].astext == str(frozen.id),
            )
            .order_by(report.created_at.desc(), report.id.desc())
            .limit(101)
        )
        runs, rejected, truncated = await historical_runs(self.repo, frozen, suite, self.now)
        groups, points = {}, []
        for run in runs:
            if not self.start <= run["at"] < self.end:
                continue
            groups.setdefault(
                run["group_id"],
                {"group_id": run["group_id"], "conditions": run["conditions"], "reports": 0},
            )["reports"] += 1
            if self.query.group_id and run["group_id"] != self.query.group_id:
                continue
            points.append(
                {
                    "report_id": run["id"],
                    "kind": run["kind"],
                    "at": run["at"].isoformat(),
                    "group_id": run["group_id"],
                    **score_view(run["result"]),
                    "baseline_report_id": run.get("baseline", {}).get("id"),
                }
            )
        return {
            "benchmark_id": str(frozen.id),
            "suite_id": suite.suite_id,
            "suite_version": suite.suite_version,
            "groups": list(groups.values()),
            "selection_required": len(groups) > 1 and not self.query.group_id,
            "items": points[self.query.offset : self.query.offset + self.query.limit],
            "next_offset": self.query.offset + self.query.limit
            if self.query.offset + self.query.limit < len(points)
            else None,
            "history_truncated": truncated,
            "rejected_reports": rejected,
            "note": "复用F09最近100报告/真实保持基线核查，再按窗口筛；截断可能遗漏旧窗口/基线，不能当全历史。所有组单列，无跨组总分或因果增长。保持率见逐报告详情。",
        }

    # 功能：复用F03/F11/F12只读详情，其他旧报告只列声明/审计引用，不伪造可靠评分或触发会诊。
    async def detail(self):
        row = await self.report(self.query.report_id)
        try:
            if row.kind in {"benchmark_run", "benchmark_retention"}:
                data = await self.reliable(row)
            elif row.kind == "model_migration":
                data = await self.migration(row)
            elif row.kind in {"skill_method_experiment", "cognitive_review"}:
                report = models.ResearchReport
                prefix = str(row.id) + ":"
                kinds = (
                    ["skill_method_attempt", "skill_method_result"]
                    if row.kind == "skill_method_experiment"
                    else ["cognitive_review_attempt", "cognitive_review_result"]
                )
                await self.dependency_budget(
                    select(func.octet_length(report.payload.cast(String)).label("bytes")).where(
                        report.kind.in_(kinds), report.report_key.startswith(prefix)
                    )
                )
                if row.kind == "skill_method_experiment":
                    if self.query.offset > 1000:
                        raise ValueError("方法实验offset最多1000，不静默改变页码")
                    frozen = await self.report(
                        UUID(row.payload["spec"]["benchmark_id"]), "frozen_benchmark"
                    )
                data = (
                    await SkillMethods(self.repo, self.engine).show(
                        MethodQuery(
                            experiment_id=row.id,
                            limit=self.query.limit,
                            offset=self.query.offset,
                        )
                    )
                    if row.kind == "skill_method_experiment"
                    else await CognitiveReview(self.repo, self.engine).status(row.id)
                )
                if row.kind == "skill_method_experiment":
                    suite = verify_frozen(frozen.payload)
                    items = {item.id: item for item in suite.items}
                    for entry in data["items"]:
                        item = items[entry["task"]["item_id"]]
                        entry.update(item_id=item.id, question=item.question, split=item.split)
                        result = entry.get("result")
                        if result and result["status"] == "scored":
                            answer = ExamAnswer.model_validate(result["answer"])
                            entry.update(
                                answer=answer.model_dump(mode="json"),
                                **grade_answer(item, answer.answer),
                            )
                    data = {
                        "status": "recomputed_method_ab",
                        "summary": data["summary"],
                        "items": data["items"],
                        "next_offset": data["next_offset"],
                        "note": "复用F12固定重复/全部未知与退化重算，不自动采用方法；冻结记忆/训练见原审计。",
                    }
                else:
                    data = {
                        "status": "cognitive_observations_only",
                        "complete": data["complete"],
                        "partial": data["partial"],
                        "stages": data["stages"],
                        "argument_graph": data["argument_graph"],
                        "note": data["note"],
                    }
            else:
                keys = [
                    "protocol",
                    "generated_at",
                    "suite_id",
                    "suite_version",
                    "benchmark_id",
                    "baseline_id",
                    "comparison_key",
                    "old_memory_gain",
                    "new_memory_gain",
                    "base_model_gain",
                    "group_id",
                    "history_truncated",
                    "knowledge_truncated",
                    "note",
                ]
                data = {
                    "status": "saved_snapshot_not_recomputed",
                    "declared_summary": {k: row.payload[k] for k in keys if k in row.payload},
                    "note": "只读历史声明，不把保存的增益/画像/重评分算新独立实验；完整内容从原报告审计追溯。",
                }
                if row.kind == "mastery_profile":
                    topics = row.payload.get("topics", [])
                    data["items"] = [
                        {
                            k: t.get(k)
                            for k in ["topic", "status", "action", "gaps", "mastery_score"]
                        }
                        for t in topics[self.query.offset : self.query.offset + self.query.limit]
                    ]
                    data["next_offset"] = (
                        self.query.offset + self.query.limit
                        if self.query.offset + self.query.limit < len(topics)
                        else None
                    )
        except (ValueError, KeyError, TypeError, AttributeError):
            data = {
                "status": "unverified_or_invalid",
                "note": "协议/条件/摘要/基线或旧记录不满足重算要求，未知不当错/零分；从原审计核查。",
            }
        return {
            "report_id": str(row.id),
            "kind": row.kind,
            "created_at": row.created_at.isoformat(),
            "audit_reference": {"kind": "reports", "id": str(row.id)},
            "data": data,
        }

    # 功能：组合固定四视图，所有输出为当前只读请求结果，不保存progress或改变认知/预算。
    async def show(self):
        if self.query.view == "overview":
            data = {
                "coverage": await self.coverage(),
                "usage": await self.usage(),
                "errors": await self.errors(),
                "daily_learning": await self.daily_learning(),
            }
        elif self.query.view == "reports":
            data = await self.listing()
        elif self.query.view == "benchmark":
            data = await self.benchmark()
        else:
            data = await self.detail()
        return {"view": self.query.view, "window": self.window(), "data": data}


# 功能：强制独立只读重复读快照/SQL5秒/总30秒，无迁移、模型构造、控制器写锁或报告保存。
async def experiment_dashboard(engine, query):
    try:
        async with (
            async_sessionmaker(engine, expire_on_commit=False)() as session,
            asyncio.timeout(30),
        ):
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            await session.execute(text("SET LOCAL statement_timeout = '5s'"))
            return await ExperimentDashboard(Repository(session), engine, query).show()
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise DeliveryError(
            str(exc)
            if type(exc) is ValueError
            else "实验参数/旧记录结构无效，校验输入和外部异常正文不展示。"
        ) from None
