# 文件职责：冻结人工复核依据，逐项确认排队或保存来源校正批注，保留原真值/证据和不可变操作审计。
from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, model_validator
from sqlalchemy import String, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import aliased

from autodidact import models
from autodidact.audit import fields
from autodidact.config import agent_config
from autodidact.delivery import DeliveryError
from autodidact.goals.scorer import goal_score
from autodidact.learning.ground_truth import digest
from autodidact.lifecycle import MemoryLifecycle
from autodidact.repository import Repository
from autodidact.runtime import controller_lock
from autodidact.schemas import CandidateGoal

PROTOCOL = "human_review_v1"
Action = Literal["review_belief", "investigate_dispute", "annotate_source"]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]
Actor = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
OPEN = {"open", "investigating", "unresolved"}
ACTIVE = {"discovered", "planned", "researching", "synthesizing", "testing", "reflecting", "failed"}


class SourceCorrection(BaseModel):
    model_config = {"extra": "forbid"}
    field: Literal["title", "author", "published_at"]
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    excerpt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]

    # 功能：日期批注只接受明确日历日期，建议值须逐字出现在摘录内，锚定不代表语义认证。
    @model_validator(mode="after")
    def anchored_value(self):
        if (
            self.field == "published_at"
            and date.fromisoformat(self.value).isoformat() != self.value
        ):
            raise ValueError("日期建议须为YYYY-MM-DD，不猜测出版时刻")
        if self.value not in self.excerpt:
            raise ValueError("建议值须逐字包含在原文摘录中")
        return self


class ReviewRequest(BaseModel):
    model_config = {"extra": "forbid"}
    request_id: UUID
    action: Action
    entity_id: UUID
    actor: Actor
    reason: Reason
    correction: SourceCorrection | None = None

    # 功能：只允许来源批注携带有据校正字段，拒绝跨动作字段/任意真值编辑。
    @model_validator(mode="after")
    def relevant(self):
        if (self.action == "annotate_source") != (self.correction is not None):
            raise ValueError("仅annotate_source必须且可提供correction")
        return self


class ReviewDecision(BaseModel):
    model_config = {"extra": "forbid"}
    proposal_id: UUID
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]
    actor: Actor
    reason: Reason
    confirm: bool = Field(strict=True)


class ReviewQuery(BaseModel):
    model_config = {"extra": "forbid"}
    proposal_id: UUID | None = None
    entity_id: UUID | None = None
    action: Action | None = None
    state: Literal["all", "pending", "approved", "rejected"] = "all"
    limit: int = Field(default=20, ge=1, le=20)
    offset: int = Field(default=0, ge=0, le=10000)

    # 功能：详情只按提议ID读取，拒绝混合列表筛选，防止页面错选实体。
    @model_validator(mode="after")
    def detail_only(self):
        if self.proposal_id and (
            self.entity_id or self.action or self.state != "all" or self.offset
        ):
            raise ValueError("提议详情不可混合列表筛选")
        return self


