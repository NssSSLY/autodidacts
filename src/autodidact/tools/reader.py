from __future__ import annotations

import hashlib

from bs4 import BeautifulSoup

from autodidact.config import agent_config
from autodidact.knowledge.sources import (
    RuleBasedSourceQualityClassifier,
    normalize_url,
    publisher_key,
)
from autodidact.schemas import SourceDocument


class WebReader:
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

    async def read(self, url: str) -> SourceDocument:
        from urllib.parse import urljoin

        from autodidact.tools.safe_fetch import fetch_public

        budget = getattr(self, "budget", None)
        batch = await budget.reserve({"web_reads": 1}, {"url": url[:2000]}) if budget else None
        try:
            final_url, headers, body, encoding = await fetch_public(url)
            content_type = headers.get("content-type", "").lower()
            if "text/html" not in content_type and "text/plain" not in content_type:
                raise ValueError(f"Unsupported content type: {content_type}")
            decoded = body.decode(encoding, errors="replace")
            lineage = normalize_url(final_url)
            if "text/html" in content_type:
                soup = BeautifulSoup(decoded, "html.parser")
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
                metadata={
                    "canonical_url": lineage,
                    "bytes": len(body),
                    "content_type": content_type,
                },
            )
        except Exception as exc:
            if budget:
                await budget.finish(batch, error=type(exc).__name__)
            raise
        if budget:
            await budget.finish(batch)
        return doc

    def with_budget(self, engine):
        from autodidact.runtime import OperationBudget

        self.budget = OperationBudget(engine)
        return self

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
