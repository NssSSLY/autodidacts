# 文件职责：从冻结可靠题集/历史答卷/当前认知证据派生主题掌握与缺口，显式保存快照或排学习目标；不调用模型或修改信念。
from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from statistics import mean
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.config import agent_config, runtime_settings
from autodidact.delivery import DeliveryError
from autodidact.goals.scorer import goal_score
from autodidact.knowledge.sources import independent_source_representatives
from autodidact.learning.ground_truth import HELD_OUT, digest, summarize
from autodidact.learning.reliable_evaluation import load_frozen, recompute_run, verify_frozen
from autodidact.runtime import controller_lock
from autodidact.schemas import CandidateGoal

MASTERY_PROTOCOL = "topic_mastery_v1"
POLICY = {
    "score_threshold": 0.8,
    "calibration_threshold": 0.8,
    "minimum_unique_items": 5,
    "minimum_test_runs": 2,
    "review_after_days": 30,
    "decline_threshold": 0.2,
    "history_limit": 100,
    "belief_limit": 500,
    "evidence_limit": 1000,
}


# 功能：只接受带时区且不在未来的数据库时间，不把缺失时间猜作现在或制造保持间隔。
def observed_time(value, now):
    if not isinstance(value, datetime) or value.tzinfo is None or value > now:
        raise ValueError("历史观察时间缺失、无时区或位于未来")
    return value.astimezone(UTC)


# 功能：固定模型/端点/采样、记忆模式/快照及研究方法分组；不同组不计算总掌握度或趋势。
def condition_key(payload):
    identity = payload.get("model_identity")
    if not isinstance(identity, dict) or set(identity) != {
        "provider",
        "model",
        "endpoint_sha256",
        "sampling_sha256",
    }:
        raise ValueError("模型身份不完整")
    if any(not isinstance(v, str) or not v.strip() for v in identity.values()):
        raise ValueError("模型身份未知")
    if identity["provider"].lower() in {"mock", "unknown", "legacy"}:
        raise ValueError("演示/未知模型不作为领域掌握证据")
    for key in ("endpoint_sha256", "sampling_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", identity[key]):
            raise ValueError("模型身份摘要不合法")
    if type(payload.get("use_memory")) is not bool:
        raise ValueError("记忆模式未知")
    if not isinstance(payload.get("memory_snapshot"), list):
        raise TypeError("记忆快照格式未知")
    if not payload["use_memory"] and payload["memory_snapshot"]:
        raise ValueError("无记忆模式与快照不一致")
    conditions = {
        "model_identity": identity,
        "use_memory": payload["use_memory"],
        "memory_sha256": payload["memory_sha256"],
        "skill_sha256": payload["skill_sha256"],
    }
    return digest(conditions), conditions


# 功能：有界读取同一冻结版本的可靠运行/保持报告并重算逐题分数，不读取模型裁判分数或重复计重评分报告。
async def historical_runs(repo, frozen, suite, now):
    rows = list(
        (
            await repo.s.scalars(
                select(models.ResearchReport)
                .where(
                    models.ResearchReport.kind.in_(["benchmark_run", "benchmark_retention"]),
                    models.ResearchReport.payload["benchmark_id"].astext == str(frozen.id),
                )
                .order_by(models.ResearchReport.created_at.desc(), models.ResearchReport.id.desc())
                .limit(POLICY["history_limit"] + 1)
            )
        ).all()
    )
    truncated = len(rows) > POLICY["history_limit"]
    valid, rejected = {}, []
    for row in rows[: POLICY["history_limit"]]:
        try:
            if len(json.dumps(row.payload, ensure_ascii=False).encode()) > 4 * 1024 * 1024:
                raise ValueError("报告超出4MiB处理上限")
            at = observed_time(row.created_at, now)
            if at < observed_time(frozen.created_at, now):
                raise ValueError("历史观察早于冻结范围")
            if (
                not isinstance(row.payload.get("training_snapshot"), list)
                or "training_sha256" not in row.payload
            ):
                raise ValueError("缺实际训练快照")
            result = recompute_run(row.payload, suite)
            group, conditions = condition_key(row.payload)
            if row.kind == "benchmark_run" and set(result["selected_splits"]) != set(HELD_OUT):
                raise ValueError("基线缺少完整held-out分集")
            if row.kind == "benchmark_retention" and result["selected_splits"] != ["retention"]:
                raise ValueError("保持报告分集不合法")
            valid[str(row.id)] = {
                "id": str(row.id),
                "kind": row.kind,
                "at": at,
                "group_id": group,
                "conditions": conditions,
                "result": result,
                "payload": row.payload,
            }
        except (ValueError, TypeError, KeyError, AttributeError):
            rejected.append({"report_id": str(row.id), "reason": "invalid_history"})
    # 保持证据必须追到窗口内完整基线并以实际数据库时间重算，缺基线不能静默当真实保持。
    for key, run in list(valid.items()):
        if run["kind"] != "benchmark_retention":
            continue
        comparison = run["payload"].get("retention_comparison", {})
        baseline_id = comparison.get("baseline_report_id") if isinstance(comparison, dict) else None
        base = valid.get(baseline_id) if isinstance(baseline_id, str) else None
        if (
            base is None
            or base["kind"] != "benchmark_run"
            or not base["result"]["complete"]
            or base["group_id"] != run["group_id"]
            or (run["at"] - base["at"]).total_seconds() < suite.min_retention_hours * 3600
        ):
            rejected.append({"report_id": key, "reason": "unverified_retention_baseline"})
            del valid[key]
        else:
            run["baseline"] = base
    return sorted(valid.values(), key=lambda r: (r["at"], r["id"])), rejected, truncated


