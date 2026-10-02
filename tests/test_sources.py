# 文件职责：检查 URL、出版方、规则来源质量及同源独立性折叠。
from autodidact.knowledge.sources import (
    EvidenceSource,
    RuleBasedSourceQualityClassifier,
    independent_source_representatives,
    normalize_url,
    publisher_key,
)


# 功能：验证去跟踪/片段时保留影响内容的查询参数。
def test_url_normalization_removes_tracking_and_fragment_but_keeps_content_query():
    url = "HTTPS://Example.COM:443/docs/?utm_source=newsletter&b=2&a=1#section"

    assert normalize_url(url) == "https://example.com/docs?a=1&b=2"


# 功能：验证普通子域合并，而不同公共托管租户保持区分。
def test_publisher_key_groups_subdomains_but_not_public_hosting_tenants():
    assert publisher_key("https://docs.python.org/3/") == "python.org"
    assert publisher_key("https://alice.github.io/project") == "alice.github.io"


# 功能：验证模型材料等级 0 与规则分类的保守输出。
def test_quality_classifier_is_conservative_and_model_output_is_level_zero():
    classifier = RuleBasedSourceQualityClassifier()

    assert classifier.assess("https://example.com/post").evidence_level == 1
    assert classifier.assess("https://arxiv.org/abs/1234.5678").evidence_level == 3
    assert classifier.assess("https://example.com/chat", "web_model").evidence_level == 0
    assert classifier.assess("https://www.ietf.org/rfc/rfc9110.html").evidence_level == 5
    assert classifier.assess("https://gov.evil.example/post").evidence_level == 1
    assert classifier.assess("https://edu.evil.example/post").evidence_level == 1


# 功能：验证同出版方及镜像正文不能计作额外独立来源。
def test_source_independence_collapses_same_publisher_and_mirrored_content():
    sources = [
        EvidenceSource("a", publisher_key="example.org", content_hash="one", evidence_level=2),
        EvidenceSource("b", publisher_key="example.org", content_hash="two", evidence_level=4),
        EvidenceSource("c", publisher_key="other.org", content_hash="two", evidence_level=3),
        EvidenceSource(
            "d", publisher_key="independent.net", content_hash="three", evidence_level=2
        ),
    ]

    representatives = independent_source_representatives(sources)

    assert {source.source_id for source in representatives} == {"b", "d"}
