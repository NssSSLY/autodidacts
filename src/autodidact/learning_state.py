# 文件职责：处理非晋升规则的运行持久状态：恢复、检查点、报告、模型统计和技能使用结果。
"""Operational persistence, separated from epistemic promotion rules."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import exists, func, select, text

from autodidact import models
from autodidact.config import agent_config
from autodidact.enums import GoalStatus


class LearningState:
    # 功能：控制器持锁时标记中断；新协议保留原尝试续跑，旧协议结束尝试并增加有界重试。
    async def recover_interrupted(self):
        """Called only with database-wide controller ownership.

        Resume durable attempts in place; retain the legacy retry path only for
        attempts that predate the durable protocol.
        """
        states = ["planned", "researching", "synthesizing", "testing", "reflecting"]
        goals = (
            await self.s.scalars(select(models.Goal).where(models.Goal.status.in_(states)))
        ).all()
        for goal in goals:
            sessions = (
                await self.s.scalars(
                    select(models.LearningSession).where(
                        models.LearningSession.goal_id == goal.id,
                        models.LearningSession.completed_at.is_(None),
                    )
                )
            ).all()
            durable = [
                a for a in sessions if (a.plan or {}).get("resume_protocol") == "durable_replay_v1"
            ]
            if durable:
                for attempt in durable:
                    attempt.result = {
                        **(attempt.result or {}),
                        "recovery": "resume_saved_work",
                        "interrupted_stage": goal.status,
                    }
                continue
            for attempt in sessions:
                attempt.result = {
                    **(attempt.result or {}),
                    "interrupted_stage": goal.status,
                    "recovery": "retry_preserving_evidence",
                }
                attempt.success = False
                attempt.completed_at = datetime.now(UTC)
            goal.metadata_json = {
                **(goal.metadata_json or {}),
                "last_interrupted_stage": goal.status,
            }
            goal.retry_count += 1
            goal.status = (
                GoalStatus.BLOCKED
                if goal.retry_count >= agent_config().learning.max_retry
                else GoalStatus.FAILED
            )
            if goal.status == GoalStatus.BLOCKED:
                goal.completed_at = datetime.now(UTC)
        await self.s.commit()
        return len(goals)

    # 功能：按创建时间选择最早未完成且未阻断的 durable_replay_v1 会话。
    async def resumable_attempt(self):
        return await self.s.scalar(
            select(models.LearningSession)
            .join(models.Goal, models.Goal.id == models.LearningSession.goal_id)
            .where(
                models.LearningSession.completed_at.is_(None),
                models.LearningSession.plan["resume_protocol"].astext == "durable_replay_v1",
                models.Goal.status != "blocked",
            )
            .order_by(models.LearningSession.created_at)
            .limit(1)
        )

    # 功能：合并并提交阶段名与载荷，供崩溃恢复和人工排查使用。
    async def checkpoint(self, attempt, stage: str, **payload):
        previous = attempt.result or {}
        stages = list(previous.get("checkpoints", []))
        if not stages or stages[-1]["stage"] != stage:
            stages.append({"stage": stage, "at": datetime.now(UTC).isoformat()})
        attempt.result = {**previous, **payload, "checkpoint": stage, "checkpoints": stages}
        await self.s.commit()

    # 功能：返回 supported/verified 且无开放争议的记忆快照，供工作台和实验使用。
    async def accepted_memory(self, limit=200):
        has_dispute = exists(
            select(models.Dispute.id).where(
                models.Dispute.belief_id == models.Belief.id,
                models.Dispute.status.in_(["open", "investigating", "unresolved"]),
            )
        )
        rows = (
            await self.s.scalars(
                select(models.Belief)
                .where(
                    models.Belief.status.in_(["supported", "verified"]),
                    ~has_dispute,
                )
                .order_by(models.Belief.confidence.desc(), models.Belief.updated_at.desc())
                .limit(limit)
            )
        ).all()
        return [
            {
                "id": str(b.id),
                "topic": b.topic,
                "statement": b.statement,
                "status": b.status,
                "confidence": b.confidence,
            }
            for b in rows
        ]

    # 功能：按报告类型和稳定键查找已有报告，支持日整合与基准幂等。
    async def get_report(self, kind, key):
        return await self.s.scalar(
            select(models.ResearchReport).where(
                models.ResearchReport.kind == kind,
                models.ResearchReport.report_key == key,
            )
        )

    # 功能：按类型/键复用或保存报告，未给键时生成独立报告标识。
    async def save_report(self, kind, payload, key=None):
        key = key or str(uuid4())
        # Makes daily snapshots safe across repeated commands and interrupted processes.
        await self.s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 718204613})
        existing = await self.get_report(kind, key)
        if existing:
            await self.s.commit()
            return existing
        report = models.ResearchReport(kind=kind, report_key=key, payload=payload)
        self.s.add(report)
        await self.s.commit()
        await self.s.refresh(report)
        return report

    # 功能：汇总模型观察、错误和评估/基准记录，更新经验统计而非宣布模型绝对可信。
    async def refresh_model_profiles(self):
        observations = (await self.s.scalars(select(models.ModelObservation))).all()
        evaluations = (await self.s.scalars(select(models.Evaluation))).all()
        groups = {}
        for obs in observations:
            key = (obs.provider, obs.displayed_model or "unknown")
            g = groups.setdefault(key, {"calls": 0, "errors": {}, "domains": {}})
            g["calls"] += 1
            error = (obs.metadata_json or {}).get("error")
            if error:
                g["errors"][error] = g["errors"].get(error, 0) + 1
        for evaluation in evaluations:
            audit = (evaluation.components or {}).get("audit", {})
            if audit.get("protocol") != "closed_book_v1":
                continue
            key = (
                audit.get("learner_provider", "openai_compatible"),
                audit.get("learner_model", "unknown"),
            )
            g = groups.setdefault(key, {"calls": 0, "errors": {}, "domains": {}})
            goal = await self.s.get(models.Goal, evaluation.goal_id)
            domain = (goal.title if goal else "unknown")[:200]
            g["domains"].setdefault(domain, []).append(evaluation.score)
        reports = (
            await self.s.scalars(
                select(models.ResearchReport)
                .where(
                    models.ResearchReport.kind.in_(
                        ["benchmark_run", "model_migration", "scheduled_benchmark"]
                    )
                )
                .order_by(models.ResearchReport.created_at)
            )
        ).all()
        for report in reports:
            payload = report.payload
            if report.kind == "model_migration":
                runs = [payload["groups"][label] for label in ["A1", "B1"]]
            elif report.kind == "scheduled_benchmark":
                runs = [payload["base"]]
            else:
                runs = [] if payload.get("memory_snapshot") else [payload]
            for run in runs:
                key = (run.get("provider", "unknown"), run.get("model", "unknown"))
                g = groups.setdefault(key, {"calls": 0, "errors": {}, "domains": {}})
                g["benchmark_score"] = run["score"]
                for detail in run.get("details", []):
                    domain = f"frozen_base:{detail.get('category', 'general')}"
                    g["domains"].setdefault(domain, []).append(detail["score"])
        for (provider, name), group in groups.items():
            profile = await self.s.scalar(
                select(models.ModelProfile)
                .where(
                    models.ModelProfile.provider == provider,
                    models.ModelProfile.model_name == name,
                )
                .limit(1)
            )
            if profile is None:
                profile = models.ModelProfile(provider=provider, model_name=name)
                self.s.add(profile)
            profile.sample_count = sum(len(v) for v in group["domains"].values())
            if "benchmark_score" in group:
                profile.benchmark_score = group["benchmark_score"]
            profile.domain_scores = {
                k: {"mean": sum(v) / len(v), "samples": len(v)} for k, v in group["domains"].items()
            }
            profile.error_profile = {
                "calls": group["calls"],
                "failures": group["errors"],
                "score_source": "闭卷核源与冻结基准（含模型裁判），未替代人工真值",
            }
        await self.s.commit()
        return len(groups)

    # 功能：统计 UTC 当天非人工目标，计算剩余自主目标配额。
    async def goal_quota_remaining(self):
        now = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        generated = await self.s.scalar(
            select(func.count())
            .select_from(models.Goal)
            .where(
                models.Goal.source != "human",
                models.Goal.created_at >= now,
            )
        )
        return max(0, agent_config().learning.daily_goal_limit - generated)

    # 功能：读取开放、调查中或未解决争议，按时间排序并限制数量。
    async def open_disputes(self, limit=20):
        return list(
            (
                await self.s.scalars(
                    select(models.Dispute)
                    .where(models.Dispute.status.in_(["open", "investigating", "unresolved"]))
                    .order_by(models.Dispute.created_at)
                    .limit(limit)
                )
            ).all()
        )

    # 功能：按触发文本和置信度选择已经独立验证的技能，候选技能不进入自动规划。
    async def selected_skills(self, title, limit=2):
        rows = (await self.s.scalars(select(models.Skill))).all()
        terms = set(title.casefold().split())
        eligible = [
            s
            for s in rows
            if (s.metadata_json or {}).get("status") == "validated"
            and (
                terms.intersection((s.trigger_condition or "").casefold().split())
                or (s.trigger_condition and s.trigger_condition in title)
            )
        ]
        return sorted(eligible, key=lambda s: s.confidence, reverse=True)[:limit]

    # 功能：拼接指定 Claim/Source 的 supported 原文片段，供决议证据保留真实引用。
    async def supported_excerpt(self, claim_id, source_id):
        passages = (
            await self.s.scalars(
                select(models.ClaimEvidence.excerpt).where(
                    models.ClaimEvidence.claim_id == claim_id,
                    models.ClaimEvidence.source_id == source_id,
                    models.ClaimEvidence.status == "supported",
                )
            )
        ).all()
        return "\n".join(passages)

    # 功能：更新本次使用技能的成功/失败计数与经验置信度，通过会话标记防止重复计数。
    async def record_skill_outcome(self, skill_ids, passed, attempt=None):
        if attempt and (attempt.result or {}).get("skills_recorded"):
            return
        for skill_id in skill_ids:
            skill = await self.s.get(models.Skill, skill_id)
            if skill:
                skill.success_count += int(passed)
                skill.failure_count += int(not passed)
                skill.confidence = (skill.success_count + 1) / (
                    skill.success_count + skill.failure_count + 2
                )
        if attempt:
            attempt.result = {**(attempt.result or {}), "skills_recorded": True}
        await self.s.commit()