# 功能：当前覆盖只用active信念，归档冲突/反对仍保留风险计数，防通过归档制造掌握提升。
async def knowledge_context(repo, topics):
    beliefs = list(
        (
            await repo.s.scalars(
                select(models.Belief)
                .where(models.Belief.topic.in_(topics))
                .order_by(models.Belief.id)
                .limit(POLICY["belief_limit"] + 1)
            )
        ).all()
    )
    truncated = len(beliefs) > POLICY["belief_limit"]
    beliefs = beliefs[: POLICY["belief_limit"]]
    by_id = {b.id: b for b in beliefs}
    if not beliefs:
        return {}, False
    disputed = set(
        (
            await repo.s.scalars(
                select(models.Dispute.belief_id)
                .where(
                    models.Dispute.belief_id.in_(by_id),
                    models.Dispute.status.in_(["open", "investigating", "unresolved"]),
                )
                .distinct()
            )
        ).all()
    )
    edges = list(
        (
            await repo.s.execute(
                select(models.Evidence, models.Source)
                .outerjoin(models.Source, models.Source.id == models.Evidence.source_id)
                .where(
                    models.Evidence.belief_id.in_(by_id),
                    models.Evidence.stance.in_(["support", "attack"]),
                )
                .order_by(models.Evidence.id)
                .limit(POLICY["evidence_limit"] + 1)
            )
        ).all()
    )
    truncated |= len(edges) > POLICY["evidence_limit"]
    sources, support, support_basis = {}, defaultdict(set), defaultdict(list)
    attacks, attack_basis = defaultdict(set), defaultdict(list)
    cfg = agent_config().learning
    for edge, source in edges[: POLICY["evidence_limit"]]:
        if (
            source is not None
            and edge.model_observation_id is None
            and edge.evidence_level > 0
            and (
                edge.stance == "attack"
                or edge.evidence_level >= cfg.min_evidence_level_for_verified_belief
            )
            and edge.excerpt
            and edge.excerpt in (source.extracted_text or "")
        ):
            sources[source.id] = source
            targets = attacks if edge.stance == "attack" else support
            basis = attack_basis if edge.stance == "attack" else support_basis
            targets[edge.belief_id].add(str(source.id))
            basis[edge.belief_id].append(
                {
                    "evidence_id": str(edge.id),
                    "source_id": str(source.id),
                    "excerpt_sha256": digest(edge.excerpt),
                    "evidence_level": edge.evidence_level,
                    "assessment_sha256": digest(edge.assessment or {}),
                }
            )
    records = await repo.lineage.evidence_sources(list(sources.values())) if sources else []
    attacked = {
        belief_id
        for belief_id, ids in attacks.items()
        if any(
            r.source_id in ids and r.evidence_level > 0 and 0 < r.credibility_score <= 1
            for r in records
        )
    }
    results = {}
    for topic in topics:
        rows = [
            b for b in beliefs if b.topic == topic and b.status not in {"retracted", "weakened"}
        ]
        accepted = [
            b
            for b in rows
            if b.status in {"supported", "verified"}
            and b.id not in disputed
            and b.lifecycle_state == "active"
        ]
        qualified = []
        for belief in accepted:
            representatives = independent_source_representatives(
                [
                    r
                    for r in records
                    if r.source_id in support[belief.id]
                    and r.evidence_level >= cfg.min_evidence_level_for_verified_belief
                    and r.evidence_level > 0
                    and 0 < r.credibility_score <= 1
                ]
            )
            if len(representatives) >= cfg.min_independent_sources_for_verified_belief:
                qualified.append(str(belief.id))
        representatives = independent_source_representatives(
            [
                r
                for r in records
                if any(r.source_id in support[b.id] for b in accepted)
                and r.evidence_level > 0
                and 0 < r.credibility_score <= 1
            ]
        )
        dispute_n = sum(b.id in disputed or b.status in {"disputed", "unresolved"} for b in rows)
        conflict_n = sum(
            b.id in disputed or b.id in attacked or b.status in {"disputed", "unresolved"}
            for b in rows
        )
        results[topic] = {
            "active_beliefs": sum(b.lifecycle_state == "active" for b in rows),
            "retained_beliefs": len(rows),
            "archived_beliefs": sum(b.lifecycle_state == "archived" for b in rows),
            "accepted_beliefs": len(accepted),
            "qualified_beliefs": len(qualified),
            "qualified_belief_ids": qualified,
            "open_dispute_beliefs": dispute_n,
            "negative_evidence_beliefs": sum(b.id in attacked for b in rows),
            "contradiction_rate": conflict_n / len(rows) if rows else None,
            "evidence_quality": mean(
                r.evidence_level / 5 * r.credibility_score for r in representatives
            )
            if representatives
            else None,
            "source_ids": sorted(r.source_id for r in representatives),
            "source_basis": [
                {
                    "source_id": r.source_id,
                    "stored_content_hash": r.content_hash,
                    "observed_text_sha256": digest(sources[UUID(r.source_id)].extracted_text or ""),
                    "effective_level": r.evidence_level,
                    "credibility": r.credibility_score if 0 <= r.credibility_score <= 1 else None,
                    "lineage_sha256": digest(sorted(r.dependency_keys)),
                }
                for r in sorted(records, key=lambda r: r.source_id)
                if any(r.source_id in support[b.id] or r.source_id in attacks[b.id] for b in rows)
            ],
            "belief_basis": [
                {
                    "belief_id": str(b.id),
                    "statement_sha256": digest(b.statement),
                    "status": b.status,
                    "lifecycle_state": b.lifecycle_state,
                    "lifecycle_version": b.lifecycle_version,
                    "has_open_dispute": b.id in disputed,
                }
                for b in rows
            ],
            "support_basis": [entry for b in accepted for entry in support_basis[b.id]],
            "attack_basis": [entry for b in rows for entry in attack_basis[b.id]],
            "complete": not truncated,
            "note": "只用active支持计算当前覆盖，归档的冲突/反对仍保留风险；不重写历史答卷或把归档当能力提升。",
        }
    return results, truncated


