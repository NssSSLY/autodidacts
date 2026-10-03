# 文件职责：复用认知仓库的只读审计，提供分页列表、原文/血缘/历史/争议/预算详情，不调用模型或写状态。
from __future__ import annotations

from datetime import UTC, datetime
from itertools import islice
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select

from autodidact import models
from autodidact.config import agent_config
from autodidact.knowledge.lineage import DEPENDENCIES
from autodidact.knowledge.sources import normalize_url

KINDS = {
    "sources": models.Source,
    "claims": models.Claim,
    "beliefs": models.Belief,
    "disputes": models.Dispute,
    "reports": models.ResearchReport,
    "operations": models.OperationEvent,
}
NOTE = "只读认知审计：主张/模型观察不是真值，未知不等于已验证；分页/展示截断不代表完整证据。"


class AuditNotFound(ValueError):
    pass


class AuditQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=10000)
    q: str = Field(default="", max_length=300)
    state: str = Field(default="", max_length=50)
    part: str = Field(default="", max_length=30)
    text_offset: int = Field(default=0, ge=0, le=2000000)


# 功能：从显式字段生成详情字典，不导出向量、密钥配置或任意ORM属性。
def fields(row, names):
    return {name: getattr(row, name) for name in names.split()}


# 功能：仅把合法UUID的显式关联变为站内审计引用，不将外部URL/任意文字变成可执行链接。
def reference(kind, value, label):
    try:
        return {"kind": kind, "id": str(UUID(str(value))), "label": label}
    except (ValueError, TypeError, AttributeError):
        return None


# 功能：限制不可信JSON展示深度/节点/文字/集合数量，明确截断而不修改原数据库数据。
def bounded_display(value):
    budget = {"nodes": 2000, "chars": 120000, "truncated": False}

    # 功能：递归复制有限展示值；截断位置留标记，控制器不执行所含指令。
    def visit(item, depth=0):
        budget["nodes"] -= 1
        if depth > 10 or budget["nodes"] < 0 or budget["chars"] <= 0:
            budget["truncated"] = True
            return "[展示已截断]"
        if isinstance(item, dict):
            entries = list(islice(item.items(), 50))
            if len(item) > 50:
                budget["truncated"] = True
            return {str(k)[:200]: visit(v, depth + 1) for k, v in entries}
        if isinstance(item, (list, tuple)):
            if len(item) > 50:
                budget["truncated"] = True
            return [visit(v, depth + 1) for v in item[:50]]
        if isinstance(item, (str, UUID, datetime)):
            raw = item.isoformat() if isinstance(item, datetime) else str(item)
            size = min(12000, budget["chars"])
            budget["chars"] -= min(len(raw), size)
            if len(raw) > size:
                budget["truncated"] = True
                return raw[:size] + "[展示已截断]"
            return raw
        return item

    # 导航来自本程序，不因不可信大正文耗尽展示预算而损坏分页或实体ID。
    result = {k: value[k] for k in ["kind", "id", "part", "parts", "pagination"] if k in value}
    if "references" in value:
        result["references"] = [
            {**r, "label": str(r["label"])[:250]} for r in value["references"][:100]
        ]
    if "items" in value:
        result["items"] = [visit(item) for item in value["items"][:50]]
    for key in ["data", "budget"]:
        if key in value:
            result[key] = visit(value[key])
    return {"view": result, "display_truncated": budget["truncated"], "note": NOTE}


