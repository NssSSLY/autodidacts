from __future__ import annotations

import logging

from autodidact.runtime import BudgetExceeded
from autodidact.tools.reader import WebReader

log = logging.getLogger(__name__)


class ResearchCollector:
    """Shared retrieval path for learning, independent verification and disputes."""

    def __init__(self, search, reader=None, repo=None):
        self.repo = repo
        self.search = search
        self.reader = reader or WebReader()

    async def fetch(self, query: str, *, exclude=(), limit: int = 3):
        from autodidact.knowledge.sources import normalize_url, publisher_key

        urls = {normalize_url(d.url) for d in exclude}
        hashes = {WebReader.hash_text(d.text) for d in exclude}
        publishers = {d.publisher_key or publisher_key(d.url) for d in exclude}
        docs = []
        excluded_rows = []
        if self.repo:
            for document in exclude:
                excluded_rows.append(
                    await self.repo.upsert_source(document, WebReader.hash_text(document.text))
                )
        try:
            hits = await self.search.search(query, limit=8)
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate search-provider failures.
            log.warning("检索降级：%s", type(exc).__name__)
            return []
        for hit in hits:
            if normalize_url(hit.url) in urls or publisher_key(hit.url) in publishers:
                continue
            try:
                doc = await self.reader.read(hit.url)
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - isolate individual reader failures.
                log.debug("跳过不可读来源：%s", type(exc).__name__)
                continue
            digest = WebReader.hash_text(doc.text)
            if digest in hashes or normalize_url(doc.url) in urls:
                continue
            if exclude and (
                doc.publisher_key in publishers
                or doc.lineage_key in {d.lineage_key or normalize_url(d.url) for d in exclude}
            ):
                continue
            urls.add(normalize_url(doc.url))
            hashes.add(digest)
            if self.repo:
                stored = await self.repo.upsert_source(doc, WebReader.hash_text(doc.text))
                all_sources = await self.repo.lineage.evidence_sources([*excluded_rows, stored])
                from autodidact.knowledge.sources import independent_source_representatives

                if excluded_rows and len(independent_source_representatives(all_sources)) <= len(
                    independent_source_representatives(all_sources[:-1])
                ):
                    continue
                excluded_rows.append(stored)
            docs.append(doc)
            if len(docs) >= limit:
                break
        return docs

    async def filter_independent(self, docs, exclude):
        if not self.repo or not exclude:
            return docs
        from autodidact.knowledge.sources import independent_source_representatives

        excluded = [await self.repo.upsert_source(d, WebReader.hash_text(d.text)) for d in exclude]
        accepted = []
        for doc in docs:
            source = await self.repo.upsert_source(doc, WebReader.hash_text(doc.text))
            evidence = await self.repo.lineage.evidence_sources([*excluded, source])
            if len(independent_source_representatives(evidence)) > len(
                independent_source_representatives(evidence[:-1])
            ):
                accepted.append(doc)
                excluded.append(source)
        return accepted
