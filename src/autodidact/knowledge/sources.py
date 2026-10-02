from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref_src",
}
_MULTI_LABEL_PUBLIC_SUFFIXES = {
    "ac.uk",
    "co.jp",
    "co.uk",
    "com.au",
    "com.cn",
    "edu.cn",
    "gov.cn",
    "gov.uk",
    "net.cn",
    "org.cn",
    "org.uk",
}
_PUBLIC_HOSTING_SUFFIXES = {"github.io", "gitlab.io", "readthedocs.io"}

_GOVERNMENT_SUFFIXES = {"gov.au", "gov.cn", "gov.uk", "go.jp"}
_ACADEMIC_SUFFIXES = {"ac.uk", "edu.au", "edu.cn", "edu.hk"}


def normalize_url(url: str) -> str:
    """Return a stable web URL for provenance and duplicate detection.

    The transformation intentionally removes only fragments, common tracking
    parameters, default ports and a trailing slash. Content-affecting query
    parameters are retained and sorted.
    """
    raw = url.strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return raw

    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return raw
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = host if port is None or default_port else f"{host}:{port}"

    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMETERS
    ]
    query = urlencode(sorted(query_items))
    return urlunsplit((scheme, netloc, path, query, ""))


def publisher_key(url: str) -> str:
    """Return a conservative publisher identity without a public-suffix dependency."""
    host = (urlsplit(normalize_url(url)).hostname or "").lower().removeprefix("www.")
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in _PUBLIC_HOSTING_SUFFIXES:
        return host
    if last_two in _MULTI_LABEL_PUBLIC_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return last_two


@dataclass(frozen=True, slots=True)
class SourceAssessment:
    quality_class: str
    evidence_level: int
    credibility_score: float
    reason: str


class RuleBasedSourceQualityClassifier:
    """Conservative, inspectable first-pass classifier.

    This classifier supplies metadata for later evidence review; it never proves
    that a source is correct or that a claim is supported by the source.
    """

    def __init__(
        self,
        *,
        model_level: int = 0,
        ordinary_web_level: int = 1,
        high_quality_secondary_level: int = 2,
        textbook_level: int = 3,
        primary_or_official_level: int = 4,
        proof_or_experiment_level: int = 5,
    ):
        self.model_level = model_level
        self.ordinary_web_level = ordinary_web_level
        self.high_quality_secondary_level = high_quality_secondary_level
        self.textbook_level = textbook_level
        self.primary_or_official_level = primary_or_official_level
        self.proof_or_experiment_level = proof_or_experiment_level

    def assess(self, url: str, source_type: str = "web") -> SourceAssessment:
        kind = source_type.strip().lower()
        normalized = normalize_url(url)
        parsed = urlsplit(normalized)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()

        if kind in {"model", "model_output", "model_observation", "web_model"}:
            return SourceAssessment(
                "model_output",
                self.model_level,
                0.0,
                "模型输出只能作为待验证线索，不能作为事实证据",
            )
        if ((host == "ietf.org" or host.endswith(".ietf.org")) and "/rfc" in path) or (
            (host == "w3.org" or host.endswith(".w3.org"))
            and (path == "/tr" or path.startswith("/tr/"))
        ):
            return SourceAssessment(
                "official_standard",
                self.proof_or_experiment_level,
                0.95,
                "URL 指向可识别的官方标准发布路径",
            )
        if kind in {"official", "official_documentation", "primary"} or _is_government_host(host):
            return SourceAssessment(
                "primary_or_official",
                self.primary_or_official_level,
                0.85,
                "来源类型或政府域名表明其可能是官方或一手资料",
            )
        if kind in {"peer_reviewed", "journal_article"} or host == "doi.org":
            return SourceAssessment(
                "scholarly_primary",
                self.primary_or_official_level,
                0.8,
                "来源类型或 DOI 入口表明其可能是学术一手资料，仍需核对正文",
            )
        if host == "arxiv.org" or host.endswith(".arxiv.org"):
            return SourceAssessment(
                "preprint",
                self.textbook_level,
                0.65,
                "预印本是较强技术资料，但不等同于已完成同行评审",
            )
        if kind in {"textbook", "reference_book"} or _is_academic_host(host):
            return SourceAssessment(
                "academic_or_textbook",
                self.textbook_level,
                0.7,
                "来源类型或学术域名表明其可能是教材或学术资料",
            )
        if kind in {"secondary", "technical_article"}:
            return SourceAssessment(
                "high_quality_secondary",
                self.high_quality_secondary_level,
                0.55,
                "来源被标记为二手技术资料，仍需追溯其原始引用",
            )
        return SourceAssessment(
            "ordinary_web",
            self.ordinary_web_level,
            0.3,
            "普通网页默认按低等级、未验证材料处理",
        )


def _is_government_host(host: str) -> bool:
    return host.endswith((".gov", ".mil")) or any(
        host == suffix or host.endswith(f".{suffix}") for suffix in _GOVERNMENT_SUFFIXES
    )


def _is_academic_host(host: str) -> bool:
    return host.endswith(".edu") or any(
        host == suffix or host.endswith(f".{suffix}") for suffix in _ACADEMIC_SUFFIXES
    )


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    source_id: str
    lineage_key: str = ""
    dependency_keys: frozenset[str] = frozenset()
    normalized_url: str = ""
    publisher_key: str = ""
    content_hash: str = ""
    evidence_level: int = 1
    credibility_score: float = 0.3


def independent_source_representatives(sources: list[EvidenceSource]) -> list[EvidenceSource]:
    """Collapse sources sharing a publisher or exact content into evidence groups."""
    unique_by_id = {source.source_id: source for source in sources}
    items = list(unique_by_id.values())
    if not items:
        return []

    parents = list(range(len(items)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for left_index, left in enumerate(items):
        for right_index in range(left_index + 1, len(items)):
            right = items[right_index]
            same_publisher = bool(
                left.publisher_key
                and right.publisher_key
                and left.publisher_key == right.publisher_key
            )
            same_content = bool(
                left.content_hash and right.content_hash and left.content_hash == right.content_hash
            )
            same_lineage = bool(
                left.lineage_key and right.lineage_key and left.lineage_key == right.lineage_key
            )
            dependencies = bool(left.dependency_keys.intersection(right.dependency_keys))
            if same_publisher or same_content or same_lineage or dependencies:
                union(left_index, right_index)

    groups: dict[int, list[EvidenceSource]] = {}
    for index, item in enumerate(items):
        groups.setdefault(find(index), []).append(item)
    return [
        max(group, key=lambda item: (item.evidence_level, item.credibility_score))
        for group in groups.values()
    ]
