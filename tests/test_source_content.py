# 文件职责：离线验证书目提取、同作品标识、正文质量及来源持久审计，不访问网络或在线数据库。
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from bs4 import BeautifulSoup
from test_claim_scope import _MemorySession, _Result

from autodidact import models
from autodidact.knowledge.bibliography import (
    extract_bibliography,
    identifier_links,
    publication_datetime,
)
from autodidact.knowledge.content_quality import (
    assess_content,
    classify_document,
    effective_source_assessment,
)
from autodidact.knowledge.sources import RuleBasedSourceQualityClassifier
from autodidact.repository import Repository
from autodidact.schemas import SourceDocument
from autodidact.tools.reader import WebReader

URL = "https://research.example/article"
BODY = (
    "方法\n我们测量样本并公开测量协议。\n结果\n"
    + "样本存在误差边界，不能推广到未测量任务。" * 25
    + "\n局限性\n仅用于该样本。\n参考文献\n[1] Example study 10.1234/example"
)
HTML = (
    """<html><head><title>研究报告</title>
<meta name="citation_author" content="作者甲"><meta name="citation_author" content="作者乙">
<meta name="citation_publication_date" content="2024/06/05">
<meta name="citation_doi" content="https://doi.org/10.1234/Example">
<meta name="citation_arxiv_id" content="2401.12345v2">
<meta name="citation_pmid" content="12345678"></head><body>"""
    + BODY
    + "</body></html>"
)


# 功能：构造携带未认证书目声明的结构化正文测试文档。
def _doc(url=URL, **values):
    data = {
        "url": url,
        "text": BODY,
        "authors": ["作者甲"],
        "published_date": "2024-06-05",
        "research_identifiers": [{"kind": "doi", "value": "10.1234/example"}],
    }
    return SourceDocument(**{**data, **values})


# 功能：验证作者、日期、标识规范化并保留原始声明出处，正文引用标识不误当本页身份。
def test_bibliography_has_origins_and_does_not_infer_from_references():
    result = extract_bibliography(BeautifulSoup(HTML, "html.parser"), URL)
    assert result["authors"] == ["作者甲", "作者乙"]
    assert result["published_date"] == "2024-06-05T00:00:00+00:00"
    assert {item["kind"] for item in result["research_identifiers"]} == {"doi", "arxiv", "pmid"}
    assert all(
        item["origin"].startswith("meta:") and item["trust"] == "declared"
        for item in result["audit"]["observations"]
    )
    ordinary = extract_bibliography(
        BeautifulSoup("<p>参考 10.1234/someone-else</p>", "html.parser"), URL
    )
    assert ordinary["research_identifiers"] == [] and ordinary["published_date"] == ""


# 功能：日期冲突、过长声明、损坏JSON或非本页JSON节点保持未知，不影响正文读取。
def test_bibliography_conflicts_and_invalid_nodes_remain_unknown():
    html = (
        HTML
        + """<meta property="article:published_time" content="2025-01-01">
    <script type="application/ld+json">not-json</script>
    <script type="application/ld+json">{"@type":"ScholarlyArticle","url":"https://other.example/paper","author":"陌生作者"}</script>"""
    )
    result = extract_bibliography(BeautifulSoup(html, "html.parser"), URL)
    assert result["published_date"] == "" and result["audit"]["date_conflict"]
    assert "陌生作者" not in result["authors"]


# 功能：验证当前文章JSON-LD字段可提取，但引用对象的作者/标识和dateModified不成为本页出版信息。
def test_jsonld_extracts_only_article_identity():
    html = """<script type="application/ld+json">{"@graph":[{"@type":"Article",
    "url":"https://research.example/article", "author":{"name":"作者丙"},
    "datePublished":"2023-01-02T10:00:00Z", "dateModified":"2025-01-01",
    "identifier":{"propertyID":"DOI","value":"10.1234/work"},
    "citation":{"author":"引用作者","identifier":"10.1234/other"}}]}</script>"""
    result = extract_bibliography(BeautifulSoup(html, "html.parser"), URL)
    assert result["authors"] == ["作者丙"]
    assert result["published_date"] == "2023-01-02T10:00:00+00:00"
    assert result["research_identifiers"] == [{"kind": "doi", "value": "10.1234/work"}]