class CognitiveAudit:
    # 功能：绑定既有仓库会话，只负责有界读取；HTTP层另设数据库只读事务。
    def __init__(self, repo):
        self.repo, self.s = repo, repo.s

    # 功能：稳定排序并多取一条判断下一页，不执行全库count或写审计记录。
    async def page(self, statement, query, order):
        rows = list(
            (
                await self.s.scalars(
                    statement.order_by(*order).offset(query.offset).limit(query.limit + 1)
                )
            ).all()
        )
        return rows[: query.limit], {
            "offset": query.offset,
            "limit": query.limit,
            "next_offset": query.offset + query.limit
            if len(rows) > query.limit and query.offset + query.limit <= 10000
            else None,
            "truncated": len(rows) > query.limit,
            "offset_cap": 10000,
        }

    # 功能：列出包含撤回/争议/已决议的认知记录，文字过滤是SQL字面量检索，不计算嵌入。
    async def listing(self, kind, query):
        if kind not in KINDS or query.part or query.text_offset:
            raise ValueError("未知审计类型或列表参数")
        model = KINDS[kind]
        names = {
            "sources": "title url quality_class",
            "claims": "statement topic",
            "beliefs": "statement topic status confidence",
            "disputes": "status resolution_notes contradiction_score",
            "reports": "kind report_key",
            "operations": "resource status reserved actual",
        }[kind]
        searchable = {
            "sources": "title url",
            "claims": "statement topic",
            "beliefs": "statement topic",
            "disputes": "status resolution_notes",
            "reports": "kind report_key",
            "operations": "resource status",
        }
        columns = [getattr(model, n) for n in searchable.get(kind, names.split()[0]).split()]
        statement = select(model)
        if query.q:
            statement = statement.where(
                or_(*(c.icontains(query.q, autoescape=True) for c in columns))
            )
        if query.state:
            state_field = {"sources": "quality_class", "reports": "kind", "claims": "topic"}.get(
                kind, "status"
            )
            statement = statement.where(getattr(model, state_field) == query.state)
        time_field = "fetched_at" if kind == "sources" else "created_at"
        rows, pagination = await self.page(
            statement, query, [getattr(model, time_field).desc(), model.id.desc()]
        )
        result = {
            "kind": kind,
            "items": [fields(r, "id " + time_field + " " + names) for r in rows],
            "pagination": pagination,
        }
        if kind == "operations":
            result["budget"] = await self.budget()
        return bounded_display(result)

    # 功能：汇总UTC今日实耗/未知预留及配置上限，0上限表示未限制而非免费，不创建预算预留。
    async def budget(self):
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = (
            await self.s.execute(
                select(
                    models.OperationEvent.resource,
                    func.sum(
                        func.coalesce(models.OperationEvent.actual, models.OperationEvent.reserved)
                    ).label("used"),
                )
                .where(models.OperationEvent.created_at >= start)
                .group_by(models.OperationEvent.resource)
                .limit(50)
            )
        ).all()
        cfg = agent_config().learning
        limits = {
            "llm_calls": cfg.max_daily_model_calls,
            "tokens": cfg.max_daily_tokens,
            "searches": cfg.max_daily_searches,
            "web_reads": cfg.max_daily_web_reads,
            "web_model_calls": cfg.max_daily_web_model_calls,
            "embedding_calls": cfg.max_daily_embedding_calls,
            "estimated_usd": cfg.max_daily_estimated_cost_usd,
        }
        used = dict(rows)
        return {
            "day_utc": start.date().isoformat(),
            "resources": [
                {"resource": k, "used_or_reserved": used.get(k, 0), "limit": limit or None}
                for k, limit in limits.items()
            ],
            "note": "未知实耗保留预留估算；美元不是提供方账单硬限额，未设上限不表示免费。",
        }

    # 功能：读取详情和指定关联分页；只复用库内证据/历史，不联网重核或触发调查。
    async def detail(self, kind, entity_id, query):
        if kind not in KINDS or query.q or query.state:
            raise ValueError("未知审计类型或详情参数")
        row = await self.s.get(KINDS[kind], entity_id)
        if row is None:
            raise AuditNotFound("记录不存在或已不可用")
        parts = {
            "claims": ["evidence"],
            "beliefs": ["evidence", "history", "disputes", "reviews"],
            "sources": ["lineage", "text"],
            "disputes": [],
            "reports": [],
            "operations": [],
        }[kind]
        part = query.part or (parts[0] if parts else "")
        if part not in parts and part:
            raise ValueError("此记录不支持指定详情分页")
        if query.text_offset and not (kind == "sources" and part == "text"):
            raise ValueError("原文偏移仅用于来源原文")
        if query.offset and (not parts or part == "text"):
            raise ValueError("此详情不支持行偏移")
        refs, collection, pagination = [], [], None
        if kind == "claims":
            data = await self.repo.claim_audit(entity_id, query.limit, query.offset)
            collection = data.pop("evidence")
            pagination = {k: data.pop(k) for k in ["offset", "next_offset", "truncated"]}
            refs.extend(reference("sources", r["source_id"], "引文来源") for r in collection)
            for key in ["parent_claim_ids", "child_claim_ids", "investigation_child_claim_ids"]:
                refs.extend(
                    reference("claims", i, key) for i in (row.structure or {}).get(key, [])[:40]
                )
            data.update(fields(row, "topic reasoning confidence learning_session_id"))
        elif kind == "beliefs":
            data = await self.repo.belief_audit(
                entity_id, query.limit, query.offset if part == "evidence" else 0
            )
            evidence = data.pop("evidence")
            pagination = {k: data.pop(k) for k in ["offset", "next_offset", "truncated"]}
            data["scores"] = self.repo._belief_snapshot(row)
            if part == "evidence":
                collection = evidence
                refs.extend(reference("sources", e["source_id"], "证据来源") for e in evidence)
                refs.extend(
                    reference("claims", (e.get("assessment") or {}).get("claim_id"), "核验原主张")
                    for e in evidence
                )
            else:
                model = {
                    "history": models.BeliefHistory,
                    "disputes": models.Dispute,
                    "reviews": models.ResearchReport,
                }[part]
                statement = select(model)
                statement = (
                    statement.where(
                        model.kind == "belief_review",
                        model.payload["belief_id"].astext == str(entity_id),
                    )
                    if part == "reviews"
                    else statement.where(model.belief_id == entity_id)
                )
                rows, pagination = await self.page(
                    statement, query, [model.created_at.desc(), model.id.desc()]
                )
                keys = {
                    "history": "action previous_state new_state reason",
                    "disputes": "status resolution_notes incoming_claim_id",
                    "reviews": "kind report_key payload",
                }[part]
                collection = [fields(r, "id created_at " + keys) for r in rows]
                if part in {"disputes", "reviews"}:
                    refs.extend(
                        reference("disputes" if part == "disputes" else "reports", r.id, part)
                        for r in rows
                    )
            refs.extend(
                reference("claims", i, "晋升主张")
                for i in (row.metadata_json or {}).get("promoted_claim_ids", [])[:40]
            )
            refs.append(
                reference("reports", data["latest_review_attempt"].get("report_id"), "最近复核报告")
            )
        elif kind == "sources":
            data = await self.repo.source_audit(entity_id)
            data.update(fields(row, "normalized_url publisher_key content_hash fetched_at"))
            if part == "text":
                original = row.extracted_text or ""
                start = query.text_offset
                data["original_text"] = original[start : start + 12000]
                more = len(original) > start + 12000
                pagination = {
                    "text_offset": start,
                    "total_chars": len(original),
                    "next_offset": start + 12000 if more and start + 12000 <= 2000000 else None,
                    "truncated": more,
                    "offset_cap": 2000000,
                }
            else:
                urls = {
                    u
                    for u in [
                        row.url,
                        row.normalized_url or (normalize_url(row.url) if row.url else None),
                    ]
                    if u
                }
                edges, pagination = await self.page(
                    select(models.SourceLink).where(
                        or_(
                            models.SourceLink.source_id == row.id,
                            models.SourceLink.from_url.in_(urls),
                            models.SourceLink.to_url.in_(urls),
                        )
                    ),
                    query,
                    [models.SourceLink.id],
                )
                collection = [
                    {
                        **fields(e, "id from_url to_url relation details"),
                        "dependency": e.relation in DEPENDENCIES,
                    }
                    for e in edges
                ]
                targets = {u for e in edges for u in (e.from_url, e.to_url)}
                if targets:
                    linked = (
                        await self.s.scalars(
                            select(models.Source)
                            .where(
                                or_(
                                    models.Source.url.in_(targets),
                                    models.Source.normalized_url.in_(targets),
                                )
                            )
                            .limit(50)
                        )
                    ).all()
                    refs.extend(
                        reference("sources", s.id, s.title or s.url)
                        for s in linked
                        if s.id != row.id
                    )
                data["lineage_note"] = (
                    "分页显示相邻声明/观察边，普通cites不合并独立性；可沿已入库来源逐层查看，未读目标不自动抓取。"
                )
        elif kind == "disputes":
            data = fields(
                row,
                "id belief_id incoming_claim_id status contradiction_score resolution_notes previous_belief_state resolution_metadata created_at resolved_at",
            )
            refs.extend(
                [
                    reference("beliefs", row.belief_id, "旧信念及历史"),
                    reference("claims", row.incoming_claim_id, "新候选主张"),
                ]
            )
            metadata = row.resolution_metadata or {}
            refs.extend(
                [
                    reference("claims", metadata.get("conditional_claim_id"), "条件化主张"),
                    reference("beliefs", metadata.get("resolved_belief_id"), "决议信念"),
                ]
            )
            refs.extend(
                reference("sources", i, "决议证据来源")
                for i in metadata.get("evidence_source_ids", [])[:50]
            )
            data["outcome_note"] = (
                "keep_old保留旧、adopt_new采用新、conditional条件化、unresolved保持未解决；无决议记录表示未知/尚未决议。"
            )
        elif kind == "reports":
            data = fields(row, "id kind report_key created_at payload")
            refs.append(reference("beliefs", (row.payload or {}).get("belief_id"), "复核信念"))
            refs.append(reference("claims", (row.payload or {}).get("claim_id"), "核验主张"))
        else:
            data = fields(row, "id batch_id resource status reserved actual details created_at")
        return bounded_display(
            {
                "kind": kind,
                "id": str(entity_id),
                "part": part,
                "parts": parts,
                "data": data,
                "items": collection,
                "pagination": pagination,
                "references": [r for r in refs if r][:100],
            }
        )
