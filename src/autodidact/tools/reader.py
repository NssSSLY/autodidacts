from __future__ import annotations

import hashlib

import httpx
from bs4 import BeautifulSoup

from autodidact.config import agent_config, runtime_settings
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
        headers = {"User-Agent": runtime_settings().http_user_agent}
        async with httpx.AsyncClient(timeout=35, headers=headers, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
        content_type = r.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            raise ValueError(f"Unsupported content type: {content_type}")
        if "text/html" in content_type:
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "noscript", "nav", "footer"]):
                tag.decompose()
            title = soup.title.get_text(" ", strip=True) if soup.title else url
            text = "\n".join(
                line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
            )
        else:
            title, text = url, r.text
        final_url = str(r.url)
        normalized = normalize_url(final_url)
        assessment = self.quality_classifier.assess(normalized, "web")
        limit = agent_config().learning.max_chars_per_source
        return SourceDocument(
            url=final_url,
            normalized_url=normalized,
            publisher_key=publisher_key(normalized),
            title=title,
            text=text[:limit],
            quality_class=assessment.quality_class,
            quality_reason=assessment.reason,
            evidence_level=assessment.evidence_level,
            credibility_score=assessment.credibility_score,
        )

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