# 功能：同一主题同一分集保留最新观察及有限趋势，不把重复测同题当更多独立样本，unknown覆盖旧成功。
def topic_observations(topic, suite, runs):
    items = [i for i in suite.items if i.category == topic and i.split in HELD_OUT]
    latest, history, delayed = {}, [], None
    for run in runs:
        details = [d for d in run["result"]["details"] if d["category"] == topic]
        if not details:
            continue
        for split in {d["split"] for d in details}:
            entries = [d for d in details if d["split"] == split]
            latest[split] = {
                "report_id": run["id"],
                "at": run["at"].isoformat(),
                **summarize(entries),
            }
        if run["kind"] == "benchmark_run":
            test = [d for d in details if d["split"] == "test"]
            if test:
                history.append(
                    {"report_id": run["id"], "at": run["at"].isoformat(), **summarize(test)}
                )
        elif "baseline" in run:
            base = [
                d
                for d in run["baseline"]["result"]["details"]
                if d["category"] == topic and d["split"] == "retention"
            ]
            previous = {d["item_id"]: d for d in base if d["correct"]}
            retested = [d for d in details if d["status"] == "scored" and d["item_id"] in previous]
            delayed = {
                "report_id": run["id"],
                "baseline_report_id": run["baseline"]["id"],
                "at": run["at"].isoformat(),
                "elapsed_hours": (run["at"] - run["baseline"]["at"]).total_seconds() / 3600,
                **summarize(details),
                "previously_correct_n": len(previous),
                "retention_rate": sum(d["correct"] for d in retested) / len(previous)
                if previous and len(retested) == len(previous)
                else None,
            }
    current_entries = []
    # 从每分集最新报告取规范条目，分集间不混用不同版本/模型/记忆条件。
    by_id = {r["id"]: r for r in runs}
    for split, summary in latest.items():
        current_entries += [
            d
            for d in by_id[summary["report_id"]]["result"]["details"]
            if d["category"] == topic and d["split"] == split
        ]
    stats = summarize(current_entries)
    known = sum(d["status"] == "scored" for d in current_entries)
    recent = history[-2:]
    delta = (
        recent[-1]["score"] - recent[0]["score"]
        if len(recent) == 2 and all(p["complete"] for p in recent)
        else None
    )
    return {
        "declared_items": len(items),
        "scored_unique_items": known,
        "coverage": known / len(items) if items else None,
        "unknown_n": stats["unknown_n"],
        "calibration": stats["calibration"],
        "by_split": latest,
        "delayed_retention": delayed,
        "test_runs": len(history),
        "complete_test_runs": sum(p["complete"] for p in history),
        "trend": {"split": "test", "delta": delta, "points": history[-12:]},
        "latest_at": max((p["at"] for p in latest.values()), default=None),
        "report_ids": sorted({p["report_id"] for p in latest.values()}),
    }


