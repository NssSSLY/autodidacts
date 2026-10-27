# 文件职责：读取显式认知来源清单、构造受审计适配器，提供有界经验路由与只读认知来源追踪。
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from autodidact import models
from autodidact.brain.llm import MockLLM, OpenAICompatibleLLM
from autodidact.brain.observed import ObservedLLM
from autodidact.config import runtime_settings
from autodidact.learning.ground_truth import digest, model_identity
from autodidact.web_models.service import WebModelService

Role = Literal["blind", "critique", "synthesis"]


class CognitiveSource(BaseModel):
    model_config = {"extra": "forbid"}
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,59}$")
    kind: Literal["mock", "api", "web"]
    enabled: bool = Field(default=False, strict=True)
    roles: list[Role] = Field(
        default_factory=lambda: ["blind", "critique", "synthesis"], min_length=1, max_length=3
    )
    model: str = Field(default="", max_length=200)
    base_url: str = Field(default="", max_length=2000)
    api_key_env: str = Field(default="", pattern=r"^(?:[A-Z][A-Z0-9_]{0,99})?$")
    web_provider: str = Field(default="", max_length=120)
    temperature: float = Field(default=0.2, ge=0, le=2, allow_inf_nan=False)
    max_output_tokens: int = Field(default=2048, ge=256, le=8192, strict=True)
    input_usd_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    output_usd_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)

    # 功能：拒绝重复角色、混合提供方配置和带URL凭据/查询的API地址，密钥只引用环境变量。
    @model_validator(mode="after")
    def valid(self):
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("来源角色不得重复")
        if self.kind == "api":
            url = urlsplit(self.base_url)
            if (
                not self.model.strip()
                or not self.api_key_env
                or self.web_provider
                or url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("API需模型/密钥环境变量及无凭据HTTP(S)地址，不混网页字段")
        elif (
            self.model
            or self.base_url
            or self.api_key_env
            or (self.kind == "web") != bool(self.web_provider)
        ):
            raise ValueError("mock/web配置不得混入API字段，web须指定已配置提供方")
        return self

    # 功能：生成仅供本次来源使用的采样/价格配置，不修改全局学习模型或把密钥写入清单。
    def settings(self):
        return runtime_settings().model_copy(
            update={
                "llm_model": self.model,
                "llm_base_url": self.base_url,
                "llm_api_key": os.environ.get(self.api_key_env, ""),
                "llm_temperature": self.temperature,
                "llm_max_output_tokens": self.max_output_tokens,
                "llm_input_usd_per_million": self.input_usd_per_million,
                "llm_output_usd_per_million": self.output_usd_per_million,
            }
        )

    # 功能：导出无秘密身份及配置摘要；网页仅声明身份，不认证实际后台模型，配置变化隔离旧工作项。
    def descriptor(self, engine):
        if self.kind == "web":
            adapter = WebModelService(engine).adapter(self.web_provider)
            identity = {
                "provider": self.web_provider,
                "model": None,
                "endpoint_sha256": digest(adapter.config.url),
                "web_config_sha256": digest(adapter.config.model_dump(mode="json")),
            }
            fresh = bool(adapter.config.new_chat_selector)
        else:
            llm = MockLLM() if self.kind == "mock" else None
            identity = (
                model_identity(llm)
                if llm
                else {
                    "provider": "openai_compatible",
                    "model": self.model,
                    "endpoint_sha256": digest(self.base_url.rstrip("/")),
                    "sampling_sha256": digest([self.temperature, self.max_output_tokens]),
                }
            )
            fresh = True
        return {
            "id": self.id,
            "kind": self.kind,
            "enabled": self.enabled,
            "roles": self.roles,
            "identity": identity,
            "config_sha256": digest(self.model_dump(mode="json")),
            "credential_env": self.api_key_env or None,
            "fresh_chat_required": self.kind == "web",
            "fresh_chat_configured": fresh,
            "evidence_level": 0,
        }

    # 功能：构造一次性API/Mock审计模型；仅显式执行阶段调用，不替换日常模型。
    def build(self, engine):
        settings = self.settings()
        inner = MockLLM() if self.kind == "mock" else OpenAICompatibleLLM(settings)
        return ObservedLLM(inner, engine, role="cognitive_review", settings=settings)


class SourceCatalog(BaseModel):
    model_config = {"extra": "forbid"}
    protocol: Literal["cognitive_sources_v1"] = "cognitive_sources_v1"
    sources: list[CognitiveSource] = Field(min_length=1, max_length=12)

    # 功能：保证人工配置来源ID唯一，避免同名选择静默覆盖。
    @model_validator(mode="after")
    def unique(self):
        if len({s.id for s in self.sources}) != len(self.sources):
            raise ValueError("认知来源ID重复")
        return self

    # 功能：仅按人工显式ID取来源，不自动发现账户、扩张网页权限或选更多模型。
    def get(self, source_id):
        for source in self.sources:
            if source.id == source_id:
                return source
        raise ValueError("认知来源ID未配置")


# 功能：有界读取JSON清单；路径由本地配置决定，不接受网页请求的任意文件路径。
def read_catalog(*, auxiliary=None):
    """合并原人工清单与已启用自动网页来源；人工采集不冒充可执行的独立盲答来源。"""
    from autodidact.web_models.auxiliary_config import read_registry

    path = Path(runtime_settings().cognitive_sources_config)
    sources = []
    if path.exists():
        if not path.is_file() or path.stat().st_size > 65536:
            raise ValueError("请先配置不超过64KiB的COGNITIVE_SOURCES_CONFIG清单")
        sources = SourceCatalog.model_validate_json(path.read_text(encoding="utf-8-sig")).sources
    registry = auxiliary if auxiliary is not None else read_registry()[0]
    sources.extend(
        CognitiveSource(id=s.id, kind="web", enabled=True, web_provider=s.id)
        for s in registry.sources if s.enabled and s.mode == "browser"
    )
    if not sources:
        raise ValueError("请配置认知来源清单，或在辅助来源页启用网页提问来源")
    return SourceCatalog(sources=sources)


# 功能：只读汇总精确来源指纹/角色/主题的运营样本，Profile只作有条件参考；未知退回人工fallback。
async def route_sources(repo, catalog, engine, role, topic, fallback):
    candidates = [s for s in catalog.sources if s.enabled and role in s.roles]
    if fallback not in {s.id for s in candidates}:
        raise ValueError("fallback须是该角色已启用的来源ID")
    entries = []
    for source in candidates:
        descriptor = source.descriptor(engine)
        observations = list(
            (
                await repo.s.execute(
                    select(
                        models.ModelObservation.id,
                        models.ModelObservation.metadata_json["error"].astext.label("error"),
                    )
                    .where(
                        models.ModelObservation.metadata_json["cognitive_source_sha256"].astext
                        == digest(descriptor),
                        models.ModelObservation.metadata_json["cognitive_role"].astext == role,
                        models.ModelObservation.metadata_json["cognitive_topic"].astext == topic,
                    )
                    .order_by(models.ModelObservation.created_at.desc(), models.ModelObservation.id)
                    .limit(201)
                )
            ).all()
        )
        bounded = observations[:200]
        failures = sum(bool(o.error) for o in bounded)
        identity = descriptor["identity"]
        profiles = list(
            (
                await repo.s.execute(
                    select(
                        models.ModelProfile.id,
                        models.ModelProfile.sample_count,
                        models.ModelProfile.benchmark_score,
                    )
                    .where(
                        models.ModelProfile.provider == identity["provider"],
                        models.ModelProfile.model_name == (identity["model"] or "unknown"),
                    )
                    .limit(6)
                )
            ).all()
        )
        entries.append(
            {
                "source": descriptor,
                "calls": len(bounded),
                "failures": failures,
                "truncated": len(observations) > 200,
                "success_fraction": (len(bounded) - failures) / len(bounded) if bounded else None,
                "profiles": [
                    {
                        "id": str(p.id),
                        "sample_count": p.sample_count,
                        "benchmark_score": p.benchmark_score
                        if p.benchmark_score is not None and math.isfinite(p.benchmark_score)
                        else None,
                    }
                    for p in profiles[:5]
                ],
                "profiles_truncated": len(profiles) > 5,
                "profile_note": "旧Profile缺端点/严格可比条件，不据此跨模型排真值分；调用成功也非答案正确。",
            }
        )
    known = all(e["calls"] >= 3 for e in entries)
    selected = (
        max(entries, key=lambda e: (e["success_fraction"], e["source"]["id"] == fallback))[
            "source"
        ]["id"]
        if known
        else fallback
    )
    return {
        "role": role,
        "topic": topic,
        "recommended_source": selected,
        "fallback": fallback,
        "basis": "最近有界同指纹运营成功率（非能力/正确率）"
        if known
        else "样本不足，保留人工fallback",
        "candidates": entries,
        "applied": False,
        "note": "只输出可回退建议，不修改配置/冻结清单，不联网探测；别名/模型共用训练数据不等于独立证据。",
    }


# 功能：追踪库内明确引用和同会话观察，关联不冒充生成因果，缺失历史身份保留未知。
async def cognitive_provenance(repo, kind, entity_id):
    model = {
        "claim": models.Claim,
        "belief": models.Belief,
        "observation": models.ModelObservation,
    }[kind]
    row = await repo.s.get(model, entity_id)
    if row is None:
        raise ValueError("认知记录不存在")
    if kind == "observation":
        meta = row.metadata_json or {}
        return {
            "kind": kind,
            "id": str(row.id),
            "provider": row.provider,
            "displayed_model": row.displayed_model,
            "access_type": row.access_type,
            "response_hash": row.response_hash,
            "context": {
                k: v
                for k, v in meta.items()
                if k.startswith("cognitive_")
                or k
                in {
                    "session_id",
                    "phase",
                    "role",
                    "error",
                    "evidence_level",
                    "operation_batch_id",
                    "model_identity",
                }
            },
            "evidence_level": 0,
        }
    claim_ids = (
        [str(row.id)]
        if kind == "claim"
        else (row.metadata_json or {}).get("promoted_claim_ids", [])
    )
    if not isinstance(claim_ids, list):
        claim_ids = []
    ids = []
    for value in claim_ids[:21]:
        try:
            ids.append(UUID(str(value)))
        except (ValueError, TypeError):
            continue
    claims = list(
        (await repo.s.scalars(select(models.Claim).where(models.Claim.id.in_(ids[:20])))).all()
    )
    sessions = {str(c.learning_session_id) for c in claims if c.learning_session_id}
    observations = (
        list(
            (
                await repo.s.scalars(
                    select(models.ModelObservation)
                    .where(models.ModelObservation.metadata_json["session_id"].astext.in_(sessions))
                    .order_by(models.ModelObservation.created_at.desc(), models.ModelObservation.id)
                    .limit(51)
                )
            ).all()
        )
        if sessions
        else []
    )
    anchors = list(
        (
            await repo.s.scalars(
                select(models.ClaimEvidence)
                .where(models.ClaimEvidence.claim_id.in_([c.id for c in claims]))
                .order_by(models.ClaimEvidence.id)
                .limit(101)
            )
        ).all()
    )
    evidence = (
        list(
            (
                await repo.s.scalars(
                    select(models.Evidence)
                    .where(models.Evidence.belief_id == row.id)
                    .order_by(models.Evidence.id)
                    .limit(101)
                )
            ).all()
        )
        if kind == "belief"
        else []
    )
    return {
        "kind": kind,
        "id": str(row.id),
        "claims": [
            {
                "id": str(c.id),
                "session_id": str(c.learning_session_id) if c.learning_session_id else None,
                "scope": c.scope,
            }
            for c in claims
        ],
        "associated_observations": [
            await cognitive_provenance(repo, "observation", o.id) for o in observations[:50]
        ],
        "claim_anchors": [
            {
                "id": str(a.id),
                "claim_id": str(a.claim_id),
                "source_id": str(a.source_id),
                "status": a.status,
            }
            for a in anchors[:100]
        ],
        "belief_evidence": [
            {
                "id": str(e.id),
                "source_id": str(e.source_id) if e.source_id else None,
                "claim_evidence_id": str(e.claim_evidence_id) if e.claim_evidence_id else None,
                "stance": e.stance,
                "evidence_level": e.evidence_level,
            }
            for e in evidence[:100]
        ],
        "truncated": len(claim_ids) > 20
        or len(observations) > 50
        or len(anchors) > 100
        or len(evidence) > 100,
        "note": "promoted_claim_ids是显式引用；同session观察仅关联，不认证具体句子的生成模型或事实支持。缺失旧身份不猜测回填，原文/信念审计请沿ID查看。",
    }
