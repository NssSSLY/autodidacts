# 文件职责：有界提取来源自报作者、发布日期与研究标识，保留字段出处和冲突，不认证身份或追取链接。
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from autodidact.knowledge.lineage import safe_target
from autodidact.knowledge.sources import normalize_url

BIBLIOGRAPHY_PROTOCOL = "bibliography_declared_v1"
ARTICLE_TYPES = {
    "Article",
    "ScholarlyArticle",
    "NewsArticle",
    "BlogPosting",
    "Report",
    "CreativeWork",
}


# 功能：遍历有限 JSON-LD 文章节点，只接受当前网页身份的声明，忽略嵌套引用文章和无效数据。
def article_nodes(soup, url):
    for script in soup.find_all("script", type="application/ld+json", limit=10):
        raw = script.string or script.get_text()
        if len(raw) > 100000:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            continue
        nodes = data if isinstance(data, list) else [data]
        for item in nodes[:20]:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph", [item])
            for node in (graph if isinstance(graph, list) else [item])[:30]:
                if not isinstance(node, dict):
                    continue
                types = node.get("@type", [])
                types = types if isinstance(types, list) else [types]
                if not any(isinstance(t, str) and t in ARTICLE_TYPES for t in types):
                    continue
                identity = node.get("url", node.get("@id"))
                if identity is not None and safe_target(url, identity) != normalize_url(url):
                    continue
                yield node


# 功能：规范有限 DOI/arXiv/PMID 研究标识，拒绝无效或过长声明；不验证注册/论文真实性。
def research_identifier(kind, raw):
    if not isinstance(raw, str) or len(raw) > 500:
        return None
    value = raw.strip()
    if kind == "doi":
        value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.IGNORECASE)
        if not re.fullmatch(r"10\.\d{4,9}/[^\s<>\"#?]+", value):
            return None
        value = value.casefold().rstrip(".,;")
    elif kind == "arxiv":
        value = re.sub(
            r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", "", value, flags=re.IGNORECASE
        ).removesuffix(".pdf")
        if not re.fullmatch(
            r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", value, flags=re.IGNORECASE
        ):
            return None
        value = value.casefold()
    elif kind == "pmid":
        value = re.sub(r"^pmid:\s*", "", value, flags=re.IGNORECASE)
        if not re.fullmatch(r"[1-9]\d{0,9}", value):
            return None
    else:
        return None
    return {"kind": kind, "value": value} if len(value) <= 300 else None


# 功能：仅解析明确 ISO 日期/时间；年份、月份或不合法日期保持未知，不使用下载时间代替发布日期。
def publication_datetime(value):
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r"\d{4}[-/]\d{2}[-/]\d{2}", value):
            return datetime.strptime(value.replace("/", "-"), "%Y-%m-%d").replace(tzinfo=UTC)
        if not re.match(r"^\d{4}-\d{2}-\d{2}T", value):
            return None
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


# 功能：提取 meta/JSON-LD 的书目观察，日期冲突不选边；普通参考文献 DOI 不当成本页研究标识。
def extract_bibliography(soup, url):
    observations, authors, dates, identifiers = [], [], [], []

    # 功能：有界保存单个声明及字段出处，分别收集作者、日期和标识，不执行声明中的指令。
    def add(field, raw, origin, kind=None):
        if (
            not isinstance(raw, str)
            or not raw.strip()
            or len(raw) > 1000
            or len(observations) >= 100
        ):
            return
        raw = " ".join(raw.split())
        value = None
        if field == "authors" and len(raw) <= 300:
            value = raw
            if value not in authors and len(authors) < 30:
                authors.append(value)
        elif field == "published_date" and len(raw) <= 100:
            parsed = publication_datetime(raw)
            value = parsed.isoformat() if parsed else ""
            if value and value not in dates:
                dates.append(value)
        elif field == "research_identifiers":
            value = research_identifier(kind, raw)
            if value and value not in identifiers and len(identifiers) < 30:
                identifiers.append(value)
        observations.append(
            {"field": field, "raw": raw, "value": value, "origin": origin, "trust": "declared"}
        )

    for meta in soup.find_all("meta", limit=300):
        name = str(meta.get("name") or meta.get("property") or "").casefold()
        raw = meta.get("content", "")
        if name in {"citation_author", "author", "dc.creator", "dc.creator.personalname"}:
            add("authors", raw, "meta:" + name)
        elif name in {
            "citation_publication_date",
            "citation_date",
            "article:published_time",
            "dc.date",
            "dc.date.issued",
            "datepublished",
        }:
            add("published_date", raw, "meta:" + name)
        elif name in {"citation_doi", "dc.identifier.doi", "citation_arxiv_id", "citation_pmid"}:
            kind = "arxiv" if "arxiv" in name else "pmid" if "pmid" in name else "doi"
            add("research_identifiers", raw, "meta:" + name, kind)
        elif name == "dc.identifier" and isinstance(raw, str) and research_identifier("doi", raw):
            add("research_identifiers", raw, "meta:" + name, "doi")
    for node in article_nodes(soup, url):
        values = node.get("author", [])
        for author in (values if isinstance(values, list) else [values])[:30]:
            add(
                "authors",
                author.get("name") if isinstance(author, dict) else author,
                "jsonld:author",
            )
        add("published_date", node.get("datePublished"), "jsonld:datePublished")
        values = node.get("identifier", [])
        for item in (values if isinstance(values, list) else [values])[:30]:
            if isinstance(item, dict):
                kind = str(item.get("propertyID", "")).casefold()
                add(
                    "research_identifiers",
                    item.get("value"),
                    "jsonld:identifier:" + kind[:30],
                    kind,
                )
            elif isinstance(item, str):
                for kind in ("doi", "arxiv"):
                    if research_identifier(kind, item):
                        add("research_identifiers", item, "jsonld:identifier", kind)
                        break
    date_conflict = len(dates) > 1
    published = dates[0] if len(dates) == 1 else ""
    return {
        "authors": authors,
        "published_date": published,
        "research_identifiers": identifiers,
        "audit": {
            "protocol": BIBLIOGRAPHY_PROTOCOL,
            "source_url": url,
            "observations": observations,
            "date_conflict": date_conflict,
            "date_precision": "date_or_explicit_timezone" if published else "unknown",
            "note": "声明未认证；仅日历日期的数据库 UTC 零点是存储表示，不是已知出版时刻",
        },
    }


# 功能：将有效作品标识映射为同作品依赖边，只降低独立性；arXiv 版本折叠，普通引用不进入此函数。
def identifier_links(url, identifiers):
    links = []
    for item in identifiers[:30]:
        item = item.model_dump() if hasattr(item, "model_dump") else item
        valid = (
            research_identifier(item.get("kind"), item.get("value"))
            if isinstance(item, dict)
            else None
        )
        if not valid:
            continue
        kind, value = valid["kind"], valid["value"]
        target = {
            "doi": "https://doi.org/",
            "arxiv": "https://arxiv.org/abs/",
            "pmid": "https://pubmed.ncbi.nlm.nih.gov/",
        }[kind]
        target += re.sub(r"v\d+$", "", value) if kind == "arxiv" else value
        links.append(
            {
                "from_url": url,
                "to_url": target,
                "relation": "same_work",
                "signal": "declared_identifier:" + kind,
                "trust": "declared",
            }
        )
    return links