# 功能：用透明门槛派生暂定掌握/退化/待复习/未知及动作；没有完整证据绝不输出掌握百分比。
def assess_topic(topic, observations, knowledge, incomplete, now):
    gaps = []
    splits = observations["by_split"]
    delayed = observations["delayed_retention"]
    for split in HELD_OUT:
        point = splits.get(split)
        if point is None:
            gaps.append(f"{split}_unassessed")
        elif not point["complete"]:
            gaps.append(f"{split}_unknown")
        elif point["score"] < POLICY["score_threshold"]:
            gaps.append(f"{split}_weak")
    if delayed is None or not delayed["complete"]:
        gaps.append("delayed_retention_missing")
    elif delayed["retention_rate"] is None:
        gaps.append("delayed_retention_not_comparable")
    elif delayed["score"] < POLICY["score_threshold"] or (
        delayed["retention_rate"] is not None
        and delayed["retention_rate"] < POLICY["score_threshold"]
    ):
        gaps.append("delayed_retention_weak")
    if observations["scored_unique_items"] < POLICY["minimum_unique_items"]:
        gaps.append("too_few_unique_items")
    if observations["complete_test_runs"] < POLICY["minimum_test_runs"]:
        gaps.append("too_few_runs")
    if (
        observations["calibration"] is None
        or observations["calibration"] < POLICY["calibration_threshold"]
    ):
        gaps.append("calibration_insufficient")
    if not knowledge.get("accepted_beliefs") or knowledge.get("qualified_beliefs") != knowledge.get(
        "accepted_beliefs"
    ):
        gaps.append("knowledge_support_insufficient")
    if knowledge.get("open_dispute_beliefs", 0):
        gaps.append("open_disputes")
    if knowledge.get("negative_evidence_beliefs", 0):
        gaps.append("negative_evidence")
    dates = [p["at"] for p in splits.values()] + ([delayed["at"]] if delayed else [])
    stale = any(
        (now - datetime.fromisoformat(at)).total_seconds() > POLICY["review_after_days"] * 86400
        for at in dates
    )
    if stale:
        gaps.append("stale_observation")
    delta = observations["trend"]["delta"]
    declined = delta is not None and delta <= -POLICY["decline_threshold"]
    if declined:
        gaps.append("comparable_test_decline")
    if incomplete:
        gaps.append("bounded_input_incomplete")
    weak = any(g.endswith("_weak") for g in gaps)
    if not splits:
        status = "unassessed"
    elif incomplete or observations["unknown_n"] or any(g.endswith("_unassessed") for g in gaps):
        status = "insufficient_evidence"
    elif declined:
        status = "degraded"
    elif (
        stale
        or knowledge.get("open_dispute_beliefs", 0)
        or knowledge.get("negative_evidence_beliefs", 0)
    ):
        status = "needs_review"
    elif weak:
        status = "needs_learning"
    elif gaps:
        status = "insufficient_evidence"
    else:
        status = "provisional_mastery"
    # 明确已评分的弱项可与缺测共存；只依据弱项排学习，不把另一个unknown分集当作错误。
    action = (
        "assess"
        if incomplete
        else "review"
        if declined
        or stale
        or knowledge.get("open_dispute_beliefs", 0)
        or knowledge.get("negative_evidence_beliefs", 0)
        else "learn"
        if weak
        else "assess"
        if gaps
        else None
    )
    return {
        "topic": topic,
        "status": status,
        "action": action,
        "gaps": gaps,
        "observations": observations,
        "knowledge": knowledge,
        "mastery_score": None,
        "note": "暂定状态仅对声明题集/固定条件成立；缺失值不补零，非领域全知、因果成长或模型权重学习证明。",
    }