# 功能：不完整日期或无时区时间不猜测出版时刻。
@pytest.mark.parametrize(
    "value", ["2024", "2024-01", "2024-02-30", "yesterday", "2024-01-01T12:00:00"]
)
def test_publication_date_does_not_guess(value):
    assert publication_datetime(value) is None


# 功能：研究标识生成同作品依赖，不把arXiv不同版本或DOI大小写当独立作品。
def test_identifiers_are_dependency_keys_not_quality_proof():
    links = identifier_links(
        URL,
        [
            {"kind": "arxiv", "value": "2401.12345v2"},
            {"kind": "doi", "value": "10.1234/EXAMPLE"},
            {"kind": "pmid", "value": "bad"},
        ],
    )
    assert len(links) == 2 and all(item["relation"] == "same_work" for item in links)
    assert links[0]["to_url"] == "https://arxiv.org/abs/2401.12345"
    assert links[1]["to_url"] == "https://doi.org/10.1234/example"


# 功能：URL官方/学术预估不能补足少量正文，只有书目声明也不提高等级。
@pytest.mark.parametrize(
    "url",
    [
        "https://agency.gov/report",
        "https://arxiv.org/abs/2401.12345",
        "https://www.w3.org/TR/demo",
        "https://doi.org/10.1234/work",
    ],
)
def test_url_and_metadata_alone_do_not_grant_quality(url):
    assessment, audit = assess_content(_doc(url, text="只有摘要"))
    assert assessment.evidence_level <= 1 and audit["signals"] == {}


# 功能：正文结构、引文、书目共同产生有上限的分类，保留可定位锚点；不宣称认证同行评审。
def test_content_quality_is_auditable_and_bounded():
    doc = _doc("https://arxiv.org/abs/2401.12345")
    quality, audit = assess_content(doc)
    assert quality.quality_class == "structured_research" and quality.evidence_level == 3
    assert "同行评审" in audit["unknown"]
    for signal in audit["signals"].values():
        assert doc.text[signal["start"] : signal["end"]] in signal["excerpt"]
    assert assess_content(_doc(published_date="not-a-date"))[0].evidence_level == 1
    assert (
        assess_content(doc, RuleBasedSourceQualityClassifier(textbook_level=1))[0].evidence_level
        <= 1
    )


# 功能：模型输出格式再像论文也只能等级0；明确撤稿声明停止供支持证据使用。
@pytest.mark.parametrize(
    "kind", ["model", "model_output", "model_observation", "web_model", " MODEL "]
)
def test_model_content_stays_level_zero(kind):
    assert assess_content(_doc(source_type=kind))[0].evidence_level == 0


# 功能：撤稿信号和用户导入文档不能自动得到高等级。
def test_retraction_and_local_import_are_conservative():
    assert assess_content(_doc(text="撤稿声明\n" + BODY))[0].evidence_level == 0
    assert assess_content(_doc(source_type="local_document"))[0].evidence_level == 1


# 功能：验证读取器在删除脚本前提取书目/同作品声明，并对实际截取正文进行质量分类。
@pytest.mark.asyncio
async def test_web_reader_extracts_bibliography_before_scripts_removed(monkeypatch):
    # 功能：返回离线HTML替身，避免访问DNS/网络或真实来源。
    async def fetch(url):
        return URL, {"content-type": "text/html"}, HTML.encode(), "utf-8"

    monkeypatch.setattr("autodidact.tools.safe_fetch.fetch_public", fetch)
    doc = await WebReader().read(URL)
    assert doc.authors == ["作者甲", "作者乙"] and len(doc.research_identifiers) == 3
    assert doc.metadata["bibliography"]["audit"]["observations"]
    assert doc.metadata["quality_audit"]["protocol"] == "content_quality_v1"
    assert any(
        item["signal"] == "declared_identifier:arxiv" for item in doc.metadata["lineage_links"]
    )


class _SourceSession(_MemorySession):
    # 功能：扩展既有内存查询替身的正文hash筛选，不改变业务仓库测试接口。
    async def execute(self, statement):
        if statement.is_select and statement.column_descriptions[0]["entity"] is models.Source:
            key = statement.compile().params.get("content_hash_1")
            return _Result(
                [
                    row
                    for row in self.rows
                    if isinstance(row, models.Source) and row.content_hash == key
                ]
            )
        return await super().execute(statement)