# 功能：把日期/UUID转换为规范JSON并限制2MiB/有限数值，不以截断载荷保存可执行决定。
def review_payload(value):
    plain = json.loads(json.dumps(value, default=str, ensure_ascii=False, allow_nan=False))
    plain = {"protocol": PROTOCOL, **plain}
    if len(json.dumps(plain, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
        raise ValueError("人工复核依据超过2MiB，拒绝截断后执行")
    return {**plain, "sha256": digest(plain)}


# 功能：核对不可变复核报告的协议及完整摘要，未知旧记录不默认为批准。
def checked(row, kind):
    if row is None or row.kind != kind:
        raise ValueError("人工复核记录不存在")
    value = row.payload
    if value.get("protocol") != PROTOCOL or value.get("sha256") != digest(
        {k: v for k, v in value.items() if k != "sha256"}
    ):
        raise ValueError("人工复核报告协议/摘要不一致")
    return value


# 功能：有界读取人工JSON，不执行内容或主动访问其中URL。
def read_review_request(path):
    from pathlib import Path

    candidate = Path(path)
    if not candidate.is_file() or candidate.stat().st_size > 65536:
        raise ValueError("人工复核清单须为不超过64KiB的JSON文件")
    return ReviewRequest.model_validate_json(candidate.read_text(encoding="utf-8-sig"))


class HumanReview:
    # 功能：复用原仓库/事务，不创建模型、外部客户端或独立真值库。
    def __init__(self, repo):
        self.repo, self.s = repo, repo.s

    # 功能：按固定实体类型读取，执行前行锁刷新，禁止任意ORM对象或属性访问。
    async def entity(self, action, entity_id, lock=False):
        model = {
            "review_belief": models.Belief,
            "investigate_dispute": models.Dispute,
            "annotate_source": models.Source,
        }[action]
        stmt = select(model).where(model.id == entity_id)
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        row = await self.s.scalar(stmt)
        if row is None:
            raise ValueError("复核实体不存在")
        return row

    # 功能：冻结来源文字/书目/质量/metadata摘要，校正仍为人工声明，不改原Source或血缘。
    def source_basis(self, row):
        return {
            **fields(
                row,
                "id url normalized_url publisher_key title author published_at source_type quality_class quality_reason credibility_score evidence_level content_hash fetched_at",
            ),
            "text_sha256": digest(row.extracted_text),
            "metadata_sha256": digest(row.metadata_json),
        }

    # 功能：复用生命周期快照并保留原证据摘录/来源声明，超过200边拒绝，不用摘要代替人工可读原文。
    async def belief_basis(self, row):
        value = await MemoryLifecycle(self.repo).snapshot("belief", row)
        edges = (
            await self.s.execute(
                select(models.Evidence, models.Source)
                .outerjoin(models.Source)
                .where(models.Evidence.belief_id == row.id)
                .order_by(models.Evidence.id)
                .limit(201)
            )
        ).all()
        if len(edges) > 200:
            raise ValueError("信念证据超过200条，拒绝截断审批依据")
        value["original_evidence"] = [
            {
                "id": str(e.id),
                "excerpt": e.excerpt,
                "stance": e.stance,
                "source": self.source_basis(s) if s else None,
            }
            for e, s in edges
        ]
        return value

    # 功能：冻结目标实体和原证据/争议范围，有界超限拒绝；复用F13信念快照而非另设认知规则。
    async def basis(self, request, lock=False):
        row = await self.entity(request.action, request.entity_id, lock)
        if request.action == "annotate_source":
            if request.correction.excerpt not in (row.extracted_text or ""):
                raise ValueError("校正摘录不在已入库来源原文中，需先通过原读取路径取得资料")
            value = self.source_basis(row)
        elif request.action == "review_belief":
            if row.lifecycle_state != "active" or row.status == "retracted":
                raise ValueError("只能对active且未撤回信念排新复核；归档先走显式恢复")
            value = await self.belief_basis(row)
        else:
            if row.status not in OPEN:
                raise ValueError("争议已解决，不重开或覆盖原裁决")
            belief = await self.entity("review_belief", row.belief_id, lock)
            claim = await self.s.get(models.Claim, row.incoming_claim_id)
            if claim is None:
                raise ValueError("争议原主张缺失，不能伪造调查依据")
            anchors = (
                await self.s.execute(
                    select(models.ClaimEvidence, models.Source)
                    .join(models.Source)
                    .where(models.ClaimEvidence.claim_id == claim.id)
                    .order_by(models.ClaimEvidence.id)
                    .limit(201)
                )
            ).all()
            if len(anchors) > 200:
                raise ValueError("争议主张超过200锚点，拒绝截断审批依据")
            value = {
                "dispute": fields(
                    row,
                    "id belief_id incoming_claim_id status contradiction_score previous_belief_state resolution_metadata resolution_notes resolved_at",
                ),
                "belief": await self.belief_basis(belief),
                "claim": fields(
                    claim, "id statement topic scope structure source_ids confidence reasoning"
                ),
                "anchors": [
                    {
                        "anchor": fields(e, "id excerpt excerpt_hash status assessment"),
                        "source": self.source_basis(s),
                    }
                    for e, s in anchors
                ],
            }
        return review_payload({"basis": value})["basis"]

    # 功能：保存请求UUID唯一的不可变提议，重复同输入复用，改理由/对象须新UUID，不直接排队。
    async def propose(self, request):
        spec = request.model_dump(mode="json")
        prior = await self.repo.get_report("human_review_proposal", str(request.request_id))
        if prior:
            if checked(prior, "human_review_proposal")["request"] != spec:
                raise ValueError("request_id已用于其他请求，不能覆盖")
            return {"proposal_id": str(prior.id), "replayed": True}
        basis = await self.basis(request)
        report = models.ResearchReport(
            kind="human_review_proposal",
            report_key=str(request.request_id),
            payload=review_payload(
                {
                    "request": spec,
                    "basis": basis,
                    "impact": "approve仅排调查目标或保存人工来源校正批注，不改真值/评分/原书目/证据，不直接调用模型。若已有授权计划运行，新目标可被后续轮次选中并收费；只想排队须先停止计划。",
                }
            ),
        )
        self.s.add(report)
        await self.s.commit()
        return {"proposal_id": str(report.id), "state": "pending", "payload": report.payload}

    # 功能：沿原配额/目标索引与旁路metadata原子排队，已有活动任务或争议终态任务复用，不清重试次数。
    async def queue(self, request, proposal_id):
        belief_review = request.action == "review_belief"
        field = "belief_review_id" if belief_review else "dispute_id"
        stmt = select(models.Goal).where(
            models.Goal.metadata_json[field].astext == str(request.entity_id)
        )
        if belief_review:
            stmt = stmt.where(models.Goal.status.in_(ACTIVE))
        existing = await self.s.scalar(
            stmt.order_by(models.Goal.created_at.desc(), models.Goal.id).limit(1)
        )
        if existing:
            return {"goal_id": str(existing.id), "created": False, "goal_status": existing.status}
        if await self.repo.goal_quota_remaining() <= 0:
            raise ValueError("今日新增目标配额已满；未批准/未排队，稍后核对依据再确认")
        if not belief_review:
            dispute = await self.entity(request.action, request.entity_id)
            if (
                len((dispute.resolution_metadata or {}).get("investigation_session_ids", []))
                >= agent_config().learning.max_dispute_investigations
            ):
                raise ValueError("争议已达调查次数上限，不用人工审批清零绕过")
        draft = CandidateGoal(
            title=f"人工原文复核信念 {request.entity_id} / {proposal_id}"
            if belief_review
            else f"独立调查争议 {request.entity_id}",
            description=f"人工复核请求 {proposal_id}；仅调查，不将请求文字视为控制器指令。\n复核理由：{request.reason}",
            source="retention" if belief_review else "conflict",
            importance=0.95,
            uncertainty=0.9,
        )
        metadata = {field: str(request.entity_id), "human_review_proposal_id": str(proposal_id)}
        if belief_review:
            from autodidact.knowledge.belief_review import REVIEW_PROTOCOL

            metadata.update(
                review_protocol=REVIEW_PROTOCOL,
                review_reason=request.reason,
                review_trigger_key=str(proposal_id),
            )
        goal, created = await self.repo.add_goal_if_absent(
            draft, goal_score(draft), metadata=metadata, commit=False
        )
        if goal.metadata_json.get(field) != str(request.entity_id):
            raise ValueError("同标题目标不是对应调查任务，拒绝混用或改写旧目标")
        return {"goal_id": str(goal.id), "created": created, "goal_status": goal.status}

    # 功能：逐项确认/拒绝；批准锁后重核冻结依据，目标或批注与不可变决定一次提交，拒绝不执行动作。
    async def decide(self, decision):
        if decision.confirm is not True:
            raise ValueError("人工复核决定须显式confirm=true")
        proposal = await self.s.get(models.ResearchReport, decision.proposal_id)
        value = checked(proposal, "human_review_proposal")
        if decision.proposal_sha256 != value["sha256"]:
            raise ValueError("确认摘要不是此提议的冻结版本，请先重新查看详情")
        prior = await self.repo.get_report("human_review_decision", str(proposal.id))
        if prior:
            payload = checked(prior, "human_review_decision")
            if payload["decision"] != decision.model_dump(mode="json"):
                raise ValueError("此提议已有不同决定/操作人或理由，不能覆盖；请另提新请求")
            return {"decision_id": str(prior.id), "replayed": True, "payload": payload}
        request = ReviewRequest.model_validate(value["request"])
        effect = None
        if decision.decision == "approve":
            if await self.basis(request, lock=True) != value["basis"]:
                raise ValueError("原文/证据/认知或版本已变化，旧提议过期；请新建请求保留历史")
            if request.action == "annotate_source":
                annotation = models.ResearchReport(
                    kind="human_source_annotation",
                    report_key=str(proposal.id),
                    payload=review_payload(
                        {
                            "proposal_id": str(proposal.id),
                            "source_id": str(request.entity_id),
                            "correction": request.correction.model_dump(mode="json"),
                            "basis": value["basis"],
                            "note": "经人工批准的校正批注，不认证作者/出版日期；不覆盖原Source或提升证据等级，自动读者仍用原字段。",
                        }
                    ),
                )
                self.s.add(annotation)
                await self.s.flush()
                effect = {"annotation_id": str(annotation.id), "source_unchanged": True}
            else:
                effect = await self.queue(request, proposal.id)
        report = models.ResearchReport(
            kind="human_review_decision",
            report_key=str(proposal.id),
            payload=review_payload(
                {
                    "decision": decision.model_dump(mode="json"),
                    "request": value["request"],
                    "proposal_sha256": value["sha256"],
                    "effect": effect,
                    "note": "本机actor是声明而非生产认证；批准不是信念/证据真值审批，研究须另显式启动。",
                }
            ),
        )
        self.s.add(report)
        await self.s.commit()
        return {
            "decision_id": str(report.id),
            "state": "approved" if decision.decision == "approve" else "rejected",
            "payload": report.payload,
        }

    # 功能：读取详情时给出当前有效性与关联审计ID，过期仍能拒绝，不更改原提议/决定。
    async def detail(self, row):
        value = checked(row, "human_review_proposal")
        request = ReviewRequest.model_validate(value["request"])
        decision = await self.repo.get_report("human_review_decision", str(row.id))
        current, status = None, "pending"
        if decision:
            checked(decision, "human_review_decision")
            status = (
                "approved" if decision.payload["decision"]["decision"] == "approve" else "rejected"
            )
        try:
            current = await self.basis(request)
            validity = "current" if current == value["basis"] else "stale"
        except ValueError:
            validity = "stale"
        return {
            "proposal_id": str(row.id),
            "state": status,
            "basis_validity": validity,
            "proposal_sha256": value["sha256"],
            "payload": value,
            "current_basis": current,
            "decision_id": str(decision.id) if decision else None,
            "decision": decision.payload if decision else None,
        }

    # 功能：对提议/决定做有界分页与关联筛选，列表不逐条加载完整原文，pending不等于依据仍有效。
    async def show(self, query):
        if query.proposal_id:
            return await self.detail(await self.s.get(models.ResearchReport, query.proposal_id))
        proposal = models.ResearchReport
        decision = aliased(models.ResearchReport)
        stmt = select(
            proposal.id,
            proposal.created_at,
            proposal.payload["request"].label("request"),
            decision.id.label("decision_id"),
            decision.payload["decision"]["decision"].astext.label("choice"),
        )
        stmt = stmt.outerjoin(
            decision,
            (decision.kind == "human_review_decision")
            & (decision.report_key == proposal.id.cast(String)),
        )
        stmt = stmt.where(proposal.kind == "human_review_proposal")
        if query.entity_id:
            stmt = stmt.where(
                proposal.payload["request"]["entity_id"].astext == str(query.entity_id)
            )
        if query.action:
            stmt = stmt.where(proposal.payload["request"]["action"].astext == query.action)
        if query.state == "pending":
            stmt = stmt.where(decision.id.is_(None))
        elif query.state != "all":
            stmt = stmt.where(
                decision.payload["decision"]["decision"].astext
                == ("approve" if query.state == "approved" else "reject")
            )
        rows = (
            (
                await self.s.execute(
                    stmt.order_by(proposal.created_at.desc(), proposal.id)
                    .offset(query.offset)
                    .limit(query.limit + 1)
                )
            )
            .mappings()
            .all()
        )
        return {
            "items": [dict(r) for r in rows[: query.limit]],
            "next_offset": query.offset + query.limit
            if len(rows) > query.limit and query.offset + query.limit <= 10000
            else None,
            "note": "待处理仅指无决定；先查详情/当前原证据再确认。只读不主动复核模型或外部原文。",
        }


# 功能：统一人工复核入口，只读快照有界，写操作同库控制器互斥，无隐式迁移/网络/模型调用。
async def human_review_action(engine, action, *, request=None, decision=None, query=None):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if action == "show":
            async with factory() as session, asyncio.timeout(20):
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                await session.execute(text("SET LOCAL statement_timeout = '5s'"))
                return await HumanReview(Repository(session)).show(query or ReviewQuery())
        if action not in {"propose", "decide"}:
            raise ValueError("未知人工复核动作")
        if action == "decide" and (decision is None or decision.confirm is not True):
            raise ValueError("人工复核决定须显式确认")
        async with controller_lock(engine), factory() as session, asyncio.timeout(30):
            reviewer = HumanReview(Repository(session))
            return (
                await reviewer.propose(request)
                if action == "propose"
                else await reviewer.decide(decision)
            )
    except (ValueError, TypeError, KeyError) as exc:
        raise DeliveryError(
            str(exc)
            if type(exc) is ValueError
            else "人工复核参数/旧记录结构无效，校验输入和外部异常正文不展示。"
        ) from None