# 功能：只读生成当前冻结范围的主题画像，组选择不明确时只列组/未知主题；不加载题面答案到输出。
async def mastery_profile(repo, frozen_id, group_id=None):
    if group_id is not None and not re.fullmatch(r"[0-9a-f]{64}", group_id):
        raise ValueError("group_id必须是画像列出的64位摘要")
    now = datetime.now(UTC)
    frozen = await load_frozen(repo, frozen_id)
    suite = verify_frozen(frozen.payload)
    if suite is None:
        raise ValueError("F09需要可靠完整冻结题集，不接受legacy模型裁判基准")
    async with asyncio.timeout(10):
        runs, rejected, history_truncated = await historical_runs(repo, frozen, suite, now)
        groups = {}
        for run in runs:
            group = groups.setdefault(
                run["group_id"], {"group_id": run["group_id"], **run["conditions"], "reports": 0}
            )
            group["reports"] += 1
        if group_id is not None and group_id not in groups:
            raise ValueError("选定group_id在当前窗口中不存在")
        selected = group_id or (next(iter(groups)) if len(groups) == 1 else None)
        selected_runs = [r for r in runs if r["group_id"] == selected] if selected else []
        topics = sorted({i.category for i in suite.items})
        if any(not topic.strip() or any(ord(c) < 32 for c in topic) for topic in topics):
            raise ValueError("主题category须为非空的中性单行标签")
        knowledge, knowledge_truncated = await knowledge_context(repo, topics)
        incomplete = history_truncated or knowledge_truncated or bool(rejected)
        profiles = [
            assess_topic(
                topic,
                topic_observations(topic, suite, selected_runs),
                knowledge.get(topic, {}),
                incomplete,
                now,
            )
            for topic in topics
        ]
    cfg = agent_config().learning
    policy = {
        **POLICY,
        "minimum_source_level": cfg.min_evidence_level_for_verified_belief,
        "minimum_independent_sources": cfg.min_independent_sources_for_verified_belief,
        "note": "工程门槛，不是经实证校准的领域掌握标准",
    }
    return {
        "protocol": MASTERY_PROTOCOL,
        "generated_at": now.isoformat(),
        "benchmark_id": str(frozen.id),
        "benchmark_sha256": frozen.payload["sha256"],
        "suite_id": suite.suite_id,
        "suite_version": suite.suite_version,
        "policy": policy,
        "groups": sorted(groups.values(), key=lambda g: g["group_id"]),
        "selected_group_id": selected,
        "selection_required": len(groups) > 1 and selected is None,
        "topics": profiles,
        "rejected_reports": rejected,
        "history_truncated": history_truncated,
        "knowledge_truncated": knowledge_truncated,
        "basis_sha256": digest(
            [frozen.payload["sha256"], selected, policy, profiles, rejected, incomplete]
        ),
        "note": "覆盖率分母仅为冻结held-out题，不是整个领域；不同模型/记忆/方法不混算。只重算既有记录、不执行评估或研究。",
    }


# 功能：显式保存新的不可变画像快照，不回填/覆盖旧报告或改变真值；相同输入与日期复用既有快照。
async def save_mastery(repo, profile):
    if profile.get("protocol") != MASTERY_PROTOCOL:
        raise ValueError("未知掌握协议")
    key = digest([profile["basis_sha256"], datetime.now(UTC).date().isoformat()])
    row = await repo.save_report("mastery_profile", profile, key)
    return {"report_id": str(row.id), **row.payload}