# 功能：来源入库后保留作者日期和审计，同正文镜像声明不覆盖原值或提高旧质量。
@pytest.mark.asyncio
async def test_source_bibliography_persists_and_mirror_cannot_overwrite():
    session = _SourceSession()
    repo = Repository(session)
    source = await repo.upsert_source(_doc(), "body-hash")
    assert source.author == "作者甲" and source.published_at.year == 2024
    source.evidence_level = 1
    mirrored = await repo.upsert_source(
        _doc("https://mirror.example/work", authors=["镜像冒认作者"], published_date="2025-01-01"),
        "body-hash",
    )
    assert mirrored is source and source.author == "作者甲" and source.published_at.year == 2024
    assert (
        source.evidence_level == 1 and len(source.metadata_json["bibliography_observations"]) == 1
    )
    audit = await repo.source_audit(source.id)
    assert audit["bibliography"]["authors"] == ["作者甲"]
    assert audit["effective_quality"]["evidence_level"] == 1


# 功能：旧来源没有书目审计时按正文上限只降不升，且不改写存储状态。
def test_legacy_quality_is_capped_without_rewriting():
    row = SimpleNamespace(
        id=uuid4(),
        url="https://agency.gov/report",
        extracted_text="只有标题",
        source_type="web",
        metadata_json={},
        evidence_level=4,
        credibility_score=0.9,
    )
    assert effective_source_assessment(row).evidence_level == 1
    assert row.evidence_level == 4 and row.metadata_json == {}
    assert classify_document(_doc()).metadata["quality_audit"]["decision"]["evidence_level"] == 2


# 功能：确认正文上限进入实际EvidenceSource与晋升门控，不只是存一份审计文字。
@pytest.mark.asyncio
async def test_quality_cap_is_used_by_lineage_and_promotion():
    from autodidact.knowledge.lineage import SourceLineage
    from autodidact.knowledge.promotion import BeliefPromotionPolicy

    class EmptyLinks:
        # 功能：返回空依赖图的只读替身，避免在线数据库访问。
        async def scalars(self, statement):
            return SimpleNamespace(all=list)

    rows = [
        models.Source(
            id=uuid4(),
            url=url,
            normalized_url=url,
            publisher_key=url,
            source_type="web",
            extracted_text="只有标题",
            metadata_json={},
            content_hash=str(uuid4()),
            evidence_level=4,
            credibility_score=0.9,
        )
        for url in ("https://agency.gov/report", "https://other.gov/report")
    ]
    sources = await SourceLineage(EmptyLinks()).evidence_sources(rows)
    assert [item.evidence_level for item in sources] == [1, 1]
    policy = BeliefPromotionPolicy(2, 2)
    assert not policy.decide(
        evaluation_passed=True, has_open_dispute=False, sources=sources
    ).verified
    for row in rows:
        row.extracted_text = "撤稿声明\n" + BODY
    sources = await SourceLineage(EmptyLinks()).evidence_sources(rows)
    assert not policy.decide(
        evaluation_passed=True, has_open_dispute=False, sources=sources
    ).promote


# 功能：损坏的历史书目字段失败关闭，不停止整个学习器或提高来源等级。
def test_malformed_bibliography_fails_closed():
    row = SimpleNamespace(
        url=URL,
        extracted_text=BODY,
        source_type="web",
        evidence_level=4,
        credibility_score=0.9,
        metadata_json={
            "bibliography": {
                "authors": [" "],
                "research_identifiers": [{"kind": "invented", "value": "x"}],
            }
        },
    )
    assert effective_source_assessment(row).evidence_level == 0


# 功能：同URL重读可补未知书目而不覆盖首次非空值，不因补字段自动提升旧等级。
@pytest.mark.asyncio
async def test_same_url_can_fill_unknown_bibliography_without_promotion():
    session = _SourceSession()
    repo = Repository(session)
    source = await repo.upsert_source(
        _doc(authors=[], published_date="", research_identifiers=[]), "body-hash"
    )
    assert source.author is None and source.published_at is None and source.evidence_level == 1
    assert await repo.upsert_source(_doc(), "body-hash") is source
    assert source.author == "作者甲" and source.published_at.year == 2024
    assert source.metadata_json["bibliography"]["authors"] == ["作者甲"]
    assert source.evidence_level == 1
