# 文件职责：根据已读正文的可定位信号保守分类内容质量并提供证据等级上限，不认证论文或证明主张。
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict

from autodidact.config import agent_config
from autodidact.knowledge.bibliography import publication_datetime, research_identifier
from autodidact.knowledge.sources import RuleBasedSourceQualityClassifier, SourceAssessment
from autodidact.schemas import SourceDocument

CONTENT_QUALITY_PROTOCOL = "content_quality_v1"
MODEL_TYPES = {"model", "model_output", "model_observation", "web_model"}
SIGNALS = {
    "methods": r"(?im)^\s*(?:\d+[.、\s]*)?(?:methods?|methodology|materials and methods|方法|研究方法|实验方法)(?:\s|[:：]|$)",
    "results": r"(?im)^\s*(?:\d+[.、\s]*)?(?:results?|findings|结果|实验结果)(?:\s|[:：]|$)",
    "references": r"(?im)^\s*(?:\d+[.、\s]*)?(?:references|bibliography|参考文献)(?:\s|[:：]|$)",
    "limitations": r"(?im)^\s*(?:\d+[.、\s]*)?(?:limitations?|局限性|局限|研究限制)(?:\s|[:：]|$)",
    "normative": r"\b(?:MUST NOT|MUST|SHALL NOT|SHALL)\b|必须|应当",
    "citations": r"(?m)10\.\d{4,9}/[^\s<>]+|^\s*\[\d{1,3}\]\s*\S.{10,}",
    "retracted": r"(?im)^\s*(?:retraction notice|retracted article|this (?:article|paper) (?:has been|is) retracted|撤稿声明|本文已撤稿|该论文已撤稿)(?:\s|[:：.!。]|$)",
}


# 功能：复用项目来源等级配置，保证读取、入库和旧证据门控使用同一策略。
def configured_classifier():
    policy = agent_config().source_policy
    return RuleBasedSourceQualityClassifier(
        model_level=0,
        ordinary_web_level=policy.ordinary_web_evidence_level,
        high_quality_secondary_level=policy.high_quality_secondary_level,
        textbook_level=policy.textbook_level,
        primary_or_official_level=policy.primary_or_official_level,
        proof_or_experiment_level=policy.proof_or_reproducible_experiment_level,
    )


# 功能：以正文锚点、书目完整性和 URL 初分共同给出有上限的质量观察；少量摘要或撤稿不具高等级。
def assess_content(doc: SourceDocument, classifier=None):
    classifier = classifier or configured_classifier()
    base = classifier.assess(doc.url, doc.source_type)
    text = doc.text
    signals = {}
    for name, pattern in SIGNALS.items():
        match = re.search(pattern, text)
        if match:
            start, end = match.span()
            signals[name] = {
                "start": start,
                "end": end,
                "excerpt": text[max(0, start - 50) : min(len(text), end + 120)],
            }
    bibliography = bool(doc.authors and publication_datetime(doc.published_date))
    identified = any(
        research_identifier(item.kind, item.value) for item in doc.research_identifiers
    )
    structured_research = all(
        name in signals for name in ("methods", "results", "references", "citations")
    )
    referenced = all(name in signals for name in ("references", "citations"))
    if doc.source_type.strip().casefold() in MODEL_TYPES:
        assessment = SourceAssessment(
            "model_output", 0, 0.0, "模型回答始终为等级0，正文格式和自报标识不能提高证据等级"
        )
    elif "retracted" in signals:
        assessment = SourceAssessment(
            "retraction_flagged",
            0,
            0.0,
            "正文存在可定位撤稿声明，暂不作为支持证据；这不是远程撤稿认证",
        )
    elif len(text.strip()) < 200:
        assessment = SourceAssessment(
            "insufficient_content",
            min(1, base.evidence_level),
            0.2,
            "正文不足200字符，可能仅为摘要/登录壳，域名不补足内容",
        )
    elif doc.source_type.strip().casefold() == "local_document":
        assessment = SourceAssessment(
            "unreviewed_local_document", 1, 0.3, "本地文档已记录正文信号，但未经独立核源，等级上限1"
        )
    elif structured_research and bibliography and identified:
        level = min(
            3,
            classifier.textbook_level,
            max(classifier.high_quality_secondary_level, base.evidence_level),
        )
        assessment = SourceAssessment(
            "structured_research",
            level,
            min(0.7, max(0.5, base.credibility_score)),
            "正文含方法、结果、参考文献及引文，并有自报作者/日期/作品标识；最高3，不推断同行评审或实验复现",
        )
    elif base.quality_class == "official_standard" and "normative" in signals and referenced:
        assessment = SourceAssessment(
            "normative_document",
            min(3, base.evidence_level),
            0.7,
            "标准路径和正文规范性条款/参考文献相符；未做正式标准/证明验真，最高3",
        )
    elif referenced and bibliography:
        assessment = SourceAssessment(
            "referenced_article",
            min(2, classifier.high_quality_secondary_level),
            0.5,
            "正文含可定位参考文献和引文，具作者/日期声明；最多二手资料等级2",
        )
    else:
        assessment = SourceAssessment(
            "unreviewed_content",
            min(1, base.evidence_level),
            min(0.3, base.credibility_score),
            "正文质量信号不足，官方/学术域名或 DOI 声明不能单独放行",
        )
    audit = {
        "protocol": CONTENT_QUALITY_PROTOCOL,
        "body_hash": hashlib.sha256(text.encode()).hexdigest(),
        "url_assessment": asdict(base),
        "signals": signals,
        "bibliography_present": bibliography,
        "research_identifier_present": identified,
        "decision": asdict(assessment),
        "unknown": ["作者身份认证", "出版日期认证", "同行评审", "远程撤稿状态", "实验复现"],
        "note": "规则分类是可审计质量观察，不是事实或主张支持证明",
    }
    return assessment, audit


# 功能：为读取/入库文档附加内容级分类和审计，替换 URL 预估等级，不修改正文或书目观察。
def classify_document(doc: SourceDocument, classifier=None):
    assessment, audit = assess_content(doc, classifier)
    return doc.model_copy(
        update={
            "quality_class": assessment.quality_class,
            "quality_reason": assessment.reason,
            "evidence_level": assessment.evidence_level,
            "credibility_score": assessment.credibility_score,
            "metadata": {**doc.metadata, "quality_audit": audit},
        }
    )


# 功能：在晋升/决议读取旧来源时重新计算正文上限，只降不升旧等级，不改写既有信念或学习历史。
def effective_source_assessment(source):
    metadata = source.metadata_json if isinstance(source.metadata_json, dict) else {}
    bibliography = metadata.get("bibliography", {})
    bibliography = bibliography if isinstance(bibliography, dict) else {}
    try:
        doc = SourceDocument(
            url=source.url or "",
            text=source.extracted_text or "",
            source_type=source.source_type or "web",
            authors=bibliography.get("authors", []),
            published_date=bibliography.get("published_date", ""),
            research_identifiers=bibliography.get("research_identifiers", []),
        )
    except (ValueError, TypeError):
        return SourceAssessment(
            "invalid_bibliography", 0, 0.0, "历史书目结构无效，保留记录但不放行支持等级"
        )
    assessment, _ = assess_content(doc)
    return SourceAssessment(
        assessment.quality_class,
        min(source.evidence_level, assessment.evidence_level),
        min(source.credibility_score, assessment.credibility_score),
        assessment.reason + f"；既存等级{source.evidence_level}只降不升",
    )
