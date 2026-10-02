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
                await self.repo.upsert_source(doc, WebReader.hash_text(doc.text))
            docs.append(doc)
            if len(docs) >= limit:
                break
        return docs
