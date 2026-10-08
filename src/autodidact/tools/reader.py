# 文件职责：安全读取网页、提取正文及质量/血缘 metadata，并可附加网页预算。
from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from autodidact.config import agent_config
from autodidact.knowledge.bibliography import extract_bibliography, identifier_links
from autodidact.knowledge.content_quality import classify_document
from autodidact.knowledge.sources import (
    RuleBasedSourceQualityClassifier,
    normalize_url,
    publisher_key,
)
from autodidact.provider_runtime import (
    ProviderFailure,
    ProviderIdentity,
    ProviderRuntime,
    classify_failure,
    reject_challenge,
)
from autodidact.schemas import SourceDocument


class WebReader:
    # 功能：按来源等级配置创建质量分类器，初始化可选预算为未绑定。
    def __init__(self):
        policy = agent_config().source_policy
        self.quality_classifier = RuleBasedSourceQualityClassifier(
            model_level=policy.model_output_evidence_level,
            ordinary_web_level=policy.ordinary_web_evidence_level,
            high_quality_secondary_level=policy.high_quality_secondary_level,
            textbook_level=policy.textbook_level,
            primary_or_official_level=policy.primary_or_official_level,
            proof_or_experiment_level=policy.proof_or_reproducible_experiment_level,
        )
        self.runtime = ProviderRuntime()

    # 功能：按来源入口origin熔断及有界重试；每次尝试仍重新校验公网/重定向并单独计预算。
    async def read(self, url: str) -> SourceDocument:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        identity = ProviderIdentity("source", "public_web", origin)

        # 功能：把一次完整安全读取和解析绑定统一调用及尝试标识。
        async def attempt(context):
            return await self._attempt(url, context)

        return await self.runtime.run(identity, attempt)

    # 功能：预算准入后单次读取/解析/质量审计，验证码页明确失败；保存失败分类和取消账本。
    async def _attempt(self, url, context):
        from urllib.parse import urljoin

        from autodidact.tools.safe_fetch import fetch_public

        budget = getattr(self, "budget", None)
        batch = (
            await budget.reserve({"web_reads": 1}, {"url": url[:2000], **context})
            if budget
            else None
        )
        try:
            final_url, headers, body, encoding = await fetch_public(url)
            content_type = headers.get("content-type", "").lower()
            if "text/html" not in content_type and "text/plain" not in content_type:
                raise ProviderFailure("unsupported_content")
            decoded = body.decode(encoding, errors="replace")
            if "text/html" in content_type:
                reject_challenge(decoded)
            lineage = normalize_url(final_url)
            lineage_links = []
            bibliography = {
                "authors": [],
                "published_date": "",
                "research_identifiers": [],
                "audit": {},
            }
            if "text/html" in content_type:
                soup = BeautifulSoup(decoded, "html.parser")
                from autodidact.knowledge.lineage import extract_lineage

                lineage_links = extract_lineage(soup, final_url, url)
                bibliography = extract_bibliography(soup, final_url)
                lineage_links.extend(
                    identifier_links(final_url, bibliography["research_identifiers"])
                )
                canonical = soup.find("link", rel="canonical")
                if canonical and canonical.get("href"):
                    lineage = normalize_url(urljoin(final_url, canonical["href"]))
                for tag in soup(["script", "style", "noscript", "nav", "footer"]):
                    tag.decompose()
                title = soup.title.get_text(" ", strip=True) if soup.title else url
                text = "\n".join(
                    line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
                )
            else:
                title, text = url, decoded
            normalized = normalize_url(final_url)
            assessment = self.quality_classifier.assess(normalized, "web")
            doc = SourceDocument(
                url=final_url,
                normalized_url=normalized,
                publisher_key=publisher_key(normalized),
                lineage_key=lineage,
                title=title,
                text=text[: agent_config().learning.max_chars_per_source],
                quality_class=assessment.quality_class,
                quality_reason=assessment.reason,
                evidence_level=min(4, assessment.evidence_level),
                credibility_score=assessment.credibility_score,
                authors=bibliography["authors"],
                published_date=bibliography["published_date"],
                research_identifiers=bibliography["research_identifiers"],
                metadata={
                    "bibliography": bibliography,
                    "canonical_url": lineage,
                    "lineage_links": lineage_links,
                    "bytes": len(body),
                    "content_type": content_type,
                },
            )
            doc = classify_document(doc, self.quality_classifier)
        except (Exception, asyncio.CancelledError) as exc:
            if budget:
                await budget.finish(
                    batch,
                    error=type(exc).__name__,
                    details={"failure_category": classify_failure(exc).category},
                )
            raise
        if budget:
            await budget.finish(batch)
        return doc

    # 功能：附加数据库预算账本并返回自身，供控制器组装调用链。
    def with_budget(self, engine):
        from autodidact.runtime import OperationBudget

        self.budget = OperationBudget(engine)
        self.runtime = ProviderRuntime(engine)
        return self

    # 功能：对提取文本求 SHA-256，作为来源内容去重键。
    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