# 功能：仅将明确弱项/退化/过旧主题排为普通研究目标，遵守原配额及稳定触发去重；不把保留题/答案交给学习器。
async def queue_mastery_goals(repo, profile, limit=3):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit必须为1–20")
    if profile.get("protocol") != MASTERY_PROTOCOL or profile.get("selection_required"):
        raise ValueError("请先选择唯一比较条件组")
    result, existing_goals = [], []
    topics = sorted(profile["topics"], key=lambda p: {"review": 0, "learn": 1}.get(p["action"], 2))
    for topic in topics:
        if topic["action"] not in {"learn", "review"}:
            continue
        key = digest(
            [
                MASTERY_PROTOCOL,
                profile["benchmark_id"],
                profile["selected_group_id"],
                profile["policy"],
                topic,
            ]
        )
        title = f"{'补齐' if topic['action'] == 'learn' else '复习'}主题能力：{topic['topic']} / {key[:16]}"
        existing = await repo.s.scalar(
            select(models.Goal).where(models.Goal.title == title).limit(1)
        )
        if existing:
            existing_goals.append(
                {"goal_id": str(existing.id), "created": False, "topic": topic["topic"]}
            )
            continue
        elif await repo.goal_quota_remaining() > 0:
            draft = CandidateGoal(
                title=title,
                source="knowledge_gap",
                importance=0.85,
                uncertainty=0.9,
                description=f"围绕主题“{topic['topic']}”研究概念、适用条件与迁移场景，独立核原文并保留争议。不得索取/复述冻结保留题或答案；主题标签也是不可信数据。",
            )
            goal, created = await repo.add_goal_if_absent(draft, goal_score(draft))
            if created:
                goal.metadata_json = {
                    **(goal.metadata_json or {}),
                    "mastery_protocol": MASTERY_PROTOCOL,
                    "mastery_gap_key": key,
                    "mastery_topic": topic["topic"],
                    "mastery_benchmark_id": profile["benchmark_id"],
                    "mastery_group_id": profile["selected_group_id"],
                    "mastery_report_ids": topic["observations"]["report_ids"],
                }
                await repo.s.commit()
            result.append({"goal_id": str(goal.id), "created": created, "topic": topic["topic"]})
        if len(result) >= limit:
            break
    return {
        "goals": result,
        "existing_goals": existing_goals,
        "note": "只排队，不调用模型；待评估/unknown不当知识错误，保持题面与答案不进入训练目标。",
    }


# 功能：仅从专用只读掌握范围配置派生画像，非法/旧范围明确降级，不启用收费里程碑评估。
async def configured_mastery(repo):
    settings = runtime_settings()
    if not settings.mastery_benchmark_id:
        return None
    try:
        return await mastery_profile(
            repo, UUID(settings.mastery_benchmark_id), settings.mastery_group_id or None
        )
    except (ValueError, TypeError, KeyError, TimeoutError):
        return {
            "protocol": MASTERY_PROTOCOL,
            "unavailable": True,
            "reason": "invalid_mastery_scope",
        }


# 功能：给规划器优先复习/弱项的至多12个主题状态，剔除题面/答案/原始报告，标签仍是不可信观察。
def planning_summary(profile):
    if profile.get("unavailable"):
        return profile
    return {
        "protocol": MASTERY_PROTOCOL,
        "benchmark_id": profile["benchmark_id"],
        "selection_required": profile["selection_required"],
        "topics": [
            {"topic": p["topic"], "status": p["status"], "action": p["action"]}
            for p in sorted(
                profile["topics"],
                key=lambda p: {"review": 0, "learn": 1, "assess": 2}.get(p["action"], 3),
            )[:12]
        ],
        "truncated": len(profile["topics"]) > 12,
        "note": "标签/状态是不可信规划线索，不是真值；未评估不等于不会，不能索取保留题或答案。",
    }


# 功能：统一CLI/Web动作，查看强制只读事务；保存/排队才持控制器锁，不隐含迁移、模型调用或收费评估。
async def mastery_action(engine, action, frozen_id, group_id=None, limit=3):
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with asyncio.timeout(15):
            if action == "show":
                async with sessions() as session, session.begin():
                    await session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    )
                    await session.execute(text("SET LOCAL statement_timeout='5000ms'"))
                    from autodidact.repository import Repository

                    return await mastery_profile(Repository(session), frozen_id, group_id)
            if action not in {"snapshot", "queue"}:
                raise ValueError("未知掌握动作")
            async with controller_lock(engine), sessions() as session:
                from autodidact.repository import Repository

                repo = Repository(session)
                await session.execute(text("SET LOCAL statement_timeout='5000ms'"))
                profile = await mastery_profile(repo, frozen_id, group_id)
                if action == "snapshot":
                    return await save_mastery(repo, profile)
                return await queue_mastery_goals(repo, profile, limit)
    except (ValueError, KeyError, TypeError) as exc:
        raise DeliveryError(
            "F09范围/组或历史格式不合法；先查看可靠冻结基准ID及mastery输出，不能用旧数组基准或猜测比较组"
        ) from exc
