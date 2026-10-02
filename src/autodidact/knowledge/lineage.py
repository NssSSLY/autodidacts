# 文件职责：从网页声明和观察提取来源依赖并持久化，传递合并同源而不提高质量等级。
"""可审计的来源依赖图；网页声明只能降低独立性，不能提高证据等级。"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from autodidact import models
from autodidact.knowledge.sources import EvidenceSource, normalize_url, publisher_key

DEPENDENCIES = {"canonical", "redirect", "same_work", "reprint", "derived_from", "identical"}


# 功能：将相对血缘链接转绝对 HTTP(S) URL，限制长度和凭据并统一 DOI 身份；不直接访问目标。
def safe_target(base, value):
    if not isinstance(value, str) or len(value) > 2000:
        return ""
    target = urljoin(base, value.strip())
    parsed = urlsplit(target)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        return ""
    if parsed.hostname.lower() in {"doi.org", "dx.doi.org"}:
        return "https://doi.org" + parsed.path.casefold().rstrip("/")
    return normalize_url(target)


# 功能：提取重定向、canonical、DOI/JSON-LD 和显式转载/引用信号，依赖与普通引用分开。
def extract_lineage(soup, url, requested_url=None):
    links = []

    # 功能：将单个合法链接与关系加入有数量上限的血缘列表，保留声明信号。
    def add(value, relation, signal):
        target = safe_target(url, value)
        if target and target != normalize_url(url) and len(links) < 100:
            item = {
                "from_url": normalize_url(url),
                "to_url": target,
                "relation": relation,
                "signal": signal,
                "trust": "declared",
            }
            if item not in links:
                links.append(item)

    if requested_url and normalize_url(requested_url) != normalize_url(url):
        links.append(
            {
                "from_url": normalize_url(requested_url),
                "to_url": normalize_url(url),
                "relation": "redirect",
                "signal": "observed_final_url",
                "trust": "observed",
            }
        )
    for link in soup.find_all("link"):
        rels = set(link.get("rel", []))
        if "canonical" in rels:
            add(link.get("href"), "canonical", "rel=canonical")
        elif rels.intersection({"original-source", "syndication-source"}):
            add(link.get("href"), "reprint", "original_source_link")
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or meta.get("property") or "").lower()
        content = meta.get("content", "")
        if name in {"original-source", "syndication-source"}:
            add(content, "reprint", name)
        elif name in {"citation_doi", "dc.identifier", "dc.identifier.doi"}:
            doi = re.search(r"10\.\d{4,9}/[^\s<>]+", content, re.IGNORECASE)
            if doi:
                add("https://doi.org/" + doi.group().rstrip(".,;"), "same_work", name)
        elif name == "citation_public_url":
            add(content, "same_work", name)
    for script in soup.find_all("script", type="application/ld+json", limit=10):
        try:
            raw = script.string or script.get_text()
            if len(raw) > 100000:
                continue
            data = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            continue
        nodes = data if isinstance(data, list) else [data]
        expanded = []
        for node in nodes[:20]:
            if isinstance(node, dict):
                expanded.extend(
                    node.get("@graph", [node])[:30]
                    if isinstance(node.get("@graph", [node]), list)
                    else [node]
                )
        for node in expanded:
            if not isinstance(node, dict):
                continue
            types = node.get("@type", [])
            types = types if isinstance(types, list) else [types]
            if not {t for t in types if isinstance(t, str)}.intersection(
                {
                    "Article",
                    "ScholarlyArticle",
                    "NewsArticle",
                    "BlogPosting",
                    "Report",
                    "CreativeWork",
                }
            ):
                continue
            identity = node.get("url", node.get("@id"))
            if isinstance(identity, str) and safe_target(url, identity) != normalize_url(url):
                continue
            for field, relation in [
                ("sameAs", "same_work"),
                ("isBasedOn", "derived_from"),
                ("citation", "cites"),
            ]:
                values = node.get(field, [])
                for value in (values if isinstance(values, list) else [values])[:30]:
                    if isinstance(value, dict):
                        value = value.get("url", value.get("@id", ""))
                    add(value, relation, "jsonld:" + field)
    # Only explicitly labelled origins collapse independence. An ordinary citation does not.
    origin = re.compile(
        r"转载自|转引自|编译自|原文(?:链接|地址)|original(?:ly)? (?:published|source)|reprinted from|adapted from",
        re.IGNORECASE,
    )
    citation = re.compile(r"引用|参考|references?|citations?", re.IGNORECASE)
    for anchor in soup.find_all("a", href=True):
        label = anchor.get_text(" ", strip=True)[:200]
        preceding = str(anchor.previous_sibling or "")[-120:]
        if origin.search(label) or origin.search(preceding):
            add(anchor["href"], "reprint", "explicit_origin_label:" + label[:100])
        elif citation.search(label) or "citation" in anchor.get("rel", []):
            add(anchor["href"], "cites", "citation_label:" + label[:100])
    return links


class SourceLineage:
    # 功能：绑定异步数据库会话，用于来源边和依赖查询。
    def __init__(self, session):
        self.s = session

    # 功能：将已取得的血缘信号/同内容别名去重写成 SourceLink，不主动沿链接爬取。
    async def record(self, source, doc):
        links = list(doc.metadata.get("lineage_links", []))
        for target, relation in [(doc.lineage_key, "canonical"), (source.url, "identical")]:
            if target and normalize_url(doc.url) != normalize_url(target):
                links.append(
                    {
                        "from_url": doc.url,
                        "to_url": target,
                        "relation": relation,
                        "signal": "content_hash" if relation == "identical" else "legacy_canonical",
                    }
                )
        for item in links[:102]:
            if not isinstance(item, dict) or item.get("relation") not in DEPENDENCIES | {"cites"}:
                continue
            left = safe_target(doc.url, item.get("from_url", doc.url))
            right = safe_target(doc.url, item.get("to_url", ""))
            if not left or not right or left == right:
                continue
            await self.s.execute(
                insert(models.SourceLink)
                .values(
                    source_id=source.id,
                    from_url=left,
                    to_url=right,
                    dedup_key=hashlib.sha256(
                        f"{left}\\n{right}\\n{item['relation']}".encode()
                    ).hexdigest(),
                    relation=item["relation"],
                    details={
                        "signal": str(item.get("signal", ""))[:300],
                        "trust": str(item.get("trust", "declared"))[:30],
                    },
                )
                .on_conflict_do_nothing(constraint="uq_source_links_relation")
            )

    # 功能：有界计算依赖边的无向传递闭包以识别同源链/环；普通 cites 不合并，截断保守处理。
    async def dependency_keys(self, urls):
        """Undirected transitive closure catches shared origins and cycles, without crawling.

        Citation edges remain visible in the graph but are deliberately not dependency edges.
        Bounds fail conservatively: incomplete lineage cannot grant extra independent votes.
        """
        seeds = {normalize_url(u) for u in urls if u}
        graph = {}
        frontier, visited = set(seeds), set()
        truncated = False
        for _ in range(32):
            if not frontier:
                break
            visited.update(frontier)
            edges = (
                await self.s.scalars(
                    select(models.SourceLink)
                    .where(
                        models.SourceLink.relation.in_(DEPENDENCIES),
                        or_(
                            models.SourceLink.from_url.in_(frontier),
                            models.SourceLink.to_url.in_(frontier),
                        ),
                    )
                    .order_by(models.SourceLink.id)
                    .limit(2001)
                )
            ).all()
            if len(edges) > 2000:
                truncated = True
            next_frontier = set()
            for edge in edges[:2000]:
                graph.setdefault(edge.from_url, set()).add(edge.to_url)
                graph.setdefault(edge.to_url, set()).add(edge.from_url)
                next_frontier.update((edge.from_url, edge.to_url))
            frontier = next_frontier - visited
            if len(visited) + len(frontier) > 10000:
                truncated = True
                break
        if frontier:
            truncated = True
        result = {}
        for seed in seeds:
            component, pending = set(), [seed]
            while pending:
                node = pending.pop()
                if node not in component:
                    component.add(node)
                    pending.extend(graph.get(node, ()) - component if node in graph else ())
            if truncated:
                component.add("lineage:incomplete")
            result[seed] = frozenset(component)
        return result

    # 功能：把 Source 实体及依赖键转换成 EvidenceSource，供晋升与决议独立性计算。
    async def evidence_sources(self, rows):
        urls = [s.normalized_url or s.url for s in rows]
        urls += [(s.metadata_json or {}).get("lineage_key", "") for s in rows]
        keys = await self.dependency_keys(urls)
        return [
            EvidenceSource(
                source_id=str(s.id),
                normalized_url=s.normalized_url or normalize_url(s.url),
                publisher_key=s.publisher_key or publisher_key(s.url),
                content_hash=s.content_hash or "",
                lineage_key=(s.metadata_json or {}).get("lineage_key", ""),
                dependency_keys=keys.get(normalize_url(s.url), frozenset())
                | keys.get((s.metadata_json or {}).get("lineage_key", ""), frozenset()),
                evidence_level=s.evidence_level,
                credibility_score=s.credibility_score,
            )
            for s in rows
        ]
