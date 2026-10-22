# 文件职责：定义F12方法快照、文字适用边界、显式启用门控与续跑代数，保留旧未纳管技能兼容行为。
from __future__ import annotations

import json
from uuid import UUID

from autodidact import models
from autodidact.brain.llm import MockLLM
from autodidact.config import runtime_settings
from autodidact.learning.ground_truth import digest, model_identity, text_key

PROTOCOL = "skill_methods_v1"


# 功能：给JSON报告附协议/摘要并限制2MiB/有限数值，不把截断内容当完整冻结依据。
def method_payload(value):
    payload = {"protocol": PROTOCOL, **value}
    if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()) > 2 * 1024 * 1024:
        raise ValueError("方法报告超过2MiB，拒绝截断后保存")
    return {**payload, "sha256": digest(payload)}


# 功能：校验F12不可变报告，未知协议/摘要变化拒绝静默降级或复用。
def check_method(payload):
    if payload.get("protocol") != PROTOCOL or digest(
        {k: v for k, v in payload.items() if k != "sha256"}
    ) != payload.get("sha256"):
        raise ValueError("方法报告协议或完整性摘要不一致")
    return payload


# 功能：冻结方法正文/原出处/生命周期，不将使用次数、经验置信度或启用指针变化当正文变化。
def skill_snapshot(skill):
    return {
        "id": str(skill.id),
        "name": skill.name,
        "description": skill.description,
        "trigger": skill.trigger_condition,
        "procedure": skill.procedure,
        "version": skill.version,
        "lifecycle_state": skill.lifecycle_state,
        "lifecycle_version": skill.lifecycle_version,
        "canonical_id": str(skill.canonical_id) if skill.canonical_id else None,
        "source_session_ids": (skill.metadata_json or {}).get("source_session_ids", []),
    }


# 功能：按人工显式文字包含/排除词筛适用范围，排除优先；不是语义条件证明或模型自主执行权限。
def method_matches(spec, title):
    value = text_key(title)
    return any(text_key(t) in value for t in spec["include_terms"]) and not any(
        text_key(t) in value for t in spec["exclude_terms"]
    )


# 功能：从当前学习模型配置生成无秘密身份，不构造网络客户端或要求实际凭据。
def learner_identity():
    settings = runtime_settings()
    if settings.llm_provider == "mock":
        return model_identity(MockLLM())
    if settings.llm_provider != "openai_compatible":
        raise ValueError("当前学习模型提供方不支持方法实验")
    return {
        "provider": "openai_compatible",
        "model": settings.llm_model,
        "endpoint_sha256": digest(settings.llm_base_url.rstrip("/")),
        "sampling_sha256": digest([settings.llm_temperature, settings.llm_max_output_tokens]),
    }


# 功能：读取F12专用代数，未发生管理操作时为0并保留原续跑身份；非法旧值拒绝绕过。
async def skill_method_epoch(repo):
    row = await repo.get_report("skill_method_epoch", "current")
    if row is None:
        return 0
    if (
        row.payload.get("protocol") != PROTOCOL
        or type(row.payload.get("revision")) is not int
        or row.payload["revision"] < 1
    ):
        raise ValueError("方法管理代数无效，拒绝盲续跑")
    return row.payload["revision"]


# 功能：在调用者持控制器锁的同一事务中推进方法代数，不独立提交或改旧规划/工作项。
async def advance_method_epoch(repo):
    revision = await skill_method_epoch(repo) + 1
    row = await repo.get_report("skill_method_epoch", "current")
    value = {"protocol": PROTOCOL, "revision": revision}
    if row:
        row.payload = value
    else:
        repo.s.add(
            models.ResearchReport(kind="skill_method_epoch", report_key="current", payload=value)
        )
    return revision


# 功能：读取当前已纳管定义并核对正文/生命周期快照，不将过期定义当新方法或恢复归档技能。
async def current_method(repo, skill):
    meta = skill.metadata_json or {}
    if meta.get("method_protocol") != PROTOCOL or not meta.get("method_definition_id"):
        raise ValueError("技能尚未注册F12适用条件/出处定义")
    row = await repo.s.get(models.ResearchReport, UUID(meta["method_definition_id"]))
    if row is None or row.kind != "skill_method_definition":
        raise ValueError("方法定义不存在")
    payload = check_method(row.payload)
    if payload["snapshot"] != skill_snapshot(skill) or payload["skill_id"] != str(skill.id):
        raise ValueError("方法正文/出处/生命周期已变更，请注册新定义保留旧历史")
    return row


# 功能：规划选择时复核显式启用报告/模型/范围/反例版本；未知管理协议不回退成旧式自动采用。
async def eligible_method(repo, skill, title):
    meta = skill.metadata_json or {}
    if "method_protocol" not in meta:
        return True
    if (
        meta.get("method_protocol") != PROTOCOL
        or meta.get("method_enabled") is not True
        or skill.lifecycle_state != "active"
    ):
        return False
    try:
        definition = await current_method(repo, skill)
        event = await repo.s.get(models.ResearchReport, UUID(meta["method_activation_id"]))
        if event is None or event.kind != "skill_method_decision":
            return False
        value = check_method(event.payload)
        return (
            value["operation"] == "activate"
            and value["definition_id"] == str(definition.id)
            and value["case_revision"] == meta.get("method_case_revision", 0)
            and value["model_identity"] == learner_identity()
            and method_matches(definition.payload["spec"], title)
        )
    except (ValueError, KeyError, TypeError):
        return False


# 功能：仅把已选方法的正文/条件/局限送给规划器，不传训练答卷/反例题面/实验真值。
async def planning_method(repo, skill):
    value = {"name": skill.name, "procedure": skill.procedure}
    if (skill.metadata_json or {}).get("method_protocol") == PROTOCOL:
        definition = await current_method(repo, skill)
        spec = definition.payload["spec"]
        value.update(
            {
                "definition_id": str(definition.id),
                "applicability": spec["applicability"],
                "limitations": spec["limitations"],
                "include_terms": spec["include_terms"],
                "exclude_terms": spec["exclude_terms"],
            }
        )
    return value
