# 文件职责：为信念/技能提出非破坏性维护建议，显式确认后原子归档/恢复/重复归并，保留全部证据和历史。
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from autodidact import models
from autodidact.config import agent_config
from autodidact.learning.ground_truth import digest
from autodidact.normalization import normalize_text_key
from autodidact.repository import Repository
from autodidact.retrieval import tokens
from autodidact.runtime import controller_lock

LIFECYCLE_PROTOCOL = "memory_lifecycle_v1"
ENTITIES = {"belief": models.Belief, "skill": models.Skill}
Kind = Literal["belief", "skill"]
Action = Literal["archive", "restore", "group", "review"]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]
Actor = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class ChangeRequest(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Kind
    entity_id: UUID
    action: Action
    reason: Reason
    target_id: UUID | None = None


class DecisionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    proposal_id: UUID
    decision: Literal["apply", "reject"]
    actor: Actor
    reason: Reason
    confirm: bool = Field(strict=True)


class MaintenanceQuery(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Kind = "belief"
    state: Literal["active", "archived", "all"] = "active"
    proposal_id: UUID | None = None
    entity_id: UUID | None = None
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=10000)


# 功能：展示独立可见性/版本/归并指针，不把归档等同撤回或置信度变化；兼容尚未flush的对象。
def lifecycle_view(row):
    return {
        "state": getattr(row, "lifecycle_state", None) or "active",
        "version": getattr(row, "lifecycle_version", None) or 0,
        "canonical_id": str(row.canonical_id) if getattr(row, "canonical_id", None) else None,
    }


# 功能：读取专用可变可见性代数，未发生变更为0；用于阻断旧冻结规划/技能的盲续跑。
async def lifecycle_epoch(repo):
    row = await repo.get_report("memory_lifecycle_epoch", "current")
    if row is None:
        return 0
    payload = row.payload
    if (
        payload.get("protocol") != LIFECYCLE_PROTOCOL
        or type(payload.get("revision")) is not int
        or payload["revision"] < 1
    ):
        raise ValueError("记忆生命周期代数无效，不能绕过旧规划隔离")
    return payload["revision"]


# 功能：只把同类型/同范围/同认识论状态的文字重复项视为可归并，不判断同义或相似结论等价。
def exact_duplicate(kind, left, right):
    if kind == "belief":
        return (
            normalize_text_key(left.statement) == normalize_text_key(right.statement)
            and normalize_text_key(left.topic) == normalize_text_key(right.topic)
            and (left.metadata_json or {}).get("claim_scope", {})
            == (right.metadata_json or {}).get("claim_scope", {})
            and left.status == right.status
            and left.status not in {"disputed", "unresolved", "retracted"}
        )
    return (
        normalize_text_key(left.name) == normalize_text_key(right.name)
        and normalize_text_key(left.trigger_condition or "")
        == normalize_text_key(right.trigger_condition or "")
        and left.procedure == right.procedure
        and (left.metadata_json or {}).get("status") == (right.metadata_json or {}).get("status")
    )


class MemoryLifecycle:
    # 功能：绑定既有仓库/事务，不创建新的知识事实库或调用模型。
    def __init__(self, repo):
        self.repo, self.s = repo, repo.s

    # 功能：读取库内实体；写者按UUID顺序锁定并刷新缓存，不使用会话旧属性作决策。
    async def load(self, kind, ids, lock=False):
        model = ENTITIES[kind]
        stmt = select(model).where(model.id.in_(ids)).order_by(model.id)
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        rows = list((await self.s.scalars(stmt)).all())
        if len(rows) != len(set(ids)):
            raise ValueError("记忆或归并目标不存在")
        return {r.id: r for r in rows}

    # 功能：冻结当前内容/生命周期/证据与开放争议依据，证据超200条拒绝截断后执行维护。
    async def snapshot(self, kind, row):
        common = {
            "kind": kind,
            "id": str(row.id),
            "lifecycle": lifecycle_view(row),
            "confidence": row.confidence,
            "metadata": row.metadata_json or {},
        }
        if kind == "skill":
            value = {
                **common,
                "name": row.name,
                "description": row.description,
                "trigger_condition": row.trigger_condition,
                "procedure": row.procedure,
                "skill_version": row.version,
                "success_count": row.success_count,
                "failure_count": row.failure_count,
            }
        else:
            edges = list(
                (
                    await self.s.execute(
                        select(models.Evidence, models.Source)
                        .select_from(models.Evidence)
                        .outerjoin(models.Source, models.Source.id == models.Evidence.source_id)
                        .where(models.Evidence.belief_id == row.id)
                        .order_by(models.Evidence.id)
                        .limit(201)
                    )
                ).all()
            )
            if len(edges) > 200:
                raise ValueError("信念证据超过200条，不能截断后归档/归并；需分域维护设计")
            disputes = list(
                (
                    await self.s.scalars(
                        select(models.Dispute.id)
                        .where(
                            models.Dispute.belief_id == row.id,
                            models.Dispute.status.in_(["open", "investigating", "unresolved"]),
                        )
                        .order_by(models.Dispute.id)
                        .limit(201)
                    )
                ).all()
            )
            if len(disputes) > 200:
                raise ValueError("开放争议过多，拒绝截断维护快照")
            value = {
                **common,
                "statement": row.statement,
                "topic": row.topic,
                "explanation": row.explanation,
                "status": row.status,
                "evidence_score": row.evidence_score,
                "test_score": row.test_score,
                "stability_score": row.stability_score,
                "source_count": row.source_count,
                "open_dispute_ids": [str(i) for i in disputes],
                "evidence": [
                    {
                        "id": str(e.id),
                        "stance": e.stance,
                        "evidence_level": e.evidence_level,
                        "strength": e.strength,
                        "source_id": str(e.source_id) if e.source_id else None,
                        "excerpt_sha256": digest(e.excerpt),
                        "assessment_sha256": digest(e.assessment),
                        "claim_evidence_id": str(e.claim_evidence_id)
                        if e.claim_evidence_id
                        else None,
                        "source_basis_sha256": digest(
                            [
                                source.content_hash,
                                source.extracted_text,
                                source.evidence_level,
                                source.credibility_score,
                                source.metadata_json,
                            ]
                        )
                        if source
                        else None,
                    }
                    for e, source in edges
                ],
            }
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 2 * 1024 * 1024:
            raise ValueError("维护快照超过2MiB，不能截断后执行变更")
        return value

    # 功能：校验非破坏性转移；开放争议不归档，归并不串链/成环，不改原事实状态或拼接证据。
    async def validate(self, request, row, target, before):
        if (request.action == "group") != bool(request.target_id):
            raise ValueError("只有group必须且仅可提供target_id")
        state = lifecycle_view(row)
        if request.action == "review":
            return
        if request.action == "restore":
            if state["state"] != "archived":
                raise ValueError("只有archived记忆可恢复")
            return
        if state["state"] != "active":
            raise ValueError("只有active记忆可归档或归并")
        model = ENTITIES[request.kind]
        if await self.s.scalar(select(model.id).where(model.canonical_id == row.id).limit(1)):
            raise ValueError("此项是归并组代表，请先恢复组内成员，禁止制造链或悬挂组")
        if request.kind == "belief" and (
            before.get("open_dispute_ids") or row.status in {"disputed", "unresolved"}
        ):
            raise ValueError("有开放争议的信念不得通过归档/归并隐藏，先完成争议流程")
        if request.action == "group":
            if (
                target is None
                or row.id == target.id
                or lifecycle_view(target)["state"] != "active"
                or target.canonical_id
            ):
                raise ValueError("归并目标须是另一active根记忆，不能自指、串链或指向归档项")
            if not exact_duplicate(request.kind, row, target):
                raise ValueError("只允许同范围/同状态文字重复项归并；相似/矛盾项仅供复核建议")
            if request.kind == "belief" and await self.s.scalar(
                select(models.Dispute.id)
                .where(
                    models.Dispute.belief_id == target.id,
                    models.Dispute.status.in_(["open", "investigating", "unresolved"]),
                )
                .limit(1)
            ):
                raise ValueError("归并目标有开放争议")

    # 功能：仅保存不可变提议与完整旧依据，不直接归档；相同内容/版本重复提议复用原记录。
    async def propose(self, request, origin="manual"):
        if (request.action == "group") != bool(request.target_id):
            raise ValueError("只有group必须且仅可提供target_id")
        ids = [request.entity_id, *([request.target_id] if request.target_id else [])]
        rows = await self.load(request.kind, ids)
        row = rows[request.entity_id]
        target = rows.get(request.target_id)
        before = await self.snapshot(request.kind, row)
        await self.validate(request, row, target, before)
        target_before = await self.snapshot(request.kind, target) if target else None
        value = {
            "protocol": LIFECYCLE_PROTOCOL,
            "request": request.model_dump(mode="json"),
            "origin": origin,
            "before": before,
            "target_before": target_before,
            "note": "仅维护提议，不判断真伪。归并只改变可见性/分组，不删除、重写或转移证据。",
        }
        value["sha256"] = digest(value)
        return await self.repo.save_report("memory_change_proposal", value, value["sha256"])

    # 功能：确认后同事务写生命周期、版本、历史/决策及全局代数；重复决策不再次执行旧操作。
    async def decide(self, request):
        if not request.confirm:
            raise ValueError("维护决定必须显式confirm=true")
        proposal = await self.s.get(models.ResearchReport, request.proposal_id)
        if proposal is None or proposal.kind != "memory_change_proposal":
            raise ValueError("维护提议不存在")
        payload = proposal.payload
        if payload.get("protocol") != LIFECYCLE_PROTOCOL or digest(
            {k: v for k, v in payload.items() if k != "sha256"}
        ) != payload.get("sha256"):
            raise ValueError("维护提议协议或摘要不一致")
        previous = await self.repo.get_report("memory_change_decision", str(proposal.id))
        if previous:
            if previous.payload.get("protocol") != LIFECYCLE_PROTOCOL:
                raise ValueError("既有维护决定协议未知，拒绝覆盖")
            if previous.payload.get("decision") != request.decision:
                raise ValueError("提议已决策，不能覆盖原选择；请创建新提议")
            return {"decision_id": str(previous.id), "replayed": True, **previous.payload}
        change = ChangeRequest.model_validate(payload["request"])
        before, after, revision = payload["before"], None, None
        if request.decision == "apply":
            if change.action == "review":
                raise ValueError("复核线索不能直接执行，请核查后另建明确归档/恢复/重复归并提议")
            rows = await self.load(
                change.kind,
                [change.entity_id, *([change.target_id] if change.target_id else [])],
                True,
            )
            row, target = rows[change.entity_id], rows.get(change.target_id)
            if await self.snapshot(change.kind, row) != before or (
                target and await self.snapshot(change.kind, target) != payload["target_before"]
            ):
                raise ValueError("提议后内容/生命周期/证据或争议已变化，拒绝旧快照执行；请重新提议")
            await self.validate(change, row, target, before)
            new_state = "active" if change.action == "restore" else "archived"
            values = {
                "lifecycle_state": new_state,
                "lifecycle_version": row.lifecycle_version + 1,
                "canonical_id": target.id if target else None,
                "updated_at": row.updated_at,
            }
            changed = await self.s.execute(
                update(ENTITIES[change.kind])
                .where(
                    ENTITIES[change.kind].id == row.id,
                    ENTITIES[change.kind].lifecycle_version == before["lifecycle"]["version"],
                )
                .values(**values)
            )
            if changed.rowcount != 1:
                raise ValueError("生命周期并发条件更新未命中唯一记录，拒绝提交")
            await self.s.refresh(row)
            after = await self.snapshot(change.kind, row)
            if after["lifecycle"]["version"] != before["lifecycle"]["version"] + 1:
                raise ValueError("生命周期并发版本不一致，已停止变更")
            if change.kind == "belief":
                self.s.add(
                    models.BeliefHistory(
                        belief_id=row.id,
                        action="lifecycle_" + change.action,
                        previous_state={"lifecycle": before["lifecycle"]},
                        new_state={"lifecycle": after["lifecycle"]},
                        reason=f"{request.actor}: {request.reason}; proposal={proposal.id}",
                    )
                )
            epoch = await self.repo.get_report("memory_lifecycle_epoch", "current")
            revision = await lifecycle_epoch(self.repo) + 1
            value = {"protocol": LIFECYCLE_PROTOCOL, "revision": revision}
            if epoch:
                epoch.payload = value
            else:
                self.s.add(
                    models.ResearchReport(
                        kind="memory_lifecycle_epoch", report_key="current", payload=value
                    )
                )
        result = {
            "protocol": LIFECYCLE_PROTOCOL,
            "proposal_id": str(proposal.id),
            "decision": request.decision,
            "actor": request.actor,
            "reason": request.reason,
            "request": change.model_dump(mode="json"),
            "before": before,
            "after": after,
            "epoch": revision,
            "at": datetime.now(UTC).isoformat(),
            "note": "归档非判假；恢复不提高置信度，不合并/转移任何证据、历史或基准分数。",
        }
        decision = models.ResearchReport(
            kind="memory_change_decision", report_key=str(proposal.id), payload=result
        )
        self.s.add(decision)
        await self.s.commit()
        return {"decision_id": str(decision.id), **result}

    # 功能：有限扫描生成文字重复归并或相似/过旧复核建议，默认不执行；截断明确报告，不覆盖历史。
    async def suggest(self, kind="belief", limit=5):
        if kind not in ENTITIES or not 1 <= limit <= 20:
            raise ValueError("建议类型或数量超出范围")
        model = ENTITIES[kind]
        rows = list(
            (
                await self.s.scalars(
                    select(model)
                    .where(model.lifecycle_state == "active")
                    .order_by(model.created_at, model.id)
                    .limit(501)
                )
            ).all()
        )
        truncated = len(rows) > 500
        rows = rows[:500]
        candidates, comparisons = [], 0
        for left, right in combinations(rows, 2):
            comparisons += 1
            if comparisons > 5000:
                truncated = True
                break
            if exact_duplicate(kind, left, right):
                candidates.append(
                    ChangeRequest(
                        kind=kind,
                        entity_id=right.id,
                        action="group",
                        target_id=left.id,
                        reason="同类型、同范围/状态的规范文字重复；建议非破坏性归并，证据和置信度不转移。",
                    )
                )
            elif kind == "belief" and normalize_text_key(left.topic) == normalize_text_key(
                right.topic
            ):
                a, b = set(tokens(left.statement)), set(tokens(right.statement))
                if a and b and len(a & b) / len(a | b) >= 0.65:
                    candidates.append(
                        ChangeRequest(
                            kind=kind,
                            entity_id=right.id,
                            action="review",
                            reason=f"与{left.id}词项重叠，可能等价/范围重叠或矛盾；仅复核，不证明可合并。",
                        )
                    )
            if len(candidates) >= limit * 3:
                truncated = True
                break
        cutoff = datetime.now(UTC) - timedelta(days=agent_config().learning.memory_review_days)
        for row in rows:
            times = [
                t
                for t in (row.created_at, row.updated_at, getattr(row, "last_accessed_at", None))
                if isinstance(t, datetime) and t.tzinfo is not None
            ]
            if times and max(times) < cutoff:
                candidates.append(
                    ChangeRequest(
                        kind=kind,
                        entity_id=row.id,
                        action="review",
                        reason="长期无更新/访问，时效或效用未知；建议人工核源后决定归档，年龄不等于错误。",
                    )
                )
        proposals, skipped = [], []
        seen = set()
        for candidate in candidates:
            if candidate.entity_id in seen:
                continue
            seen.add(candidate.entity_id)
            try:
                report = await self.propose(candidate, "bounded_rules")
                if not await self.repo.get_report("memory_change_decision", str(report.id)):
                    proposals.append(str(report.id))
            except ValueError as exc:
                skipped.append({"entity_id": str(candidate.entity_id), "reason": str(exc)[:300]})
            if len(proposals) >= limit:
                break
        return {
            "protocol": LIFECYCLE_PROTOCOL,
            "proposal_ids": proposals,
            "skipped": skipped[:20],
            "scan_truncated": truncated,
            "scanned": len(rows),
            "comparisons": min(comparisons, 5000),
            "suggestion_limit": limit,
            "note": "有界规则建议不等于全库去重/语义等价或真实过时认证；没有自动归档/删除/模型请求。",
        }

    # 功能：只读查看当前/归档记忆、提议详情/是否过期及不可变决策，审计不隐藏已归档原文。
    async def view(self, query):
        if query.proposal_id:
            if query.entity_id:
                raise ValueError("提议与实体详情不能混选")
            report = await self.s.get(models.ResearchReport, query.proposal_id)
            if not report or report.kind != "memory_change_proposal":
                raise ValueError("维护提议不存在")
            decision = await self.repo.get_report("memory_change_decision", str(report.id))
            payload = report.payload
            integrity_valid = payload.get("protocol") == LIFECYCLE_PROTOCOL and digest(
                {k: v for k, v in payload.items() if k != "sha256"}
            ) == payload.get("sha256")
            change = ChangeRequest.model_validate(payload["request"])
            try:
                rows = await self.load(
                    change.kind,
                    [change.entity_id, *([change.target_id] if change.target_id else [])],
                )
                current = await self.snapshot(change.kind, rows[change.entity_id])
                target = (
                    await self.snapshot(change.kind, rows[change.target_id])
                    if change.target_id
                    else None
                )
                stale = (
                    not integrity_valid
                    or current != payload["before"]
                    or target != payload["target_before"]
                )
            except (ValueError, KeyError, TypeError):
                current, stale = None, True
            basis_changed = stale
            stale = not integrity_valid or (decision is None and basis_changed)
            return {
                "proposal_id": str(report.id),
                "proposal": payload,
                "integrity_valid": integrity_valid,
                "stale": stale,
                "basis_changed": basis_changed,
                "status": "invalid"
                if not integrity_valid
                else decision.payload.get("decision")
                if decision
                else "stale"
                if stale
                else "pending",
                "current": current,
                "decision": decision.payload if decision else None,
                "decision_id": str(decision.id) if decision else None,
            }
        model = ENTITIES[query.kind]
        if query.entity_id:
            rows = await self.load(query.kind, [query.entity_id])
            row = rows[query.entity_id]
            members = list(
                (
                    await self.s.scalars(
                        select(model)
                        .where(model.canonical_id == row.id)
                        .order_by(model.id)
                        .offset(query.offset)
                        .limit(query.limit + 1)
                    )
                ).all()
            )
            representative = await self.s.get(model, row.canonical_id) if row.canonical_id else None
            events = list(
                (
                    await self.s.scalars(
                        select(models.ResearchReport)
                        .where(
                            models.ResearchReport.kind == "memory_change_decision",
                            models.ResearchReport.payload["request"]["kind"].astext == query.kind,
                            models.ResearchReport.payload["request"]["entity_id"].astext
                            == str(row.id),
                        )
                        .order_by(
                            models.ResearchReport.created_at.desc(), models.ResearchReport.id.desc()
                        )
                        .offset(query.offset)
                        .limit(query.limit + 1)
                    )
                ).all()
            )
            return {
                "data": await self.snapshot(query.kind, row),
                "representative": {
                    "id": str(representative.id),
                    "lifecycle": lifecycle_view(representative),
                }
                if representative
                else None,
                "group_members": [
                    {"id": str(m.id), "lifecycle": lifecycle_view(m)}
                    for m in members[: query.limit]
                ],
                "members_next_offset": query.offset + query.limit
                if len(members) > query.limit
                else None,
                "events": [{"id": str(e.id), "payload": e.payload} for e in events[: query.limit]],
                "next_offset": query.offset + query.limit if len(events) > query.limit else None,
            }
        stmt = select(model)
        if query.state != "all":
            stmt = stmt.where(model.lifecycle_state == query.state)
        rows = list(
            (
                await self.s.scalars(
                    stmt.order_by(model.created_at.desc(), model.id)
                    .offset(query.offset)
                    .limit(query.limit + 1)
                )
            ).all()
        )
        proposals = list(
            (
                await self.s.scalars(
                    select(models.ResearchReport)
                    .where(
                        models.ResearchReport.kind == "memory_change_proposal",
                        models.ResearchReport.payload["request"]["kind"].astext == query.kind,
                    )
                    .order_by(
                        models.ResearchReport.created_at.desc(), models.ResearchReport.id.desc()
                    )
                    .offset(query.offset)
                    .limit(query.limit + 1)
                )
            ).all()
        )
        return {
            "protocol": LIFECYCLE_PROTOCOL,
            "kind": query.kind,
            "state": query.state,
            "items": [
                {
                    "id": str(r.id),
                    "title": r.statement if query.kind == "belief" else r.name,
                    "lifecycle": lifecycle_view(r),
                    "epistemic_status": r.status
                    if query.kind == "belief"
                    else (r.metadata_json or {}).get("status"),
                }
                for r in rows[: query.limit]
            ],
            "next_offset": query.offset + query.limit if len(rows) > query.limit else None,
            "proposals": [
                {
                    "id": str(p.id),
                    "request": p.payload["request"],
                    "decision": (
                        d.payload["decision"]
                        if (d := await self.repo.get_report("memory_change_decision", str(p.id)))
                        else None
                    ),
                }
                for p in proposals[: query.limit]
            ],
            "proposals_next_offset": query.offset + query.limit
            if len(proposals) > query.limit
            else None,
        }


# 功能：统一无模型维护入口，查看独立只读/有限超时，提议及显式决定持原控制器锁，不隐式迁移。
async def lifecycle_action(
    engine, action, *, query=None, change=None, decision=None, kind="belief", limit=5
):
    from autodidact.service_runtime import DeliveryError

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if action == "show":
            async with factory() as session, asyncio.timeout(20):
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                await session.execute(text("SET LOCAL statement_timeout = '5s'"))
                return await MemoryLifecycle(Repository(session)).view(query or MaintenanceQuery())
        if (
            action not in {"propose", "decide", "suggest"}
            or kind not in ENTITIES
            or not 1 <= limit <= 20
        ):
            raise ValueError("维护动作/类型或limit无效")
        async with controller_lock(engine), factory() as session, asyncio.timeout(30):
            maintenance = MemoryLifecycle(Repository(session))
            if action == "suggest":
                return await maintenance.suggest(kind, limit)
            if action == "decide":
                return await maintenance.decide(decision)
            report = await maintenance.propose(change)
            return {"proposal_id": str(report.id), "proposal": report.payload}
    except (ValueError, KeyError, TypeError) as exc:
        raise DeliveryError(str(exc)) from None
